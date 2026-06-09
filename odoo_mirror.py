"""
Espejo local de product.template desde Odoo a Postgres.

Pulla en lotes via JSON-RPC e inserta/upserta a odoo_books_mirror.
Job en background, idempotente (re-ejecutar es seguro — los registros
existentes se actualizan, los nuevos se insertan).

Default: solo libros con barcode y SIN description_sale (~727k).
Pasa only_pending=False para espejar TODOS (~1M).

Campos copiados: id, barcode, name, description, description_sale,
list_price, categ_id, categ_name.
"""
import asyncio
import os
from datetime import datetime
from typing import Any

import db
from odoo_client import OdooClient, OdooError

MIRROR_BATCH_SIZE = int(os.environ.get("MIRROR_BATCH_SIZE", "1000"))
MIRROR_FIELDS = [
    "id", "barcode", "name", "description",
    "description_sale", "list_price", "categ_id",
]

mirror_job: dict | None = None


# ── Job state ──────────────────────────────────────────────────────────
def _job_init(only_pending: bool, batch_size: int) -> dict:
    global mirror_job
    mirror_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "only_pending": only_pending,
        "batch_size": batch_size,
        "total_target": 0,
        "offset": 0,
        "mirrored": 0,
        "errors": [],
    }
    return mirror_job


def stop_mirror_job() -> bool:
    """Pide al job que pare en el proximo loop. Idempotente."""
    global mirror_job
    if mirror_job and mirror_job.get("status") == "running":
        mirror_job["status"] = "stopped"
        return True
    return False


def get_mirror_status() -> dict:
    """Snapshot del estado del job para la UI / poller."""
    job = dict(mirror_job) if mirror_job else {"status": "idle"}
    job["mirror_count_local"] = count_mirror_rows()
    if "errors" in job:
        job["errors"] = job["errors"][-5:]
    return job


def count_mirror_rows() -> int:
    """Cuantos libros tenemos espejados localmente."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, "SELECT COUNT(*) FROM odoo_books_mirror")
        return cur.fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


# ── Upsert ─────────────────────────────────────────────────────────────
def _upsert_batch(rows: list[dict]) -> int:
    """Inserta/actualiza un lote de filas. Devuelve cuantas se procesaron."""
    if not rows:
        return 0
    conn = db.get_connection()
    cur = conn.cursor()
    inserted = 0
    for r in rows:
        odoo_id = r.get("id")
        if not odoo_id:
            continue

        # categ_id en Odoo viene como [id, "name"] o False
        categ = r.get("categ_id")
        categ_id, categ_name = None, None
        if isinstance(categ, list) and len(categ) >= 2:
            categ_id = categ[0]
            categ_name = categ[1]

        barcode = r.get("barcode") or None
        name = r.get("name") or None
        # Odoo devuelve False para campos vacios — normalizar a None
        desc = r.get("description") if r.get("description") else None
        desc_sale = r.get("description_sale") if r.get("description_sale") else None
        list_price = r.get("list_price")
        if list_price in (False, None):
            list_price = None

        try:
            if db.IS_POSTGRES:
                db.execute_query(cur, """
                    INSERT INTO odoo_books_mirror
                        (odoo_id, barcode, name, description, description_sale,
                         list_price, categ_id, categ_name, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT (odoo_id) DO UPDATE SET
                        barcode = EXCLUDED.barcode,
                        name = EXCLUDED.name,
                        description = EXCLUDED.description,
                        description_sale = EXCLUDED.description_sale,
                        list_price = EXCLUDED.list_price,
                        categ_id = EXCLUDED.categ_id,
                        categ_name = EXCLUDED.categ_name,
                        synced_at = CURRENT_TIMESTAMP
                """, (odoo_id, barcode, name, desc, desc_sale,
                      list_price, categ_id, categ_name))
            else:
                db.execute_query(cur, """
                    INSERT OR REPLACE INTO odoo_books_mirror
                        (odoo_id, barcode, name, description, description_sale,
                         list_price, categ_id, categ_name, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (odoo_id, barcode, name, desc, desc_sale,
                      list_price, categ_id, categ_name))
            inserted += 1
        except Exception:
            # Un row malo no debe abortar el lote — Odoo a veces devuelve
            # registros con tipos raros.
            continue

    conn.commit()
    conn.close()
    return inserted


# ── Main job loop ──────────────────────────────────────────────────────
async def run_mirror_job(only_pending: bool = True,
                         batch_size: int = MIRROR_BATCH_SIZE):
    """
    Loop principal: pulla en lotes via search_read, upserta, repite hasta
    agotar. Si Odoo da error en un lote, espera 30s y reintenta.
    """
    global mirror_job
    if mirror_job and mirror_job.get("status") == "running":
        raise RuntimeError("Mirror job already running")

    job = _job_init(only_pending, batch_size)
    domain = [["barcode", "!=", False]]
    if only_pending:
        domain.append(["description_sale", "=", False])

    print(f"[Mirror] Arrancado — only_pending={only_pending}, batch={batch_size}")

    try:
        async with OdooClient() as odoo:
            try:
                total = await odoo.search_count("product.template", domain)
                job["total_target"] = total
                print(f"[Mirror] Target: {total} libros")
            except Exception as e:
                err = f"{type(e).__name__}: {e!r}"
                job["errors"].append(f"count: {err[:200]}")
                print(f"[Mirror] No pude contar target: {err}")

            offset = 0
            target = job["total_target"]
            while job["status"] == "running":
                if target and offset >= target:
                    break
                try:
                    rows = await odoo.search_read(
                        "product.template", domain, MIRROR_FIELDS,
                        offset=offset, limit=batch_size, order="id",
                    )
                except Exception as e:
                    err = f"{type(e).__name__}: {e!r}"
                    job["errors"].append(f"fetch@{offset}: {err[:200]}")
                    print(f"[Mirror] Fetch error @ {offset}: {err}")
                    await asyncio.sleep(30)
                    continue

                if not rows:
                    print(f"[Mirror] Odoo devolvio 0 filas @ offset {offset} — fin")
                    break

                inserted = _upsert_batch(rows)
                job["mirrored"] += inserted
                offset += len(rows)
                job["offset"] = offset
                pct = (offset / target * 100) if target else 0
                print(f"[Mirror] {offset}/{target} ({pct:.1f}%) — "
                      f"total espejados: {job['mirrored']}")

            if job["status"] == "running":
                job["status"] = "completed"
                print(f"[Mirror] COMPLETED: {job['mirrored']} libros espejados")
            else:
                print(f"[Mirror] STOPPED: {job['mirrored']} libros espejados")
    except Exception as e:
        job["status"] = "error"
        err = f"{type(e).__name__}: {e!r}"
        job["errors"].append(f"fatal: {err[:200]}")
        print(f"[Mirror] Fatal: {err}")


# ── CSV streaming export ───────────────────────────────────────────────
def export_csv_streaming():
    """
    Generator que yields chunks de CSV. Usar con StreamingResponse de FastAPI.
    Streamea sin cargar la tabla entera a memoria.
    """
    import csv
    import io

    conn = db.get_connection()
    cur = conn.cursor()
    db.execute_query(cur, """
        SELECT odoo_id, barcode, name, description_sale,
               list_price, categ_id, categ_name, synced_at
        FROM odoo_books_mirror
        ORDER BY odoo_id
    """)

    headers = ["odoo_id", "barcode", "name", "description_sale",
               "list_price", "categ_id", "categ_name", "synced_at"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate()

    try:
        while True:
            rows = cur.fetchmany(1000)
            if not rows:
                break
            for r in rows:
                w.writerow(r)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    finally:
        conn.close()
