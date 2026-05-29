"""
Odoo enrichment orchestrator.

Three concurrent loops share the Casa del Libro page pool:

1. queue_refill_loop — when the queue has fewer than REFILL_THRESHOLD pending
   rows, pulls the next batch of books from Odoo (FIFO by create_date) where
   description_sale is empty.

2. scrape_workers (POOL_SIZE) — each consumes pending rows from the queue,
   scrapes Casa del Libro by ISBN, stores the scraped JSON. If not found,
   moves the row to notfound_books.

3. push_loop — periodically takes 'scraped' rows, writes the HTML description
   back to Odoo in batches, marks them 'written'.

State lives in enrichment_queue + notfound_books tables (schema in scraper.init_db).
A single global job dict tracks live progress and stop signal.
"""
import asyncio
import html
import json
import os
import random
import re
from datetime import datetime
from typing import Any

from playwright.async_api import async_playwright

import db
from scraper import (
    BASE_URL, PROXY_URL, CHROMIUM_ARGS, _setup_page, scrape_book, init_db,
    check_in_db_by_isbn, lookup_url_by_isbn,
)
from odoo_client import OdooClient, OdooError

POOL_SIZE = int(os.environ.get("BULK_POOL_SIZE", "6"))
REFILL_THRESHOLD = int(os.environ.get("ENRICH_QUEUE_REFILL_AT", "500"))
REFILL_BATCH = int(os.environ.get("ENRICH_QUEUE_REFILL_SIZE", "2000"))
PUSH_BATCH = int(os.environ.get("ENRICH_PUSH_BATCH", "50"))
PUSH_INTERVAL_S = int(os.environ.get("ENRICH_PUSH_INTERVAL_S", "30"))
MAX_ATTEMPTS = int(os.environ.get("ENRICH_MAX_ATTEMPTS", "1"))
# Identificador del servidor para multi-server deploy. Solo telemetria —
# la garantia de no-duplicacion la da FOR UPDATE SKIP LOCKED en Postgres.
WORKER_NAME = os.environ.get("WORKER_NAME", "default")
# Si el ISBN no esta en cdl_isbn_index NI en books table, ¿hacer search en CDL?
# Por defecto NO — fast-fail directo a notfound. Pone esto a "1" si quieres
# que el worker pague el costo del search para los huerfanos.
FALLBACK_TO_SEARCH = os.environ.get("ENRICH_FALLBACK_SEARCH", "0") == "1"

# Global singleton job state (only one enrichment job runs at a time)
enrichment_job: dict | None = None


# ── DB helpers ─────────────────────────────────────────────────────────
def _count_queue_by_status() -> dict[str, int]:
    conn = db.get_connection()
    cur = conn.cursor()
    out = {}
    for status in ("pending", "scraping", "scraped", "pushing", "written"):
        db.execute_query(cur, "SELECT COUNT(*) FROM enrichment_queue WHERE status = ?", (status,))
        out[status] = cur.fetchone()[0]
    db.execute_query(cur, "SELECT COUNT(*) FROM notfound_books")
    out["notfound"] = cur.fetchone()[0]
    conn.close()
    return out


def _queue_insert_many(rows: list[dict]) -> int:
    """Insert new Odoo IDs into the queue, ignoring those already present."""
    if not rows:
        return 0
    conn = db.get_connection()
    cur = conn.cursor()
    inserted = 0
    for r in rows:
        try:
            if db.IS_POSTGRES:
                db.execute_query(cur, """
                    INSERT INTO enrichment_queue (odoo_id, barcode, name, status)
                    VALUES (?, ?, ?, 'pending')
                    ON CONFLICT (odoo_id) DO NOTHING
                """, (r["id"], r.get("barcode") or "", r.get("name") or ""))
            else:
                db.execute_query(cur, """
                    INSERT OR IGNORE INTO enrichment_queue (odoo_id, barcode, name, status)
                    VALUES (?, ?, ?, 'pending')
                """, (r["id"], r.get("barcode") or "", r.get("name") or ""))
            inserted += cur.rowcount or 0
        except Exception as e:
            print(f"[Enrich] Insert error for odoo_id={r.get('id')}: {e}")
    conn.commit()
    conn.close()
    return inserted


def _claim_pending(limit: int = 1) -> list[dict]:
    """
    Atomically claim pending rows: SELECT FOR UPDATE SKIP LOCKED + UPDATE
    en una sola transacción. Multi-server-safe: dos procesos contra el mismo
    Postgres NUNCA reclaman la misma fila — el lock de Postgres lo garantiza.

    En SQLite (single-server local) usamos el patron antiguo de 2 pasos
    porque SQLite no tiene SKIP LOCKED (y locks todo el archivo en escrituras
    de todos modos, asi que no hay race).
    """
    conn = db.get_connection()
    cur = conn.cursor()

    if db.IS_POSTGRES:
        # Update + RETURNING + subquery con FOR UPDATE SKIP LOCKED = patron
        # canonico de Postgres para queue workers. Una sola query, atomica.
        # %s placeholders porque db.execute_query traduce de ? a %s.
        db.execute_query(cur, """
            UPDATE enrichment_queue
            SET status='scraping',
                claimed_by=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE odoo_id IN (
                SELECT odoo_id FROM enrichment_queue
                WHERE status='pending'
                ORDER BY queued_at ASC
                LIMIT ?
                FOR UPDATE SKIP LOCKED
            )
            RETURNING odoo_id, barcode, name, attempts
        """, (WORKER_NAME, limit))
        rows = [
            {"odoo_id": r[0], "barcode": r[1], "name": r[2], "attempts": r[3]}
            for r in cur.fetchall()
        ]
        conn.commit()
    else:
        db.execute_query(cur, """
            SELECT odoo_id, barcode, name, attempts
            FROM enrichment_queue
            WHERE status = 'pending'
            ORDER BY queued_at ASC
            LIMIT ?
        """, (limit,))
        rows = [
            {"odoo_id": r[0], "barcode": r[1], "name": r[2], "attempts": r[3]}
            for r in cur.fetchall()
        ]
        if rows:
            ids = [r["odoo_id"] for r in rows]
            placeholders = ",".join(["?"] * len(ids))
            db.execute_query(cur,
                f"UPDATE enrichment_queue SET status='scraping', "
                f"claimed_by=?, updated_at=CURRENT_TIMESTAMP "
                f"WHERE odoo_id IN ({placeholders})",
                (WORKER_NAME, *ids)
            )
            conn.commit()
    conn.close()
    return rows


def _mark_scraped(odoo_id: int, data: dict):
    conn = db.get_connection()
    cur = conn.cursor()
    db.execute_query(cur, """
        UPDATE enrichment_queue
        SET status='scraped', scraped_data=?, updated_at=CURRENT_TIMESTAMP
        WHERE odoo_id = ?
    """, (json.dumps(data, ensure_ascii=False), odoo_id))
    conn.commit()
    conn.close()


def _mark_notfound(odoo_id: int, barcode: str, name: str, reason: str):
    """Move row from queue → notfound_books (delete from queue)."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        if db.IS_POSTGRES:
            db.execute_query(cur, """
                INSERT INTO notfound_books (odoo_id, barcode, name, reason)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (odoo_id) DO UPDATE SET
                    attempts = notfound_books.attempts + 1,
                    last_attempt = CURRENT_TIMESTAMP,
                    reason = EXCLUDED.reason
            """, (odoo_id, barcode, name, reason))
        else:
            db.execute_query(cur, """
                INSERT OR REPLACE INTO notfound_books
                (odoo_id, barcode, name, reason, attempts, first_seen, last_attempt)
                VALUES (?, ?, ?, ?,
                    COALESCE((SELECT attempts FROM notfound_books WHERE odoo_id = ?), 0) + 1,
                    COALESCE((SELECT first_seen FROM notfound_books WHERE odoo_id = ?), CURRENT_TIMESTAMP),
                    CURRENT_TIMESTAMP)
            """, (odoo_id, barcode, name, reason, odoo_id, odoo_id))
        db.execute_query(cur, "DELETE FROM enrichment_queue WHERE odoo_id = ?", (odoo_id,))
        conn.commit()
    finally:
        conn.close()


def _bump_attempt(odoo_id: int, err: str):
    """Failed transient attempt — keep in queue, increment attempts. Will be retried until MAX_ATTEMPTS."""
    conn = db.get_connection()
    cur = conn.cursor()
    db.execute_query(cur, """
        UPDATE enrichment_queue
        SET status='pending', attempts=attempts+1,
            last_error=?, updated_at=CURRENT_TIMESTAMP
        WHERE odoo_id = ?
    """, (err[:500], odoo_id))
    conn.commit()
    conn.close()


def _claim_scraped_batch(limit: int) -> list[dict]:
    """
    Atomically claim a batch of 'scraped' rows for pushing to Odoo: marca
    como 'pushing' para que otro server no las tome.
    Multi-server-safe: FOR UPDATE SKIP LOCKED en Postgres.
    """
    conn = db.get_connection()
    cur = conn.cursor()

    if db.IS_POSTGRES:
        db.execute_query(cur, """
            UPDATE enrichment_queue
            SET status='pushing',
                claimed_by=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE odoo_id IN (
                SELECT odoo_id FROM enrichment_queue
                WHERE status='scraped'
                ORDER BY updated_at ASC
                LIMIT ?
                FOR UPDATE SKIP LOCKED
            )
            RETURNING odoo_id, scraped_data
        """, (WORKER_NAME, limit))
        rows = [
            {"odoo_id": r[0], "data": json.loads(r[1])}
            for r in cur.fetchall() if r[1]
        ]
        conn.commit()
    else:
        db.execute_query(cur, """
            SELECT odoo_id, scraped_data FROM enrichment_queue
            WHERE status = 'scraped' ORDER BY updated_at ASC LIMIT ?
        """, (limit,))
        rows = [{"odoo_id": r[0], "data": json.loads(r[1])} for r in cur.fetchall() if r[1]]
        if rows:
            ids = [r["odoo_id"] for r in rows]
            placeholders = ",".join(["?"] * len(ids))
            db.execute_query(cur,
                f"UPDATE enrichment_queue SET status='pushing', "
                f"claimed_by=?, updated_at=CURRENT_TIMESTAMP "
                f"WHERE odoo_id IN ({placeholders})",
                (WORKER_NAME, *ids))
            conn.commit()
    conn.close()
    return rows


def _mark_written(odoo_ids: list[int]):
    if not odoo_ids:
        return
    conn = db.get_connection()
    cur = conn.cursor()
    placeholders = ",".join(["?"] * len(odoo_ids))
    db.execute_query(cur,
        f"UPDATE enrichment_queue SET status='written', "
        f"updated_at=CURRENT_TIMESTAMP WHERE odoo_id IN ({placeholders})",
        tuple(odoo_ids)
    )
    conn.commit()
    conn.close()


def _revert_pushing(odoo_ids: list[int]):
    """
    Si el push a Odoo falla, devolver las filas a 'scraped' para reintento
    en el siguiente push_loop tick.
    """
    if not odoo_ids:
        return
    conn = db.get_connection()
    cur = conn.cursor()
    placeholders = ",".join(["?"] * len(odoo_ids))
    db.execute_query(cur,
        f"UPDATE enrichment_queue SET status='scraped', "
        f"updated_at=CURRENT_TIMESTAMP WHERE odoo_id IN ({placeholders}) "
        f"AND status='pushing'",
        tuple(odoo_ids)
    )
    conn.commit()
    conn.close()


def _reclaim_stuck_pushing(stuck_minutes: int = 10):
    """
    Recovery: filas marcadas 'pushing' por mas de N minutos probablemente
    son de un worker que murio. Devolverlas a 'scraped' para que cualquier
    worker (incluyendo otro server) las re-procese.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    if db.IS_POSTGRES:
        db.execute_query(cur, """
            UPDATE enrichment_queue
            SET status='scraped', updated_at=CURRENT_TIMESTAMP
            WHERE status='pushing'
              AND updated_at < (CURRENT_TIMESTAMP - (? * INTERVAL '1 minute'))
        """, (stuck_minutes,))
    else:
        db.execute_query(cur, """
            UPDATE enrichment_queue
            SET status='scraped', updated_at=CURRENT_TIMESTAMP
            WHERE status='pushing'
              AND datetime(updated_at) < datetime('now', '-' || ? || ' minutes')
        """, (stuck_minutes,))
    n = cur.rowcount or 0
    conn.commit()
    conn.close()
    if n:
        print(f"[Enrich] Recovery: {n} filas 'pushing' stuck reverted to 'scraped'")
    return n


# ── HTML description renderer ──────────────────────────────────────────
def render_html_description(d: dict) -> str:
    """Build an enriched HTML description from scraped Casa del Libro data."""
    def esc(v: Any) -> str:
        return html.escape(str(v).strip()) if v else ""

    synopsis = esc(d.get("description", ""))
    if synopsis in ("No Description", ""):
        synopsis = ""

    ficha_pairs = [
        ("ISBN", d.get("isbn")),
        ("Autor", d.get("author")),
        ("Editorial", d.get("editorial")),
        ("Traductor", d.get("translator")),
        ("Ilustrador", d.get("illustrator")),
        ("Idioma", d.get("language")),
        ("Páginas", d.get("pages")),
        ("Encuadernación", d.get("binding")),
        ("Fecha de publicación", d.get("release_date")),
        ("Año de edición", d.get("edition_year")),
        ("Plaza de edición", d.get("edition_place")),
        ("Colección", d.get("collection")),
        ("Tiempo de lectura", d.get("reading_time")),
        ("Alto", d.get("height")),
        ("Ancho", d.get("width")),
        ("Peso", d.get("weight")),
        ("Origen", d.get("origin")),
    ]
    ficha_html = ""
    rows = [f"<li><strong>{k}:</strong> {esc(v)}</li>"
            for k, v in ficha_pairs if v and str(v).strip() and str(v).strip().lower() != "unknown"]
    if rows:
        ficha_html = (
            "<h3 style='margin-top:1em'>Ficha técnica</h3>\n"
            "<ul style='list-style:none;padding-left:0'>\n"
            + "\n".join(rows) +
            "\n</ul>"
        )

    parts = []
    if synopsis:
        parts.append(f"<p>{synopsis}</p>")
    if ficha_html:
        parts.append(ficha_html)
    return "\n".join(parts) if parts else ""


# ── Loops ──────────────────────────────────────────────────────────────
def _reclaim_stuck_scraping(stuck_minutes: int = 15) -> int:
    """Worker que murio dejo filas en 'scraping'. Devolverlas a 'pending'."""
    conn = db.get_connection()
    cur = conn.cursor()
    if db.IS_POSTGRES:
        db.execute_query(cur, """
            UPDATE enrichment_queue
            SET status='pending', claimed_by=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE status='scraping'
              AND updated_at < (CURRENT_TIMESTAMP - (? * INTERVAL '1 minute'))
        """, (stuck_minutes,))
    else:
        db.execute_query(cur, """
            UPDATE enrichment_queue
            SET status='pending', claimed_by=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE status='scraping'
              AND datetime(updated_at) < datetime('now', '-' || ? || ' minutes')
        """, (stuck_minutes,))
    n = cur.rowcount or 0
    conn.commit()
    conn.close()
    if n:
        print(f"[Enrich] Recovery: {n} filas 'scraping' stuck reverted to 'pending'")
    return n


async def queue_refill_loop(odoo: OdooClient, job: dict):
    """Fill the queue from Odoo whenever pending count drops below threshold."""
    domain = [
        ["barcode", "!=", False],
        ["description_sale", "=", False],
    ]
    last_offset = job.get("odoo_offset", 0)
    recovery_tick = 0

    while job["status"] == "running":
        counts = _count_queue_by_status()
        recovery_tick += 1
        # Recovery periodico de filas stuck (worker que murio sin liberar)
        if recovery_tick % 5 == 0:
            try:
                _reclaim_stuck_scraping(stuck_minutes=15)
            except Exception as e:
                print(f"[Enrich] Scraping recovery error: {e}")

        if counts["pending"] >= REFILL_THRESHOLD:
            await asyncio.sleep(20)
            continue

        try:
            rows = await odoo.search_read(
                "product.template", domain,
                fields=["id", "barcode", "name"],
                offset=last_offset, limit=REFILL_BATCH,
                order="create_date asc, id asc",
            )
        except Exception as e:
            job["errors"].append(f"refill: {str(e)[:120]}")
            print(f"[Enrich] Odoo refill error: {e}")
            await asyncio.sleep(60)
            continue

        if not rows:
            print(f"[Enrich] No more pending books in Odoo at offset {last_offset}. Refill loop done.")
            job["refill_done"] = True
            return

        inserted = _queue_insert_many(rows)
        last_offset += len(rows)
        job["odoo_offset"] = last_offset
        job["odoo_total_seen"] = job.get("odoo_total_seen", 0) + len(rows)
        print(f"[Enrich] Refilled: fetched {len(rows)} from Odoo (offset now {last_offset}), {inserted} new in queue")


async def scrape_worker(worker_id: int, page_queue: asyncio.Queue, job: dict):
    """
    Cascading cache lookup for each Odoo book:
      1. books table by ISBN → reuse local data (0 Playwright cost)
      2. cdl_isbn_index → scrape with direct URL (1 nav instead of 2)
      3. Search fallback (default OFF) → scrape via search→click
      4. else → mark notfound
    """
    while job["status"] == "running":
        claimed = _claim_pending(limit=1)
        if not claimed:
            await asyncio.sleep(5)
            continue

        row = claimed[0]
        odoo_id, barcode, name = row["odoo_id"], row["barcode"], row["name"]
        attempts = row["attempts"]

        if not barcode or not str(barcode).strip().isdigit() or len(str(barcode)) < 10:
            _mark_notfound(odoo_id, barcode, name, "Invalid ISBN/barcode")
            job["notfound"] += 1
            continue

        # ── Fast path 1: cache lookup in local books table ──
        cached = check_in_db_by_isbn(barcode)
        if cached and cached.get("title") and cached["title"] != "Unknown Title":
            cached["odoo_id"] = odoo_id
            cached["isbn"] = cached.get("isbn") or barcode
            _mark_scraped(odoo_id, cached)
            job["scraped"] += 1
            job["cache_hits"] += 1
            continue

        # ── Fast path 2: direct URL from sitemap ISBN index ──
        direct_url = lookup_url_by_isbn(barcode)

        # ── Fast-fail: ISBN not in our world at all ──
        if not direct_url and not FALLBACK_TO_SEARCH:
            _mark_notfound(odoo_id, barcode, name, "Not in CDL ISBN index")
            job["notfound"] += 1
            continue

        job["current_book"] = (
            f"W{worker_id} {'[direct]' if direct_url else '[search]'}: "
            f"{(name or barcode)[:60]}"
        )

        page = await page_queue.get()
        try:
            data = await scrape_book(page, query=barcode, direct_url=direct_url)
            if data and data.get("title") and data["title"] != "Unknown Title":
                data["odoo_id"] = odoo_id
                _mark_scraped(odoo_id, data)
                job["scraped"] += 1
                if direct_url:
                    job["direct_hits"] += 1
            else:
                if attempts + 1 >= MAX_ATTEMPTS:
                    _mark_notfound(odoo_id, barcode, name, "Not found on Casa del Libro")
                    job["notfound"] += 1
                else:
                    _bump_attempt(odoo_id, "scrape returned no data")
                    job["retried"] += 1
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if attempts + 1 >= MAX_ATTEMPTS:
                _mark_notfound(odoo_id, barcode, name, err[:200])
                job["notfound"] += 1
            else:
                _bump_attempt(odoo_id, err)
                job["retried"] += 1
        finally:
            await page_queue.put(page)

        # Mucho menor delay porque los cache hits no requieren rate limiting
        await asyncio.sleep(random.uniform(0.2, 0.6))


async def push_loop(odoo: OdooClient, job: dict):
    """
    Periodically push scraped rows back to Odoo.
    Multi-server-safe: _claim_scraped_batch marca como 'pushing' atomicamente.
    Fallos revierten a 'scraped' para reintento. Cada 5 ticks corre recovery
    de filas 'pushing' stuck (por si un worker murio sin revertir).
    """
    tick = 0
    while job["status"] == "running":
        await asyncio.sleep(PUSH_INTERVAL_S)
        tick += 1
        if tick % 5 == 0:
            try:
                _reclaim_stuck_pushing(stuck_minutes=10)
            except Exception as e:
                print(f"[Enrich] Recovery error: {e}")

        batch = _claim_scraped_batch(PUSH_BATCH)
        if not batch:
            continue

        written_ids: list[int] = []
        failed_ids: list[int] = []
        for item in batch:
            try:
                html_desc = render_html_description(item["data"])
                if not html_desc:
                    _mark_notfound(
                        item["odoo_id"],
                        item["data"].get("isbn", ""),
                        item["data"].get("title", ""),
                        "Scraped but no usable content",
                    )
                    continue
                values = {"description": html_desc}
                ok = await odoo.write("product.template", [item["odoo_id"]], values)
                if ok:
                    written_ids.append(item["odoo_id"])
                else:
                    failed_ids.append(item["odoo_id"])
            except Exception as e:
                job["errors"].append(f"push odoo_id={item['odoo_id']}: {str(e)[:120]}")
                print(f"[Enrich] Push error for odoo_id={item['odoo_id']}: {e}")
                failed_ids.append(item["odoo_id"])

        if written_ids:
            _mark_written(written_ids)
            job["written"] += len(written_ids)
            print(f"[Enrich] Pushed {len(written_ids)} to Odoo (total written: {job['written']})")
        if failed_ids:
            _revert_pushing(failed_ids)


# ── Public entry point ────────────────────────────────────────────────
async def run_enrichment_job() -> str:
    """
    Start the enrichment job. Only one runs at a time.
    Returns a status string when done (or on stop signal).
    """
    global enrichment_job

    if enrichment_job and enrichment_job.get("status") == "running":
        raise RuntimeError("Enrichment job already running")

    init_db()
    enrichment_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "worker_name": WORKER_NAME,
        "current_book": "Conectando a Odoo...",
        "scraped": 0,
        "written": 0,
        "notfound": 0,
        "retried": 0,
        "cache_hits": 0,       # libros resueltos por books table (0 nav)
        "direct_hits": 0,      # libros resueltos por URL del sitemap (1 nav)
        "odoo_offset": 0,
        "odoo_total_seen": 0,
        "odoo_total_target": 0,
        "refill_done": False,
        "errors": [],
    }
    job = enrichment_job

    try:
        async with OdooClient() as odoo:
            # Cuenta total de target una sola vez (para el % en UI)
            try:
                job["odoo_total_target"] = await odoo.search_count(
                    "product.template",
                    [["barcode", "!=", False], ["description_sale", "=", False]],
                )
                print(f"[Enrich] Target total: {job['odoo_total_target']} libros sin description_sale")
            except Exception as e:
                print(f"[Enrich] No pude obtener target count: {e}")

            async with async_playwright() as p:
                launch_opts = {"headless": True, "args": CHROMIUM_ARGS}
                if PROXY_URL:
                    launch_opts["proxy"] = {"server": PROXY_URL}
                browser = await p.chromium.launch(**launch_opts)

                page_queue: asyncio.Queue = asyncio.Queue()
                for _ in range(POOL_SIZE):
                    pg = await browser.new_page()
                    await _setup_page(pg)
                    await page_queue.put(pg)

                # Start all loops concurrently
                workers = [scrape_worker(i, page_queue, job) for i in range(POOL_SIZE)]
                await asyncio.gather(
                    queue_refill_loop(odoo, job),
                    push_loop(odoo, job),
                    *workers,
                    return_exceptions=True,
                )

                await browser.close()
        if job["status"] == "running":
            job["status"] = "completed"
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"Fatal: {str(e)[:200]}")
        print(f"[Enrich] Fatal: {e}")

    job["finished_at"] = datetime.now().isoformat()
    return job["status"]


def stop_enrichment_job() -> bool:
    if enrichment_job and enrichment_job.get("status") == "running":
        enrichment_job["status"] = "stopped"
        return True
    return False


def get_enrichment_status() -> dict:
    """Live status for the UI poller."""
    job = dict(enrichment_job) if enrichment_job else {"status": "idle"}
    job["queue"] = _count_queue_by_status() if enrichment_job else None
    if "errors" in job:
        job["errors"] = job["errors"][-5:]
    return job


def get_notfound_count() -> int:
    conn = db.get_connection()
    cur = conn.cursor()
    db.execute_query(cur, "SELECT COUNT(*) FROM notfound_books")
    n = cur.fetchone()[0]
    conn.close()
    return n


def get_isbn_index_count() -> int:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, "SELECT COUNT(*) FROM cdl_isbn_index")
        n = cur.fetchone()[0]
    except Exception:
        n = 0
    conn.close()
    return n


isbn_index_job: dict | None = None


async def run_build_isbn_index_job() -> str:
    """
    Standalone one-shot job: read all CDL sitemap sub-files and populate
    the cdl_isbn_index table. ~5-10 min total. Idempotent (safe to re-run).
    """
    global isbn_index_job

    if isbn_index_job and isbn_index_job.get("status") == "running":
        raise RuntimeError("ISBN index job already running")

    init_db()
    isbn_index_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "message": "Iniciando lectura de sitemap...",
        "indexed": 0,
    }

    def _progress(msg: str):
        isbn_index_job["message"] = msg

    try:
        from sitemap import populate_isbn_index
        stats = await populate_isbn_index(progress_cb=_progress)
        isbn_index_job["indexed"] = stats.get("indexed", 0)
        isbn_index_job["status"] = "completed"
        isbn_index_job["message"] = f"✅ {isbn_index_job['indexed']} ISBNs indexados"
    except Exception as e:
        isbn_index_job["status"] = "error"
        isbn_index_job["message"] = f"Error: {str(e)[:200]}"
        print(f"[ISBN-Index] Fatal: {e}")

    isbn_index_job["finished_at"] = datetime.now().isoformat()
    return isbn_index_job["status"]


def get_isbn_index_job_status() -> dict:
    return dict(isbn_index_job) if isbn_index_job else {"status": "idle"}
