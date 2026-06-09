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
    launch_browser_pool, close_browser_pool,
)
from odoo_client import OdooClient, OdooError
import notify
import google_books
import aiohttp

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
# Google Books como fuente cascada cuando CDL no tiene el libro (o falla).
# Por defecto ENCENDIDO — es gratis, no usa Playwright y suele recuperar
# 60-80% de los libros que CDL no listo.
GBOOKS_FALLBACK = os.environ.get("ENRICH_GBOOKS_FALLBACK", "1") == "1"
# Cuando CDL si tiene el libro pero le faltan campos (description/idioma/etc),
# rellenar los huecos con Google Books. Cuesta 1 API call extra por libro
# scrapeado en CDL. Por defecto APAGADO — actviar si te importa la cobertura
# de campos mas que la velocidad.
GBOOKS_ENRICH_CDL = os.environ.get("ENRICH_GBOOKS_ENRICH_CDL", "0") == "1"

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
    """
    Construye el HTML enriquecido que va al campo `description` de Odoo.
    Acepta datos de Casa del Libro y/o Google Books (mismas keys, ver
    google_books._normalize). Solo emite secciones que tengan datos.
    """
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
        ("Grosor", d.get("thickness")),
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

    # Categorias / tags (CDL los trae como > separado; Google Books como lista)
    cats_html = ""
    cats = d.get("categories") or d.get("tags") or []
    if isinstance(cats, str):
        cats = [cats]
    cats = [c for c in cats if c]
    if cats:
        chips = "".join(f"<span style='background:#eef;padding:2px 8px;border-radius:12px;margin:2px;display:inline-block;font-size:0.9em'>{esc(c)}</span>" for c in cats[:10])
        cats_html = f"<p style='margin-top:1em'><strong>Categorías:</strong> {chips}</p>"

    # Rating si Google Books lo trajo
    rating = d.get("average_rating")
    rating_count = d.get("ratings_count")
    rating_html = ""
    if rating:
        rating_html = f"<p><strong>Valoración:</strong> {esc(rating)}/5"
        if rating_count:
            rating_html += f" ({esc(rating_count)} valoraciones)"
        rating_html += "</p>"

    # Link a preview de Google Books si esta
    preview_html = ""
    preview = d.get("preview_link")
    if preview:
        preview_html = (
            f"<p style='margin-top:0.5em'><a href='{esc(preview)}' target='_blank' "
            f"rel='noopener'>Ver vista previa en Google Books</a></p>"
        )

    # Fuente (atribucion discreta al final)
    source_html = ""
    source = d.get("source") or ""
    if source:
        labels = {
            "cdl": "Casa del Libro",
            "cdl_cache": "Casa del Libro (cache local)",
            "google_books": "Google Books",
            "cdl+google_books": "Casa del Libro + Google Books",
            "google_books+cdl": "Casa del Libro + Google Books",
        }
        label = labels.get(source, source)
        source_html = (
            f"<p style='font-size:0.75em;color:#888;margin-top:1em;font-style:italic'>"
            f"Fuente: {esc(label)}</p>"
        )

    parts = []
    if synopsis:
        parts.append(f"<p>{synopsis}</p>")
    if ficha_html:
        parts.append(ficha_html)
    if cats_html:
        parts.append(cats_html)
    if rating_html:
        parts.append(rating_html)
    if preview_html:
        parts.append(preview_html)
    if source_html:
        parts.append(source_html)
    return "\n".join(parts) if parts else ""


# ── Loops ──────────────────────────────────────────────────────────────
async def monitor_loop(job: dict):
    """
    Manda un reporte por Telegram cada NOTIFY_INTERVAL_MIN (default 60).
    No-op si las env vars TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID no estan.
    Tambien dispara alerta si el ritmo cae a 0 o si la tasa de errores
    sube por encima de cierto umbral (deteccion basica de ban).
    """
    if not notify.is_configured():
        print("[Monitor] Telegram no configurado, monitor_loop dormido")
        return

    interval_min = int(os.environ.get("NOTIFY_INTERVAL_MIN", "60"))
    interval_s = interval_min * 60
    ban_threshold = float(os.environ.get("NOTIFY_BAN_NOTFOUND_PCT", "0.85"))

    await notify.notify_job_started(job.get("odoo_total_target", 0))

    last_written = job.get("written", 0)
    last_notfound = job.get("notfound", 0)
    stagnant_ticks = 0

    while job["status"] == "running":
        await asyncio.sleep(interval_s)
        if job["status"] != "running":
            break

        try:
            counts = _count_queue_by_status()
            written = job.get("written", 0)
            notfound = job.get("notfound", 0)
            delta_written = written - last_written
            delta_notfound = notfound - last_notfound

            await notify.notify_stats(job, counts, delta_written, interval_min)

            # Alerta de estancamiento (0 escritos en el ultimo intervalo)
            if delta_written == 0 and delta_notfound == 0:
                stagnant_ticks += 1
                if stagnant_ticks >= 2:
                    await notify.notify_alert(
                        "warn", "Estancamiento detectado",
                        f"0 libros escritos ni notfound en los ultimos "
                        f"{stagnant_ticks * interval_min} minutos. "
                        f"Workers vivos: {counts.get('scraping', 0)}. "
                        f"Pending: {counts.get('pending', 0)}. "
                        f"Revisa logs del contenedor.")
                    stagnant_ticks = 0
            else:
                stagnant_ticks = 0

            # Alerta de posible ban (tasa notfound muy alta en este intervalo)
            total_processed = delta_written + delta_notfound
            if total_processed >= 30:  # solo con muestra significativa
                notfound_pct = delta_notfound / total_processed
                if notfound_pct >= ban_threshold:
                    await notify.notify_alert(
                        "critical", "Posible ban de IP",
                        f"En el ultimo intervalo: {delta_notfound}/{total_processed} "
                        f"libros marcados notfound ({notfound_pct*100:.0f}%). "
                        f"Casa del Libro puede estar bloqueando.")

            last_written = written
            last_notfound = notfound
        except Exception as e:
            print(f"[Monitor] error: {e}")


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


async def _try_google_books(gb_session, barcode: str) -> dict | None:
    """Lookup en Google Books con timeout corto. Devuelve None si falla o sin data."""
    if not gb_session:
        return None
    try:
        return await google_books.fetch_by_isbn(gb_session, barcode, timeout_s=10.0)
    except Exception:
        return None


async def scrape_worker(
    worker_id: int,
    page_queue: asyncio.Queue,
    job: dict,
    gb_session: aiohttp.ClientSession | None = None,
):
    """
    Cascading lookup for each Odoo book:
      1. books table by ISBN          → reuse local data (0 cost)
      2. cdl_isbn_index → scrape CDL  → datos completos (peso/altura/etc)
         2a. opcional: enriquecer con Google Books si CDL deja huecos
      3. Google Books API fallback    → para libros que CDL no tiene
      4. Search fallback en CDL (OFF) → cuando todo lo anterior falla
      5. else → mark notfound
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
            cached.setdefault("source", "cdl_cache")
            _mark_scraped(odoo_id, cached)
            job["scraped"] += 1
            job["cache_hits"] += 1
            continue

        # ── Fast path 2: direct URL from sitemap ISBN index ──
        direct_url = lookup_url_by_isbn(barcode)

        job["current_book"] = (
            f"W{worker_id} {'[direct]' if direct_url else '[gbooks]'}: "
            f"{(name or barcode)[:60]}"
        )

        cdl_data: dict | None = None
        cdl_error: str | None = None

        # ── Path A: CDL via direct URL ──
        if direct_url:
            page = await page_queue.get()
            try:
                data = await scrape_book(page, query=barcode, direct_url=direct_url)
                if data and data.get("title") and data["title"] != "Unknown Title":
                    data["source"] = "cdl"
                    cdl_data = data
                else:
                    cdl_error = "Not found on Casa del Libro"
            except Exception as e:
                cdl_error = f"{type(e).__name__}: {e}"
            finally:
                await page_queue.put(page)

        # ── Path B: Google Books (fallback o enrich) ──
        gb_data: dict | None = None
        need_gb = False
        if cdl_data is None and GBOOKS_FALLBACK:
            need_gb = True
        elif cdl_data is not None and GBOOKS_ENRICH_CDL:
            # Solo pegar GB si CDL no trajo description
            if not cdl_data.get("description") or cdl_data.get("description") in ("No Description", ""):
                need_gb = True

        if need_gb and gb_session:
            gb_data = await _try_google_books(gb_session, barcode)

        # ── Decision ──
        final_data = google_books.merge_book_data(cdl_data, gb_data)

        if final_data and final_data.get("title") and final_data.get("title") != "Unknown Title":
            final_data["odoo_id"] = odoo_id
            final_data["isbn"] = final_data.get("isbn") or barcode
            _mark_scraped(odoo_id, final_data)
            job["scraped"] += 1
            src = final_data.get("source", "")
            if direct_url and cdl_data:
                job["direct_hits"] += 1
            if "google_books" in src:
                job["gbooks_hits"] = job.get("gbooks_hits", 0) + 1
            if "+" in src:
                job["gbooks_merged"] = job.get("gbooks_merged", 0) + 1
            # Delay entre requests
            delay_min = float(os.environ.get("ENRICH_WORKER_DELAY_MIN", "1.5"))
            delay_max = float(os.environ.get("ENRICH_WORKER_DELAY_MAX", "3.5"))
            await asyncio.sleep(random.uniform(delay_min, delay_max))
            continue

        # ── Nothing worked: retry or mark notfound ──
        reason = cdl_error or "Not in CDL ISBN index and Google Books has no match"
        if attempts + 1 >= MAX_ATTEMPTS:
            _mark_notfound(odoo_id, barcode, name, reason[:200])
            job["notfound"] += 1
        else:
            _bump_attempt(odoo_id, reason)
            job["retried"] += 1

        # Delay
        delay_min = float(os.environ.get("ENRICH_WORKER_DELAY_MIN", "1.5"))
        delay_max = float(os.environ.get("ENRICH_WORKER_DELAY_MAX", "3.5"))
        await asyncio.sleep(random.uniform(delay_min, delay_max))


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
        "gbooks_hits": 0,      # libros resueltos solo por Google Books
        "gbooks_merged": 0,    # libros con CDL + Google Books mergeados
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
                # Si hay PROXY_POOL, levanta N browsers (uno por proxy). Si no,
                # un solo browser (con PROXY_URL opcional).
                browsers, page_queue = await launch_browser_pool(p, POOL_SIZE)

                # Sesion aiohttp compartida para Google Books (sin proxy — la
                # API es publica y no necesita rotacion IP).
                gb_session = None
                if GBOOKS_FALLBACK or GBOOKS_ENRICH_CDL:
                    gb_session = aiohttp.ClientSession()
                    print(f"[Enrich] Google Books cascade: fallback={GBOOKS_FALLBACK}, "
                          f"enrich_cdl={GBOOKS_ENRICH_CDL}")

                # Start all loops concurrently
                workers = [scrape_worker(i, page_queue, job, gb_session) for i in range(POOL_SIZE)]
                await asyncio.gather(
                    queue_refill_loop(odoo, job),
                    push_loop(odoo, job),
                    monitor_loop(job),
                    *workers,
                    return_exceptions=True,
                )

                await close_browser_pool(browsers)
                if gb_session:
                    try:
                        await gb_session.close()
                    except Exception:
                        pass
        if job["status"] == "running":
            job["status"] = "completed"
    except Exception as e:
        job["status"] = "error"
        err_msg = f"{type(e).__name__}: {e}"
        job["errors"].append(f"Fatal: {err_msg[:200]}")
        print(f"[Enrich] Fatal: {e}")
        # Avisar por Telegram que el job murio. Antes era silencioso → user
        # se quedo 3 dias sin notificaciones porque el job habia explotado.
        try:
            await notify.notify_alert("critical", "Enrichment job murio",
                                      f"Excepcion fatal: {err_msg[:300]}")
        except Exception:
            pass
    finally:
        # notify_job_stopped en finally — siempre se manda, incluso si el job
        # exploto con excepcion antes de llegar al codigo normal de cierre.
        try:
            await notify.notify_job_stopped(job.get("written", 0),
                                            reason=job.get("status", "stopped"))
        except Exception:
            pass

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


def retry_notfound_books(older_than_hours: int = 12, limit: int = 50000) -> int:
    """
    Mueve filas de notfound_books de vuelta a enrichment_queue (status=pending)
    para reintentar. Util tras un ban — muchos libros marcados notfound en
    realidad existian pero CDL los rechazo por throttle.

    older_than_hours: solo recuperar las marcadas hace mas de X horas (evita
                      reintentar libros que apenas fueron descartados).
    limit:            tope de filas a mover en una sola llamada.

    Retorna: cantidad de filas movidas.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    moved = 0
    try:
        if db.IS_POSTGRES:
            db.execute_query(cur, """
                WITH retrieved AS (
                    DELETE FROM notfound_books
                    WHERE odoo_id IN (
                        SELECT odoo_id FROM notfound_books
                        WHERE last_attempt < (CURRENT_TIMESTAMP - (? * INTERVAL '1 hour'))
                        ORDER BY last_attempt ASC
                        LIMIT ?
                    )
                    RETURNING odoo_id, barcode, name
                )
                INSERT INTO enrichment_queue (odoo_id, barcode, name, status, attempts, claimed_by)
                SELECT odoo_id, barcode, name, 'pending', 0, NULL
                FROM retrieved
                ON CONFLICT (odoo_id) DO UPDATE SET
                    status = 'pending', attempts = 0, claimed_by = NULL,
                    updated_at = CURRENT_TIMESTAMP
            """, (older_than_hours, limit))
            moved = cur.rowcount or 0
        else:
            # SQLite no tiene DELETE...RETURNING bien. Hacemos en 2 pasos.
            db.execute_query(cur, """
                SELECT odoo_id, barcode, name FROM notfound_books
                WHERE datetime(last_attempt) < datetime('now', '-' || ? || ' hours')
                ORDER BY last_attempt ASC LIMIT ?
            """, (older_than_hours, limit))
            rows = cur.fetchall()
            for r in rows:
                db.execute_query(cur, """
                    INSERT OR REPLACE INTO enrichment_queue
                    (odoo_id, barcode, name, status, attempts, claimed_by)
                    VALUES (?, ?, ?, 'pending', 0, NULL)
                """, (r[0], r[1], r[2]))
            ids = [r[0] for r in rows]
            if ids:
                ph = ",".join(["?"] * len(ids))
                db.execute_query(cur, f"DELETE FROM notfound_books WHERE odoo_id IN ({ph})", tuple(ids))
            moved = len(rows)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[Enrich] retry_notfound error: {e}")
        raise
    finally:
        conn.close()
    print(f"[Enrich] retry_notfound: {moved} filas movidas a pending")
    return moved


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
