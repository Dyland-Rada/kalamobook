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
import pricing_engine
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
# Un lock mas viejo que esto se considera huerfano (proceso muerto a
# mitad de run, p.ej. por un re-deploy) y se roba automaticamente.
LOCK_TTL_MIN = int(os.environ.get("SYNC_STOCK_LOCK_TTL_MIN", "30"))


def _acquire_lock() -> bool:
    """
    Adquiere lock atomicamente. Devuelve False si ya esta tomado por un
    proceso vivo. Los locks huerfanos (mas viejos que LOCK_TTL_MIN) se
    roban: sin esto, un deploy que mata un run a mitad deja el sync
    bloqueado para siempre en silencio (paso el 14/07: lock huerfano
    2 dias, 21k libros sin sincronizar y n8n devolviendo 'started').
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            UPDATE sync_state
            SET lock_activo = true, lock_desde = NOW()
            WHERE entidad = ?
              AND (lock_activo = false
                   OR lock_desde < NOW() - INTERVAL '{LOCK_TTL_MIN} minutes')
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


def _load_pausados() -> set[str]:
    """
    Proveedores pausados (activo=false). Sus libros se saltan sin registrar
    error: es una pausa querida, no un fallo de configuracion.
    """
    try:
        import proveedores_admin
        return proveedores_admin.pausados()
    except Exception as e:
        print(f"[SinliSync] no pude leer pausados: {e}")
        return set()


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
                       m.odoo_id, m.list_price, m.pvp_base
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
                       m.odoo_id, m.list_price, m.pvp_base
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

    # FIX marker-skip: los inserts masivos del n8n comparten actualizado_en
    # exacto (miles de filas con el mismo timestamp). Si el LIMIT corta a
    # mitad de un grupo de timestamps identicos, el proximo fetch con
    # "> marker" saltaria las filas restantes de ese timestamp (perdida
    # silenciosa: paso el 16/07 con DISBOOK/DISTRIFER, 17k saltados).
    # Solucion: extender el lote con TODAS las filas que compartan el
    # timestamp de la ultima fila.
    if rows and len(rows) >= limit:
        last_ts = rows[-1][4]
        seen_isbn_prov = {(r[0], r[1]) for r in rows}
        conn = db.get_connection()
        cur = conn.cursor()
        try:
            if solo_proveedor:
                db.execute_query(cur, """
                    SELECT lp.isbn, lp.proveedor_email, lp.stock_disponible,
                           lp.precio_con_iva, lp.actualizado_en,
                           m.odoo_id, m.list_price, m.pvp_base
                    FROM libros_proveedor lp
                    JOIN odoo_books_mirror m ON m.barcode = lp.isbn
                    WHERE lp.actualizado_en = ?
                      AND lp.proveedor_email = ?
                      AND lp.isbn IS NOT NULL AND m.odoo_id IS NOT NULL
                """, (last_ts, solo_proveedor))
            else:
                db.execute_query(cur, """
                    SELECT lp.isbn, lp.proveedor_email, lp.stock_disponible,
                           lp.precio_con_iva, lp.actualizado_en,
                           m.odoo_id, m.list_price, m.pvp_base
                    FROM libros_proveedor lp
                    JOIN odoo_books_mirror m ON m.barcode = lp.isbn
                    WHERE lp.actualizado_en = ?
                      AND lp.proveedor_email != ?
                      AND lp.isbn IS NOT NULL AND m.odoo_id IS NOT NULL
                """, (last_ts, AZETA_EMAIL))
            extra = [r for r in cur.fetchall()
                     if (r[0], r[1]) not in seen_isbn_prov]
            if extra:
                print(f"[SinliSync] lote extendido +{len(extra):,} filas "
                      f"con mismo timestamp de frontera")
                rows = list(rows) + extra
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
        "pvp_base": float(r[7]) if r[7] is not None else None,
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
        "skipped_pausados": 0,
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
                         limit_override: int | None = None,
                         pausados: set[str] | None = None) -> tuple[int, Any]:
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
    pausados = pausados or set()
    valid_books: list[dict] = []
    for book in batch:
        if book["actualizado_en"] and (max_ts is None or book["actualizado_en"] > max_ts):
            max_ts = book["actualizado_en"]

        # Proveedor pausado: ni stock ni precio. Su almacen quedo a 0 al
        # pausarlo y asi sigue hasta que lo reactiven.
        if book["proveedor_email"] in pausados:
            job["skipped_pausados"] += 1
            job["items_processed"] += 1
            continue

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
    # Precio: agrupar por PRECIO WEB (pvp_base + suplemento API-15).
    prices_by_value: dict[float, list[int]] = {}
    to_deactivate: list[int] = []        # PVP < 2,90 -> apagar
    pvp_base_updates: list[tuple[int, float]] = []  # (odoo_id, pvp crudo)

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
        # No empujar stock a productos que la regla API-15 apaga (< 2,90 o
        # sin precio): el producto esta archivado y la escritura fallaria.
        # El bloque de precio de abajo SI corre (deactiva/reactiva + pvp_base).
        if pricing_engine.web_price(book["precio_con_iva"]) is not None:
            key = (pid, loc_id)
            if key in quant_cache:
                to_update_by_loc_qty.setdefault((loc_id, qty), []).append(quant_cache[key])
            else:
                to_create.append({
                    "product_id": pid,
                    "location_id": loc_id,
                    "inventory_quantity": qty,
                })

        # Precio condicional (motor API-15): comparamos el PVP crudo con
        # pvp_base (no con list_price, que ya es el precio web con suplemento).
        # Si el PVP cambió, recalculamos precio web y actualizamos pvp_base.
        price = book["precio_con_iva"]
        pvp_base_now = book["pvp_base"]
        if price is not None and (
            pvp_base_now is None or abs(price - pvp_base_now) > 0.001
        ):
            wp = pricing_engine.web_price(price)
            if wp is None:
                to_deactivate.append(book["odoo_id"])   # PVP < 2,90 -> apagar
            else:
                prices_by_value.setdefault(wp, []).append(book["odoo_id"])
            pvp_base_updates.append((book["odoo_id"], price))

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

    # ── 8. PRECIO WEB: write por valor único (pvp_base + suplemento) ──
    reactivados: list[int] = []
    for price, tmpl_ids in prices_by_value.items():
        for chunk in _chunks(tmpl_ids, WRITE_CHUNK):
            try:
                await odoo.write("product.template", chunk,
                                 {"list_price": price, "active": True})
                job["price_written"] += len(chunk)
                reactivados.extend(chunk)
            except Exception as e:
                job["err_price"] += len(chunk)
                err = f"price={price}: {type(e).__name__}: {str(e)[:150]}"
                job["errors"].append(err)

    # ── 8a. Desarchivar variantes de lo que acabamos de reactivar ─────
    # Odoo archiva variantes en cascada pero no las devuelve al desarchivar
    # la plantilla: quedaria plantilla activa + variante archivada, que no
    # admite stock ("product.product no encontrado").
    if reactivados:
        try:
            job["variantes_reactivadas"] = (
                job.get("variantes_reactivadas", 0)
                + await pricing_engine.reactivar_variantes(odoo, reactivados))
        except Exception as e:
            job["errors"].append(f"reactivar variantes: {str(e)[:120]}")

    # ── 8b. APAGAR los de PVP < 2,90 (regla API-15) ─────────────────
    for chunk in _chunks(to_deactivate, WRITE_CHUNK):
        try:
            await odoo.write("product.template", chunk, {"active": False})
            job["price_written"] += len(chunk)
        except Exception as e:
            job["err_price"] += len(chunk)
            job["errors"].append(f"apagar: {type(e).__name__}: {str(e)[:120]}")

    # ── 8c. Guardar pvp_base (PVP crudo) en el mirror para idempotencia ─
    if pvp_base_updates:
        try:
            from psycopg2.extras import execute_values
            conn2 = db.get_connection(); cur2 = conn2.cursor()
            execute_values(cur2, """
                UPDATE odoo_books_mirror m SET pvp_base = v.pvp
                FROM (VALUES %s) AS v(oid, pvp)
                WHERE m.odoo_id = v.oid
            """, pvp_base_updates, template="(%s,%s)",
                page_size=len(pvp_base_updates))
            conn2.commit(); conn2.close()
        except Exception as e:
            job["errors"].append(f"pvp_base upd: {str(e)[:120]}")

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
    t_start = time.monotonic()

    # El lock PRIMERO: si no, el cron horario que arranca encima de un
    # backlog en curso pisaba sync_job con un job vacio en estado "error"
    # y el que si estaba trabajando dejaba de verse (paso el 31/07 con el
    # push de PODIPRINT: parecia fallido y habia terminado bien).
    if not _acquire_lock():
        rechazado = _new_job(mode, conc, solo_proveedor, max_books)
        rechazado["status"] = "error"
        rechazado["errors"].append(
            "No se pudo adquirir lock — ya hay otra ejecucion.")
        print("[SinliSync] Lock ocupado, no arranco (hay otra ejecucion)")
        return rechazado

    sync_job = _new_job(mode, conc, solo_proveedor, max_books)
    job = sync_job

    try:
        job["stage"] = "loading_caches"
        prov_to_wh = _load_provider_warehouse_map()
        pausados = _load_pausados()
        job["pausados"] = sorted(pausados)
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
                    pausados=pausados,
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
        try:
            import audit_log
            errs = (job.get("err_no_product", 0) + job.get("err_apply", 0)
                    + job.get("err_price", 0) + job.get("err_other", 0)
                    + job.get("err_no_warehouse", 0))
            # Solo registrar si hubo trabajo real (evitar ruido de ciclos vacios)
            if job["items_processed"] > 0 or errs > 0 or job["status"] == "error":
                audit_log.log_event(
                    "sinli_sync", f"sync_{job['status']}",
                    f"Procesados {job['items_processed']:,} libros SINLI: "
                    f"{job['stock_written']:,} stock, {job['price_written']:,} "
                    f"precios, {errs} errores ({job['elapsed_s']}s, modo {job.get('mode')})",
                    detalle={k: job.get(k) for k in ("items_processed",
                             "stock_written", "price_written", "err_no_product",
                             "err_apply", "err_price", "err_other",
                             "err_no_warehouse", "skipped_pausados", "pausados",
                             "mode", "solo_proveedor",
                             "elapsed_s", "batches_done")},
                    nivel="error" if (errs > 0 or job["status"] == "error") else "info",
                )
        except Exception:
            pass

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


# ─────────────────────────────────────────────────────────────────────
# REEMPLAZO COMPLETO CEGALD (spec Server A 2026-07-08)
#
# El CEGALD funciona por AUSENCIA: el proveedor solo lista lo disponible.
# Lo que no viene → no disponible → stock 0 en SU almacén.
#
# Universo a apagar = quants con quantity > 0 en la location del proveedor
# (leído de Odoo, la fuente de verdad de "qué está disponible ahora").
# Presentes = ISBNs del proveedor en libros_proveedor cuyo
# stock_actualizado_en pertenece a la última corrida CEGALD (ventana
# alrededor del MAX del proveedor).
#
# SALVAGUARDA: si presentes < UMBRAL% del stock actual en Odoo, NO se
# apaga nada (CEGALD probablemente parcial/truncado) — solo se avisa.
# ─────────────────────────────────────────────────────────────────────

CEGALD_UMBRAL_PCT = float(os.environ.get("CEGALD_UMBRAL_PCT", "50"))
# Ventana: presentes = stock_actualizado_en >= max_ts - ventana.
# Un CEGALD se procesa en minutos; 12h cubre corridas largas de n8n sin
# arrastrar el CEGALD anterior (llegan ~1/día por proveedor).
CEGALD_VENTANA_HORAS = float(os.environ.get("CEGALD_VENTANA_HORAS", "12"))
# Frescura: si el ultimo CEGALD es mas viejo que esto, no hay nada nuevo
# que reemplazar (evita apagar con datos rancios).
CEGALD_FRESCURA_HORAS = float(os.environ.get("CEGALD_FRESCURA_HORAS", "48"))

cegald_job: dict | None = None


def get_cegald_status() -> dict:
    job = dict(cegald_job) if cegald_job else {"status": "idle"}
    if "errors" in job:
        job["errors"] = job["errors"][-15:]
    if "isbn_a_apagar_sample" in job:
        job["isbn_a_apagar_sample"] = job["isbn_a_apagar_sample"][:50]
    return job


def stop_cegald():
    global cegald_job
    if cegald_job and cegald_job.get("status") == "running":
        cegald_job["status"] = "stopped"
        return True
    return False


def _cegald_presentes(proveedor_email: str) -> tuple[set[str], str | None]:
    """
    ISBNs presentes en la última foto CEGALD del proveedor.

    Fuente primaria: cegald_isbns_v2 (migrada 2026-07-17: la tabla
    original sinli_cegald_isbns quedó con la secuencia corrupta tras un
    rebuild que cortó Postgres a media escritura — 'cache lookup failed
    for sequence'. La v2 es identica en columnas, con IDENTITY limpia).
    Presentes = ISBNs registrados en la ventana del último archivo.

    Fallback (si la tabla no existe o está vacía para el proveedor):
    timestamps de libros_proveedor. OJO: el upsert del n8n solo toca
    stock_actualizado_en cuando el stock cambia, así que este fallback
    subestima brutalmente los presentes — la salvaguarda de tamaño
    bloqueará el apagado, que es el comportamiento seguro.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        # ── Fuente primaria: cegald_isbns_v2 ─────────────────────────
        try:
            db.execute_query(cur, """
                SELECT MAX(registrado_en) FROM cegald_isbns_v2
                WHERE proveedor_email = ?
            """, (proveedor_email,))
            r = cur.fetchone()
            max_reg = r[0] if r else None
            if max_reg:
                db.execute_query(cur, f"""
                    SELECT DISTINCT isbn FROM cegald_isbns_v2
                    WHERE proveedor_email = ?
                      AND isbn IS NOT NULL
                      AND registrado_en >= (
                          (SELECT MAX(registrado_en) FROM cegald_isbns_v2
                           WHERE proveedor_email = ?)
                          - INTERVAL '{CEGALD_VENTANA_HORAS} hours'
                      )
                """, (proveedor_email, proveedor_email))
                presentes = {row[0] for row in cur.fetchall()}
                if presentes:
                    print(f"[CEGALD] Presentes desde cegald_isbns_v2: "
                          f"{len(presentes):,} (foto {max_reg})")
                    return presentes, str(max_reg)
        except Exception:
            # Tabla aún no existe — fallback silencioso
            try: conn.rollback()
            except Exception: pass

        # ── Fallback: timestamps (subestima; la salvaguarda protege) ──
        db.execute_query(cur, """
            SELECT MAX(stock_actualizado_en) FROM libros_proveedor
            WHERE proveedor_email = ?
        """, (proveedor_email,))
        r = cur.fetchone()
        max_ts = r[0] if r else None
        if not max_ts:
            return set(), None
        db.execute_query(cur, f"""
            SELECT isbn FROM libros_proveedor
            WHERE proveedor_email = ?
              AND stock_disponible > 0
              AND isbn IS NOT NULL
              AND stock_actualizado_en >= (
                  (SELECT MAX(stock_actualizado_en) FROM libros_proveedor
                   WHERE proveedor_email = ?)
                  - INTERVAL '{CEGALD_VENTANA_HORAS} hours'
              )
        """, (proveedor_email, proveedor_email))
        print("[CEGALD] AVISO: usando fallback de timestamps "
              "(sinli_cegald_isbns no disponible) — presentes subestimados")
        return {r[0] for r in cur.fetchall()}, str(max_ts)
    finally:
        conn.close()


async def _odoo_isbns_con_stock(odoo: OdooClient,
                                 location_id: int) -> dict[str, list[int]]:
    """
    {isbn: [quant_ids]} de todos los quants con quantity > 0 en la location.
    Resuelve product_id -> product_tmpl_id -> barcode en batch.
    """
    # Paginado: leer 253k+ quants de una sola llamada revienta Odoo
    # ("Odoo Server Error"). Traer en páginas ordenadas por id.
    quants: list[dict] = []
    PAGE = 40000
    offset = 0
    while True:
        page = await odoo.search_read(
            "stock.quant",
            [["location_id", "=", location_id], ["quantity", ">", 0]],
            ["id", "product_id"], offset=offset, limit=PAGE, order="id",
        )
        quants.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE
    pid_to_quants: dict[int, list[int]] = {}
    for q in quants:
        pid = q["product_id"]
        pid_v = pid[0] if isinstance(pid, list) else pid
        pid_to_quants.setdefault(pid_v, []).append(q["id"])

    out: dict[str, list[int]] = {}
    pids = list(pid_to_quants.keys())
    for i in range(0, len(pids), 1000):
        chunk = pids[i:i + 1000]
        prods = await odoo.search_read(
            "product.product", [["id", "in", chunk]],
            ["id", "product_tmpl_id"],
        )
        tmpl_to_pid = {}
        for p in prods:
            tmpl = p["product_tmpl_id"]
            tmpl_v = tmpl[0] if isinstance(tmpl, list) else tmpl
            tmpl_to_pid[tmpl_v] = p["id"]
        if not tmpl_to_pid:
            continue
        tmpls = await odoo.search_read(
            "product.template", [["id", "in", list(tmpl_to_pid.keys())]],
            ["id", "barcode"],
        )
        for t in tmpls:
            barcode = t.get("barcode")
            if barcode:
                pid_v = tmpl_to_pid[t["id"]]
                out[barcode] = pid_to_quants.get(pid_v, [])
    return out


def _apagar_en_bd_cegald(proveedor_email: str, presentes: set[str],
                         dry_run: bool) -> dict:
    """
    Pone a 0 en libros_proveedor lo que el proveedor ya no lista.

    Es la regla acordada con SINLI: si un libro venia con stock y en el
    siguiente CEGALD no aparece, se da por agotado. Estaba aplicada solo
    contra Odoo, asi que Odoo quedaba bien y nuestra tabla seguia diciendo
    que habia stock. Eso importa mas de lo que parecia: el feed de
    marketplace lee libros_proveedor, no Odoo, y por eso se vendio en Fnac
    el 9788419195531 que Distriforma no listaba desde el 25 de mayo.

    Mismo arreglo que se hizo para AZETA el 10/08 en azeta_push_odoo.
    """
    out = {"candidatos": 0, "apagados": 0, "unidades": 0}
    if not presentes:
        return out
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT isbn, stock_disponible FROM libros_proveedor
            WHERE proveedor_email = ? AND stock_disponible > 0
        """, (proveedor_email,))
        ausentes = [(i, int(q or 0)) for i, q in cur.fetchall()
                    if i not in presentes]
        out["candidatos"] = len(ausentes)
        out["unidades"] = sum(q for _, q in ausentes)
        if dry_run or not ausentes:
            return out

        isbns = [i for i, _ in ausentes]
        LOTE = 5000
        for i in range(0, len(isbns), LOTE):
            db.execute_query(cur, """
                UPDATE libros_proveedor
                SET stock_disponible = 0, actualizado_en = NOW()
                WHERE proveedor_email = ? AND stock_disponible > 0
                  AND isbn = ANY(?)
            """, (proveedor_email, isbns[i:i + LOTE]))
            out["apagados"] += cur.rowcount or 0
            conn.commit()
        return out
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return out
    finally:
        conn.close()


async def run_cegald_replacement(proveedor_email: str,
                                  dry_run: bool = True) -> dict:
    """
    Reemplazo completo CEGALD para UN proveedor:
    - presentes en el último CEGALD → (ya están a 1 vía sync normal)
    - con stock en Odoo pero AUSENTES del CEGALD → stock 0

    dry_run=True: calcula y reporta sin escribir (modo obligatorio de prueba).
    """
    global cegald_job
    cegald_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "stage": "starting",
        "proveedor": proveedor_email,
        "dry_run": dry_run,
        "cegald_max_ts": None,
        "presentes": 0,
        "con_stock_odoo": 0,
        "a_apagar": 0,
        "apagados": 0,
        "salvaguarda_activada": False,
        "salvaguarda_motivo": None,
        "isbn_a_apagar_sample": [],
        "bd_candidatos": 0,
        "bd_apagados": 0,
        "bd_unidades": 0,
        "errors": [],
        "elapsed_s": 0,
    }
    job = cegald_job
    t_start = time.monotonic()

    try:
        if proveedor_email == AZETA_EMAIL:
            raise RuntimeError("AZETA esta excluido del reemplazo CEGALD")
        if proveedor_email in _load_pausados():
            raise RuntimeError(
                f"{proveedor_email} esta PAUSADO: su almacen ya esta a 0. "
                "Reactivalo antes de correr el reemplazo CEGALD.")

        # Resolver location del proveedor
        job["stage"] = "resolving_location"
        prov_to_wh = _load_provider_warehouse_map()
        wh_code = prov_to_wh.get(proveedor_email)
        if not wh_code:
            raise RuntimeError(f"Proveedor sin mapping: {proveedor_email}")

        async with OdooClient() as odoo:
            wh_to_loc = await _load_warehouse_locations(odoo)
            location_id = wh_to_loc.get(wh_code)
            if not location_id:
                raise RuntimeError(f"Warehouse {wh_code} sin location en Odoo")
            if location_id == 14:
                raise RuntimeError("Anti-colision: location 14 es de AZETA")
            job["warehouse"] = wh_code
            job["location_id"] = location_id

            # 1. Presentes en el ultimo CEGALD
            job["stage"] = "reading_cegald"
            presentes, max_ts = _cegald_presentes(proveedor_email)
            job["cegald_max_ts"] = max_ts
            job["presentes"] = len(presentes)
            if not presentes:
                raise RuntimeError("Sin CEGALD: 0 presentes en libros_proveedor")

            # Frescura: no reemplazar con un CEGALD viejo
            from datetime import timedelta, timezone as _tz
            max_dt = datetime.fromisoformat(max_ts)
            if max_dt.tzinfo is not None:
                max_dt = max_dt.astimezone(_tz.utc).replace(tzinfo=None)
            age_h = (datetime.utcnow() - max_dt).total_seconds() / 3600
            job["cegald_age_hours"] = round(age_h, 1)
            if age_h > CEGALD_FRESCURA_HORAS:
                job["salvaguarda_activada"] = True
                job["salvaguarda_motivo"] = (
                    f"CEGALD viejo ({age_h:.0f}h > {CEGALD_FRESCURA_HORAS:.0f}h). "
                    "Sin datos frescos, apagado omitido."
                )

            # 2. Con stock en Odoo (universo a revisar)
            job["stage"] = "reading_odoo_stock"
            isbn_quants = await _odoo_isbns_con_stock(odoo, location_id)
            job["con_stock_odoo"] = len(isbn_quants)

            # 3. Diferencia
            a_apagar = set(isbn_quants.keys()) - presentes
            job["a_apagar"] = len(a_apagar)
            job["isbn_a_apagar_sample"] = sorted(a_apagar)[:100]

            # 4. SALVAGUARDA de tamaño (spec seccion 5)
            if not job["salvaguarda_activada"] and job["con_stock_odoo"] > 0:
                pct = 100 * len(presentes) / job["con_stock_odoo"]
                job["presentes_vs_stock_pct"] = round(pct, 1)
                if pct < CEGALD_UMBRAL_PCT:
                    job["salvaguarda_activada"] = True
                    job["salvaguarda_motivo"] = (
                        f"CEGALD sospechosamente pequeño: {len(presentes):,} "
                        f"presentes vs {job['con_stock_odoo']:,} con stock "
                        f"({pct:.0f}% < {CEGALD_UMBRAL_PCT:.0f}%). Apagado omitido."
                    )

            # 5. Apagar (si no dry_run y sin salvaguarda)
            if dry_run:
                job["stage"] = "dry_run_done"
            elif job["salvaguarda_activada"]:
                job["stage"] = "skipped_by_safeguard"
            else:
                job["stage"] = "apagando"
                quant_ids = [qid for isbn in a_apagar
                             for qid in isbn_quants[isbn]]
                CHUNK = 500
                for i in range(0, len(quant_ids), CHUNK):
                    if job["status"] != "running":
                        break
                    chunk = quant_ids[i:i + CHUNK]
                    try:
                        await odoo.write("stock.quant", chunk,
                                         {"inventory_quantity": 0})
                        try:
                            await odoo.execute_kw(
                                "stock.quant", "action_apply_inventory",
                                [chunk])
                        except Exception as apply_err:
                            verify = await odoo.read(
                                "stock.quant", chunk, ["quantity"])
                            ok = all(abs(float(v.get("quantity") or 0)) < 0.01
                                     for v in verify)
                            if not ok:
                                raise apply_err
                        job["apagados"] += len(chunk)
                    except Exception as e:
                        err = f"apagar chunk@{i}: {type(e).__name__}: {str(e)[:150]}"
                        job["errors"].append(err)
                        print(f"[CEGALD] {err}")

        # Y lo mismo en nuestra tabla, con las mismas salvaguardas. Va fuera
        # del bloque de Odoo porque no depende de los quants: alcanza tambien
        # a los que se apagaron en Odoo hace semanas y aqui nunca se
        # limpiaron. Sin esto, quien lee libros_proveedor -el feed de
        # marketplace- sigue vendiendo lo que el proveedor ya no tiene.
        if not job["salvaguarda_activada"]:
            job["stage"] = "apagando en la BD"
            bd = _apagar_en_bd_cegald(proveedor_email, presentes, dry_run)
            job["bd_candidatos"] = bd["candidatos"]
            job["bd_apagados"] = bd["apagados"]
            job["bd_unidades"] = bd["unidades"]
            if bd.get("error"):
                job["errors"].append(f"bd: {bd['error']}")
            print(f"[CEGALD] BD: {bd['candidatos']:,} con stock que ya no "
                  f"lista ({bd['unidades']:,} uds), {bd['apagados']:,} apagados")

        if job["status"] == "running":
            job["status"] = "completed"
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e!r}"[:300])
        print(f"[CEGALD] Fatal: {e!r}")
    finally:
        job["elapsed_s"] = round(time.monotonic() - t_start, 2)
        resumen = (
            f"CEGALD {proveedor_email.split('@')[0]}"
            f"{' [DRY RUN]' if dry_run else ''}: "
            f"{job['presentes']:,} presentes, {job['con_stock_odoo']:,} con "
            f"stock en {job.get('warehouse', '?')}, {job['a_apagar']:,} a apagar"
            f"{', ' + str(job['apagados']) + ' apagados' if not dry_run else ''}. "
            f"Salvaguarda: "
            f"{job['salvaguarda_motivo'] if job['salvaguarda_activada'] else 'OK (tamaño normal)'}"
        )
        print(f"[CEGALD] {resumen}")
        try:
            import audit_log
            audit_log.log_event(
                "sinli_sync",
                "cegald_dry_run" if dry_run else "cegald_replacement",
                resumen,
                detalle={k: job.get(k) for k in (
                    "proveedor", "warehouse", "presentes", "con_stock_odoo",
                    "a_apagar", "apagados", "salvaguarda_activada",
                    "salvaguarda_motivo", "cegald_max_ts", "cegald_age_hours",
                    "dry_run", "elapsed_s")},
                nivel="error" if (job["status"] == "error"
                                   or job["salvaguarda_activada"]) else "info",
            )
        except Exception:
            pass

    return job


if __name__ == "__main__":
    # python sync_stock_sinli.py once     -> un lote
    # python sync_stock_sinli.py backlog  -> bucle hasta vaciar
    # python sync_stock_sinli.py cegald <email> [--apply]  -> reemplazo CEGALD
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if mode == "cegald":
        email = sys.argv[2]
        dry = "--apply" not in sys.argv
        asyncio.run(run_cegald_replacement(email, dry_run=dry))
    else:
        asyncio.run(run_once(loop_until_empty=(mode == "backlog")))