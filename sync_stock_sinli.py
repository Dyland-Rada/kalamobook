"""
Sync stock/precio SINLI → Odoo.

Lee libros_proveedor (los 11 proveedores SINLI, NO AZETA) y escribe en Odoo:
- stock.quant.inventory_quantity en el warehouse del proveedor
- product.template.list_price (SOLO si precio_con_iva difiere del actual)

Anti-colisión:
- Filtro obligatorio en query: proveedor_email != 'info@azetadistribuciones.es'
- AZETA y location_id=14 (AZE01) son del scraper, no de este sync
- No toca description/categorías/dimensiones (eso es del scraper)

Lock + marcapáginas en sync_state[entidad='libros_proveedor_to_odoo'].
Errores van a sync_errores.

Modos:
- run_once(): un lote (2000 libros) — para cron y validación
- run_backlog(): bucle hasta vaciar — para arranque inicial (~104k libros)
- Cron 1h interno (task asyncio, sin APScheduler)

Workaround Fault Odoo v19 SaaS: action_apply_inventory ejecuta pero
serializa None — try/except + verificar releyendo quant.quantity.
"""
import asyncio
import json
import os
import time
from datetime import datetime
from typing import Any

import db
from odoo_client import OdooClient, OdooError

ENTIDAD = "libros_proveedor_to_odoo"
AZETA_EMAIL = "info@azetadistribuciones.es"
BATCH_SIZE = 2000
CRON_INTERVAL_S = int(os.environ.get("SYNC_STOCK_CRON_INTERVAL_S", "3600"))
PUSH_CONCURRENCY = int(os.environ.get("SYNC_STOCK_CONCURRENCY", "8"))

# Estado en memoria
sync_job: dict | None = None
_cron_task: asyncio.Task | None = None
_cron_state: dict = {
    "enabled": False,
    "interval_s": CRON_INTERVAL_S,
    "last_run_at": None,
    "last_run_status": None,
    "last_summary": None,
    "next_run_at": None,
    "runs_total": 0,
    "errors": [],
}


def get_status() -> dict:
    job = dict(sync_job) if sync_job else {"status": "idle"}
    if "errors" in job:
        job["errors"] = job["errors"][-15:]
    return job


def stop():
    global sync_job
    if sync_job and sync_job.get("status") == "running":
        sync_job["status"] = "stopped"
        return True
    return False


def get_cron_status() -> dict:
    out = dict(_cron_state)
    if "errors" in out:
        out["errors"] = out["errors"][-10:]
    out["task_running"] = bool(_cron_task and not _cron_task.done())
    return out


# ─── Lock + marcapáginas ──────────────────────────────────────────────
def _acquire_lock() -> bool:
    """Adquiere lock atómicamente. Devuelve False si ya estaba locked."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            UPDATE sync_state
            SET lock_activo = true, lock_desde = NOW()
            WHERE entidad = ? AND lock_activo = false
        """, (ENTIDAD,))
        affected = cur.rowcount
        conn.commit()
        return affected > 0
    except Exception as e:
        print(f"[SinliSync] acquire_lock FAIL: {e}")
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        conn.close()


def _release_lock():
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            UPDATE sync_state SET lock_activo = false
            WHERE entidad = ?
        """, (ENTIDAD,))
        conn.commit()
    except Exception as e:
        print(f"[SinliSync] release_lock FAIL: {e}")
        try: conn.rollback()
        except Exception: pass
    finally:
        conn.close()


def _get_marker():
    """
    Lee ultimo_timestamp y lo devuelve siempre como naive datetime para
    que compare bien con libros_proveedor.actualizado_en (timestamp sin tz).
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur,
            "SELECT ultimo_timestamp FROM sync_state WHERE entidad = ?",
            (ENTIDAD,))
        r = cur.fetchone()
        if not r or r[0] is None:
            return None
        ts = r[0]
        # sync_state.ultimo_timestamp es timestamptz (aware), pero
        # libros_proveedor.actualizado_en es timestamp sin tz (naive).
        # Para comparar en Python: convertir aware -> naive (UTC).
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            from datetime import timezone
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        return ts
    finally:
        conn.close()


def _advance_marker(new_ts, items_count: int, ok: bool):
    """Avanza el marcapáginas. Solo si ok=True; si no, libera lock sin avanzar."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        if ok:
            db.execute_query(cur, """
                UPDATE sync_state
                SET ultimo_timestamp = ?,
                    ultima_ejecucion = NOW(),
                    ultima_ejecucion_ok = true,
                    items_procesados = ?,
                    lock_activo = false
                WHERE entidad = ?
            """, (new_ts, items_count, ENTIDAD))
        else:
            db.execute_query(cur, """
                UPDATE sync_state
                SET ultima_ejecucion = NOW(),
                    ultima_ejecucion_ok = false,
                    items_procesados = ?,
                    lock_activo = false
                WHERE entidad = ?
            """, (items_count, ENTIDAD))
        conn.commit()
    except Exception as e:
        print(f"[SinliSync] advance_marker FAIL: {e}")
        try: conn.rollback()
        except Exception: pass
    finally:
        conn.close()


def _set_marker_to_now():
    """Setea ultimo_timestamp = NOW(). Usar al terminar backlog inicial."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            UPDATE sync_state
            SET ultimo_timestamp = NOW()
            WHERE entidad = ?
        """, (ENTIDAD,))
        conn.commit()
    finally:
        conn.close()


# ─── Caches ────────────────────────────────────────────────────────────
def _load_provider_warehouse_map() -> dict[str, str]:
    """Devuelve {proveedor_email: warehouse_code} desde proveedor_almacen_odoo."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur,
            "SELECT proveedor_email, warehouse_code FROM proveedor_almacen_odoo")
        return {r[0]: r[1] for r in cur.fetchall() if r[0] and r[1]}
    finally:
        conn.close()


async def _load_warehouse_locations(odoo: OdooClient) -> dict[str, int]:
    """Devuelve {warehouse_code: lot_stock_id} pulleando stock.warehouse."""
    rows = await odoo.search_read(
        "stock.warehouse", [], ["code", "lot_stock_id"],
    )
    out: dict[str, int] = {}
    for r in rows:
        code = r.get("code")
        lot = r.get("lot_stock_id")
        if not code or not lot:
            continue
        # lot_stock_id en Odoo viene como [id, name]
        lot_id = lot[0] if isinstance(lot, list) else lot
        out[code] = int(lot_id)
    return out


# ─── Errores ──────────────────────────────────────────────────────────
def _record_error(isbn: str, proveedor: str, mensaje: str, payload: dict):
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        if db.IS_POSTGRES:
            db.execute_query(cur, """
                INSERT INTO sync_errores
                    (entidad, isbn, proveedor_email, payload,
                     mensaje_error, intentos, resuelto, creado_en, actualizado_en)
                VALUES (?, ?, ?, ?::jsonb, ?, 1, false, NOW(), NOW())
            """, (ENTIDAD, isbn, proveedor, json.dumps(payload), mensaje[:1000]))
        else:
            db.execute_query(cur, """
                INSERT INTO sync_errores
                    (entidad, isbn, proveedor_email, payload,
                     mensaje_error, intentos, resuelto, creado_en, actualizado_en)
                VALUES (?, ?, ?, ?, ?, 1, false,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (ENTIDAD, isbn, proveedor, json.dumps(payload), mensaje[:1000]))
        conn.commit()
    except Exception as e:
        print(f"[SinliSync] record_error FAIL: {e}")
        try: conn.rollback()
        except Exception: pass
    finally:
        conn.close()


# ─── Query del lote ───────────────────────────────────────────────────
def _fetch_batch(marker, limit: int = BATCH_SIZE,
                 solo_proveedor: str | None = None) -> list[dict]:
    """
    Lote de libros NO-AZETA cambiados después del marcador.
    JOIN con mirror = solo libros que ya existen en Odoo (grupo A).
    solo_proveedor: si se pasa, filtra por ese proveedor_email específico.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        if solo_proveedor:
            db.execute_query(cur, """
                SELECT lp.isbn, lp.proveedor_email, lp.stock_disponible,
                       lp.precio_con_iva, lp.actualizado_en,
                       m.odoo_id, m.list_price
                FROM libros_proveedor lp
                JOIN odoo_books_mirror m ON m.barcode = lp.isbn
                WHERE lp.actualizado_en > ?
                  AND lp.proveedor_email = ?
                  AND lp.proveedor_email != ?
                  AND lp.isbn IS NOT NULL
                  AND m.odoo_id IS NOT NULL
                ORDER BY lp.actualizado_en
                LIMIT ?
            """, (marker, solo_proveedor, AZETA_EMAIL, limit))
        else:
            db.execute_query(cur, """
                SELECT lp.isbn, lp.proveedor_email, lp.stock_disponible,
                       lp.precio_con_iva, lp.actualizado_en,
                       m.odoo_id, m.list_price
                FROM libros_proveedor lp
                JOIN odoo_books_mirror m ON m.barcode = lp.isbn
                WHERE lp.actualizado_en > ?
                  AND lp.proveedor_email != ?
                  AND lp.isbn IS NOT NULL
                  AND m.odoo_id IS NOT NULL
                ORDER BY lp.actualizado_en
                LIMIT ?
            """, (marker, AZETA_EMAIL, limit))
        rows = cur.fetchall()
    finally:
        conn.close()

    return [{
        "isbn": r[0],
        "proveedor_email": r[1],
        "stock_disponible": int(r[2] or 0),
        "precio_con_iva": float(r[3]) if r[3] is not None else None,
        "actualizado_en": r[4],
        "odoo_id": r[5],
        "list_price_mirror": float(r[6]) if r[6] is not None else None,
    } for r in rows]


# ─── Batch helpers ────────────────────────────────────────────────────
SEARCH_CHUNK = int(os.environ.get("SYNC_STOCK_SEARCH_CHUNK", "1000"))
WRITE_CHUNK  = int(os.environ.get("SYNC_STOCK_WRITE_CHUNK", "500"))
CREATE_CHUNK = int(os.environ.get("SYNC_STOCK_CREATE_CHUNK", "200"))


def _chunks(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


async def _resolve_product_ids_batch(odoo: OdooClient,
                                      template_ids: list[int]) -> dict[int, int]:
    """{template_id: product.product.id}, batch search."""
    out: dict[int, int] = {}
    unique = list(set(template_ids))
    for chunk in _chunks(unique, SEARCH_CHUNK):
        rows = await odoo.search_read(
            "product.product",
            [["product_tmpl_id", "in", chunk]],
            ["id", "product_tmpl_id"],
        )
        for r in rows:
            tmpl = r.get("product_tmpl_id")
            tmpl_id = tmpl[0] if isinstance(tmpl, list) else tmpl
            if tmpl_id not in out:
                out[tmpl_id] = r["id"]
    return out


async def _resolve_quants_in_locations(odoo: OdooClient,
                                        product_ids: list[int],
                                        location_ids: list[int]) -> dict[tuple[int, int], int]:
    """
    {(product_id, location_id): quant_id} para los que YA existen.
    1 search_read por chunk con [product_id IN ...] y [location_id IN ...].
    """
    out: dict[tuple[int, int], int] = {}
    if not product_ids or not location_ids:
        return out
    unique_pids = list(set(product_ids))
    for chunk in _chunks(unique_pids, SEARCH_CHUNK):
        rows = await odoo.search_read(
            "stock.quant",
            [["product_id", "in", chunk],
             ["location_id", "in", location_ids]],
            ["id", "product_id", "location_id"],
        )
        for r in rows:
            pid = r.get("product_id")
            loc = r.get("location_id")
            pid_v = pid[0] if isinstance(pid, list) else pid
            loc_v = loc[0] if isinstance(loc, list) else loc
            key = (pid_v, loc_v)
            if key not in out:
                out[key] = r["id"]
    return out


# ─── Orchestrators ────────────────────────────────────────────────────
def _new_job(mode: str, concurrency: int = PUSH_CONCURRENCY,
             solo_proveedor: str | None = None,
             max_books: int | None = None) -> dict:
    return {
        "status": "running",
        "mode": mode,
        "concurrency": concurrency,
        "solo_proveedor": solo_proveedor,
        "max_books": max_books,
        "started_at": datetime.now().isoformat(),
        "stage": "starting",
        "items_total_estimate": 0,
        "items_processed": 0,
        "stock_written": 0,
        "price_written": 0,
        "err_no_product": 0,
        "err_apply": 0,
        "err_price": 0,
        "err_other": 0,
        "err_no_warehouse": 0,
        "batches_done": 0,
        "elapsed_s": 0,
        "errors": [],
    }


async def _run_one_batch(odoo: OdooClient,
                         prov_to_wh: dict[str, str],
                         wh_to_loc: dict[str, int],
                         pid_cache: dict[int, int],
                         job: dict,
                         solo_proveedor: str | None = None,
                         limit_override: int | None = None) -> tuple[int, Any]:
    """
    Procesa un lote en batch (multi-location).
    Estrategia: resolver pids batch, agrupar libros por (location, qty),
    write masivos por grupo, action_apply en lotes.
    Devuelve (n_procesados, max_actualizado_en).
    """
    marker = _get_marker()
    limit = limit_override or BATCH_SIZE
    job["stage"] = f"fetching (marker={marker})"
    batch = _fetch_batch(marker, limit, solo_proveedor=solo_proveedor)
    if not batch:
        return 0, marker

    job["stage"] = f"batch pushing ({len(batch)} libros)"
    max_ts = marker

    # ── 1. Validar warehouse/location de cada libro ──────────────────
    valid_books: list[dict] = []
    for book in batch:
        if book["actualizado_en"] and (max_ts is None or book["actualizado_en"] > max_ts):
            max_ts = book["actualizado_en"]

        wh_code = prov_to_wh.get(book["proveedor_email"])
        if not wh_code:
            _record_error(book["isbn"], book["proveedor_email"],
                          "Proveedor sin mapping en proveedor_almacen_odoo",
                          {"book": book})
            job["err_no_warehouse"] += 1
            job["items_processed"] += 1
            continue
        location_id = wh_to_loc.get(wh_code)
        if not location_id:
            _record_error(book["isbn"], book["proveedor_email"],
                          f"Warehouse {wh_code} sin lot_stock_id en Odoo",
                          {"wh": wh_code})
            job["err_no_warehouse"] += 1
            job["items_processed"] += 1
            continue
        # Anti-colisión: NUNCA escribir en location 14 (AZE01)
        if location_id == 14:
            _record_error(book["isbn"], book["proveedor_email"],
                          "Anti-colision: location 14 (AZE01) es del scraper",
                          {"wh": wh_code, "location_id": location_id})
            job["err_no_warehouse"] += 1
            job["items_processed"] += 1
            continue
        book["location_id"] = location_id
        valid_books.append(book)

    if not valid_books:
        return len(batch), max_ts

    # ── 2. Resolver pids batch ──────────────────────────────────────
    template_ids_needed = [b["odoo_id"] for b in valid_books
                            if b["odoo_id"] not in pid_cache]
    if template_ids_needed:
        new_pids = await _resolve_product_ids_batch(odoo, template_ids_needed)
        pid_cache.update(new_pids)
    print(f"[SinliBatch] pids resueltos: {len(pid_cache):,}")

    # ── 3. Resolver quants existentes en las locations del lote ─────
    location_ids = list(set(b["location_id"] for b in valid_books))
    pids_needed = [pid_cache[b["odoo_id"]] for b in valid_books
                    if pid_cache.get(b["odoo_id"])]
    quant_cache = await _resolve_quants_in_locations(
        odoo, pids_needed, location_ids
    )
    print(f"[SinliBatch] quants existentes: {len(quant_cache):,} "
          f"en {len(location_ids)} locations")

    # ── 4. Particionar: create vs update por (location, qty) ────────
    to_create: list[dict] = []
    to_update_by_loc_qty: dict[tuple[int, int], list[int]] = {}
    # Precio: agrupar por precio (1 write por valor único)
    prices_by_value: dict[float, list[int]] = {}

    for book in valid_books:
        pid = pid_cache.get(book["odoo_id"])
        if not pid:
            _record_error(book["isbn"], book["proveedor_email"],
                          "product.product no encontrado",
                          {"template_id": book["odoo_id"]})
            job["err_no_product"] += 1
            job["items_processed"] += 1
            continue

        loc_id = book["location_id"]
        qty = book["stock_disponible"]
        key = (pid, loc_id)
        if key in quant_cache:
            to_update_by_loc_qty.setdefault((loc_id, qty), []).append(quant_cache[key])
        else:
            to_create.append({
                "product_id": pid,
                "location_id": loc_id,
                "inventory_quantity": qty,
            })

        # Precio condicional
        price = book["precio_con_iva"]
        list_price_now = book["list_price_mirror"]
        if price is not None and (
            list_price_now is None or abs(price - list_price_now) > 0.001
        ):
            prices_by_value.setdefault(price, []).append(book["odoo_id"])

        job["items_processed"] += 1

    print(f"[SinliBatch] particion: {len(to_create):,} crear, "
          f"{sum(len(v) for v in to_update_by_loc_qty.values()):,} update "
          f"({len(to_update_by_loc_qty)} grupos), "
          f"{sum(len(v) for v in prices_by_value.values()):,} precios "
          f"({len(prices_by_value)} valores únicos)")

    # ── 5. CREATE en lotes ──────────────────────────────────────────
    new_quant_ids: list[int] = []
    for chunk in _chunks(to_create, CREATE_CHUNK):
        try:
            res = await odoo.execute_kw("stock.quant", "create", [chunk])
            if isinstance(res, list):
                new_quant_ids.extend(res)
            elif isinstance(res, int):
                new_quant_ids.append(res)
            job["stock_written"] += len(chunk)
        except Exception as e:
            job["err_apply"] += len(chunk)
            err = f"create chunk: {type(e).__name__}: {str(e)[:150]}"
            job["errors"].append(err)
            print(f"[SinliBatch] {err}")

    # ── 6. UPDATE quants por (location, qty) ─────────────────────────
    for (loc_id, qty), quant_ids in to_update_by_loc_qty.items():
        for chunk in _chunks(quant_ids, WRITE_CHUNK):
            try:
                await odoo.write("stock.quant", chunk,
                                 {"inventory_quantity": qty})
                job["stock_written"] += len(chunk)
            except Exception as e:
                job["err_apply"] += len(chunk)
                err = f"update loc={loc_id} qty={qty}: {type(e).__name__}: {str(e)[:150]}"
                job["errors"].append(err)
                print(f"[SinliBatch] {err}")

    # ── 7. APPLY en lotes ────────────────────────────────────────────
    all_quant_ids = new_quant_ids + [
        qid for qids in to_update_by_loc_qty.values() for qid in qids
    ]
    for chunk in _chunks(all_quant_ids, WRITE_CHUNK):
        try:
            await odoo.execute_kw(
                "stock.quant", "action_apply_inventory", [chunk]
            )
        except Exception as apply_err:
            # Workaround Fault: verificar releyendo
            try:
                verify = await odoo.read(
                    "stock.quant", chunk, ["quantity", "inventory_quantity"]
                )
                ok = all(
                    abs(float(v.get("quantity") or 0)
                        - float(v.get("inventory_quantity") or 0)) < 0.01
                    for v in verify
                )
                if not ok:
                    job["err_apply"] += 1
                    err = f"apply chunk: {type(apply_err).__name__}: {str(apply_err)[:150]}"
                    job["errors"].append(err)
            except Exception as verify_err:
                job["err_apply"] += 1
                err = f"verify chunk: {type(verify_err).__name__}: {str(verify_err)[:150]}"
                job["errors"].append(err)

    # ── 8. PRECIO: write por valor único ────────────────────────────
    for price, tmpl_ids in prices_by_value.items():
        for chunk in _chunks(tmpl_ids, WRITE_CHUNK):
            try:
                await odoo.write("product.template", chunk,
                                 {"list_price": price})
                job["price_written"] += len(chunk)
            except Exception as e:
                job["err_price"] += len(chunk)
                err = f"price={price}: {type(e).__name__}: {str(e)[:150]}"
                job["errors"].append(err)

    print(f"[SinliBatch] DONE batch: stk={job['stock_written']:,} "
          f"prc={job['price_written']:,} err_apply={job['err_apply']} "
          f"err_price={job['err_price']} err_wh={job['err_no_warehouse']} "
          f"err_np={job['err_no_product']}")

    return len(batch), max_ts


async def run_once(loop_until_empty: bool = False,
                    solo_proveedor: str | None = None,
                    max_books: int | None = None,
                    concurrency: int | None = None) -> dict:
    """
    Modo run-once: un lote.
    Si loop_until_empty=True, sigue hasta vaciar (modo backlog).
    solo_proveedor: filtra por proveedor_email (e.g. 'sinli.icaro@zonalibros.com')
    max_books: tope total de libros (None = sin tope, BATCH_SIZE por lote)
    concurrency: workers en paralelo (default PUSH_CONCURRENCY=8)
    """
    global sync_job
    conc = concurrency or PUSH_CONCURRENCY
    mode = "backlog" if loop_until_empty else "once"
    if solo_proveedor or max_books:
        mode = "test" if max_books and max_books <= 50 else mode
    sync_job = _new_job(mode, conc, solo_proveedor, max_books)
    job = sync_job
    t_start = time.monotonic()

    if not _acquire_lock():
        job["status"] = "error"
        job["errors"].append("No se pudo adquirir lock — ya hay otra ejecucion.")
        return job

    try:
        job["stage"] = "loading_caches"
        prov_to_wh = _load_provider_warehouse_map()
        pid_cache: dict[int, int] = {}

        # Estimación de pendientes (para mostrar progreso)
        marker = _get_marker()
        conn = db.get_connection()
        cur = conn.cursor()
        try:
            if solo_proveedor:
                db.execute_query(cur, """
                    SELECT COUNT(*) FROM libros_proveedor lp
                    JOIN odoo_books_mirror m ON m.barcode = lp.isbn
                    WHERE lp.actualizado_en > ?
                      AND lp.proveedor_email = ?
                      AND lp.proveedor_email != ?
                      AND lp.isbn IS NOT NULL
                      AND m.odoo_id IS NOT NULL
                """, (marker, solo_proveedor, AZETA_EMAIL))
            else:
                db.execute_query(cur, """
                    SELECT COUNT(*) FROM libros_proveedor lp
                    JOIN odoo_books_mirror m ON m.barcode = lp.isbn
                    WHERE lp.actualizado_en > ?
                      AND lp.proveedor_email != ?
                      AND lp.isbn IS NOT NULL
                      AND m.odoo_id IS NOT NULL
                """, (marker, AZETA_EMAIL))
            est = int(cur.fetchone()[0])
            if max_books:
                est = min(est, max_books)
            job["items_total_estimate"] = est
        finally:
            conn.close()
        print(f"[SinliSync] {job['items_total_estimate']:,} pendientes "
              f"desde marker={marker} prov={solo_proveedor or 'ALL'} "
              f"conc={conc}")

        async with OdooClient() as odoo:
            job["stage"] = "loading_warehouses"
            wh_to_loc = await _load_warehouse_locations(odoo)
            print(f"[SinliSync] Warehouses cargados: {wh_to_loc}")

            while job["status"] == "running":
                # Limitar el batch al max_books restante si aplica
                remaining = (max_books - job["items_processed"]) if max_books else None
                if remaining is not None and remaining <= 0:
                    break
                batch_limit = min(BATCH_SIZE, remaining) if remaining else BATCH_SIZE

                n, max_ts = await _run_one_batch(
                    odoo, prov_to_wh, wh_to_loc, pid_cache, job,
                    solo_proveedor=solo_proveedor,
                    limit_override=batch_limit,
                )
                if n == 0:
                    break

                job["batches_done"] += 1
                # Avanzar marcapáginas con el max_ts del lote (ok=True).
                # Importante: si solo_proveedor o max_books están activos
                # (modo prueba), NO avanzamos marker — solo es validación.
                if (max_ts and job["status"] == "running"
                        and not solo_proveedor and not max_books):
                    _advance_marker(max_ts, job["items_processed"], ok=True)
                    if not _acquire_lock():
                        job["errors"].append("Lock perdido durante el bucle.")
                        break

                if not loop_until_empty:
                    break

        if job["status"] == "running":
            job["status"] = "completed"
    except Exception as e:
        job["status"] = "error"
        err = f"{type(e).__name__}: {e!r}"
        job["errors"].append(err[:300])
        print(f"[SinliSync] Fatal: {err}")
    finally:
        # En caso de error o stop antes del primer advance_marker, liberar lock
        _release_lock()
        job["elapsed_s"] = round(time.monotonic() - t_start, 2)
        job["stage"] = "done"
        print(f"[SinliSync] DONE status={job['status']} "
              f"proc={job['items_processed']:,} stk={job['stock_written']:,} "
              f"prc={job['price_written']:,} en {job['elapsed_s']}s")

    return job


# ─── Cron 1h ──────────────────────────────────────────────────────────
async def _cron_loop():
    print(f"[SinliCron] Arrancado, intervalo {_cron_state['interval_s']}s")
    while _cron_state["enabled"]:
        try:
            t0 = time.monotonic()
            res = await run_once(loop_until_empty=False)
            elapsed = round(time.monotonic() - t0, 1)
            _cron_state["last_run_at"] = datetime.now().isoformat()
            _cron_state["last_run_status"] = res.get("status")
            _cron_state["last_summary"] = {
                "items_processed": res.get("items_processed", 0),
                "stock_written": res.get("stock_written", 0),
                "price_written": res.get("price_written", 0),
                "errors_total": (
                    res.get("err_no_product", 0) + res.get("err_apply", 0)
                    + res.get("err_price", 0) + res.get("err_other", 0)
                    + res.get("err_no_warehouse", 0)
                ),
                "elapsed_s": res.get("elapsed_s", 0),
            }
            _cron_state["runs_total"] += 1
            print(f"[SinliCron] Run #{_cron_state['runs_total']} OK en {elapsed}s")
        except Exception as e:
            _cron_state["last_run_status"] = "error"
            err = f"{type(e).__name__}: {e!r}"
            _cron_state["errors"].append(err[:300])
            print(f"[SinliCron] Fatal: {err}")

        from datetime import timedelta
        _cron_state["next_run_at"] = (
            datetime.now() + timedelta(seconds=_cron_state["interval_s"])
        ).isoformat()
        for _ in range(_cron_state["interval_s"]):
            if not _cron_state["enabled"]:
                break
            await asyncio.sleep(1)

    print("[SinliCron] Detenido")
    _cron_state["next_run_at"] = None


def start_cron() -> bool:
    global _cron_task
    if _cron_task and not _cron_task.done():
        return False
    _cron_state["enabled"] = True
    _cron_state["errors"] = []
    try:
        _cron_task = asyncio.create_task(_cron_loop())
        return True
    except RuntimeError:
        _cron_state["enabled"] = False
        return False


def stop_cron() -> bool:
    if not _cron_state["enabled"]:
        return False
    _cron_state["enabled"] = False
    return True


# ─── Helpers para UI ──────────────────────────────────────────────────
def get_recent_errors(limit: int = 20) -> list[dict]:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT isbn, proveedor_email, mensaje_error, intentos, creado_en
            FROM sync_errores
            WHERE entidad = ?
            ORDER BY creado_en DESC
            LIMIT ?
        """, (ENTIDAD, limit))
        return [{
            "isbn": r[0], "proveedor": r[1], "mensaje": r[2],
            "intentos": r[3], "creado": str(r[4]),
        } for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def get_marker_info() -> dict:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT ultimo_timestamp, ultima_ejecucion,
                   ultima_ejecucion_ok, items_procesados,
                   lock_activo, lock_desde
            FROM sync_state WHERE entidad = ?
        """, (ENTIDAD,))
        r = cur.fetchone()
        if not r:
            return {}
        return {
            "ultimo_timestamp": str(r[0]),
            "ultima_ejecucion": str(r[1]) if r[1] else None,
            "ultima_ejecucion_ok": r[2],
            "items_procesados": r[3],
            "lock_activo": r[4],
            "lock_desde": str(r[5]) if r[5] else None,
        }
    except Exception:
        return {}
    finally:
        conn.close()


def get_pending_count() -> int:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        marker = _get_marker()
        db.execute_query(cur, """
            SELECT COUNT(*) FROM libros_proveedor lp
            JOIN odoo_books_mirror m ON m.barcode = lp.isbn
            WHERE lp.actualizado_en > ?
              AND lp.proveedor_email != ?
              AND lp.isbn IS NOT NULL
              AND m.odoo_id IS NOT NULL
        """, (marker, AZETA_EMAIL))
        return int(cur.fetchone()[0])
    except Exception:
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    # python sync_stock_sinli.py once     -> un lote
    # python sync_stock_sinli.py backlog  -> bucle hasta vaciar
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    asyncio.run(run_once(loop_until_empty=(mode == "backlog")))