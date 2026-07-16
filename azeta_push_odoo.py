"""
Fase 2 AZETA — Push de datos enriquecidos del mirror a Odoo.

Lee odoo_books_mirror WHERE azeta_fetched_at IS NOT NULL y escribe en Odoo:
- product.template.description       (HTML de Sinopsis)
- product.template.weight            (kg, convertido desde "557.0 gr")
- product.template.list_price        (azeta_price_eur)
- product.template.categ_id          (resuelto desde odoo_product_categories_cache)
- stock.quant en AZE01 (location 14, cap 50, stock=0 -> queda en 0 sin borrar)

Idempotente. Solo procesa libros AZETA (inferred_source='azeta_catalog' o
supplier_names ILIKE '%AZETA%'). NO toca otros warehouses.

Workaround Odoo v19 SaaS: action_apply_inventory puede lanzar
"cannot marshal None" (XML-RPC) o un OdooError genérico (JSON-RPC).
La operación SI se aplica; envolvemos en try/except y verificamos releyendo.
"""
import asyncio
import os
import re
import time
from datetime import datetime
from typing import Any

import db
from odoo_client import OdooClient, OdooError


# ── Constantes AZETA ──────────────────────────────────────────────────
AZE01_LOCATION_ID = int(os.environ.get("AZETA_LOCATION_ID", "14"))
STOCK_CAP = 50
AZETA_PROVEEDOR_EMAIL = "info@azetadistribuciones.es"
ENTIDAD_STOCK = "azeta_stock_to_odoo"  # key en sync_state para marker

# Tope de libros por corrida (None = todos). Usar para tests con 1 libro.
DEFAULT_BATCH_SIZE = 200


def _get_azeta_marker():
    """Lee el marker (stock_actualizado_en) del último ciclo. Naive."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur,
            "SELECT ultimo_timestamp FROM sync_state WHERE entidad = ?",
            (ENTIDAD_STOCK,))
        r = cur.fetchone()
        if not r or r[0] is None:
            return None
        ts = r[0]
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            from datetime import timezone
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        return ts
    finally:
        conn.close()


def _ensure_azeta_marker_row():
    """Asegura que la fila exista en sync_state. Sin ella, no podemos marker."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur,
            "SELECT 1 FROM sync_state WHERE entidad = ?", (ENTIDAD_STOCK,))
        if cur.fetchone():
            return
        if db.IS_POSTGRES:
            db.execute_query(cur, """
                INSERT INTO sync_state
                    (entidad, ultimo_timestamp, ultima_ejecucion,
                     ultima_ejecucion_ok, items_procesados, lock_activo)
                VALUES (?, '1970-01-01 00:00:00+00', NULL, NULL, 0, false)
                ON CONFLICT (entidad) DO NOTHING
            """, (ENTIDAD_STOCK,))
        else:
            db.execute_query(cur, """
                INSERT OR IGNORE INTO sync_state
                    (entidad, ultimo_timestamp, items_procesados, lock_activo)
                VALUES (?, '1970-01-01 00:00:00', 0, 0)
            """, (ENTIDAD_STOCK,))
        conn.commit()
    except Exception as e:
        print(f"[AzetaPush] ensure_marker_row FAIL: {e}")
        try: conn.rollback()
        except Exception: pass
    finally:
        conn.close()


def _advance_azeta_marker(new_ts, items_count: int):
    """Avanza marker tras corrida exitosa."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            UPDATE sync_state
            SET ultimo_timestamp = ?,
                ultima_ejecucion = NOW(),
                ultima_ejecucion_ok = true,
                items_procesados = ?
            WHERE entidad = ?
        """, (new_ts, items_count, ENTIDAD_STOCK))
        conn.commit()
    except Exception as e:
        print(f"[AzetaPush] advance_marker FAIL: {e}")
        try: conn.rollback()
        except Exception: pass
    finally:
        conn.close()


def _set_azeta_marker_to_now():
    """Setea marker = NOW(). Usar tras backlog inicial para evitar reproceso."""
    _ensure_azeta_marker_row()
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            UPDATE sync_state SET ultimo_timestamp = NOW()
            WHERE entidad = ?
        """, (ENTIDAD_STOCK,))
        conn.commit()
    finally:
        conn.close()


# ── Estado del job ────────────────────────────────────────────────────
push_job: dict | None = None


def get_push_status() -> dict:
    job = dict(push_job) if push_job else {"status": "idle"}
    if "errors" in job:
        job["errors"] = job["errors"][-15:]
    return job


def stop_push():
    global push_job
    if push_job and push_job.get("status") == "running":
        push_job["status"] = "stopped"
        return True
    return False


# ── Helpers de conversion ─────────────────────────────────────────────
_WEIGHT_RE = re.compile(r"([\d.,]+)\s*(g|gr|kg)?", re.IGNORECASE)


def _parse_weight_to_kg(text: str | None) -> float | None:
    """'557.0 gr' -> 0.557; '1.5 kg' -> 1.5; '' -> None"""
    if not text:
        return None
    m = _WEIGHT_RE.search(text)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    unit = (m.group(2) or "gr").lower()
    if unit == "kg":
        return round(val, 3)
    return round(val / 1000.0, 3)


def _safe_truncate_html(text: str | None, max_len: int = 30000) -> str | None:
    """Odoo no tiene limite duro pero evitamos payloads gigantes."""
    if not text:
        return None
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


# ── Lectura del mirror ────────────────────────────────────────────────
def _count_azeta_books_to_push() -> int:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT COUNT(*) FROM odoo_books_mirror
            WHERE azeta_fetched_at IS NOT NULL
              AND odoo_id IS NOT NULL
        """)
        return int(cur.fetchone()[0])
    except Exception:
        return 0
    finally:
        conn.close()


def _read_azeta_books_batch(offset: int, limit: int,
                            only_isbn: str | None = None) -> list[dict]:
    """
    Devuelve filas con: odoo_id, barcode, description, cdl_weight,
    azeta_price_eur, inferred_categories, supplier_names.

    only_isbn: si se pasa, devuelve solo ese ISBN (modo test).
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        if only_isbn:
            db.execute_query(cur, """
                SELECT odoo_id, barcode, description, cdl_weight,
                       azeta_price_eur, inferred_categories, supplier_names
                FROM odoo_books_mirror
                WHERE barcode = ?
                  AND azeta_fetched_at IS NOT NULL
                LIMIT 1
            """, (only_isbn,))
        else:
            db.execute_query(cur, """
                SELECT odoo_id, barcode, description, cdl_weight,
                       azeta_price_eur, inferred_categories, supplier_names
                FROM odoo_books_mirror
                WHERE azeta_fetched_at IS NOT NULL
                  AND odoo_id IS NOT NULL
                ORDER BY odoo_id
                LIMIT ? OFFSET ?
            """, (limit, offset))
        rows = cur.fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        out.append({
            "odoo_id": r[0],
            "barcode": r[1],
            "description": r[2],
            "cdl_weight": r[3],
            "azeta_price_eur": r[4],
            "inferred_categories": r[5],
            "supplier_names": r[6],
        })
    return out


def _load_categ_cache() -> dict[str, int]:
    """Pre-carga odoo_product_categories_cache (path -> categ_id)."""
    conn = db.get_connection()
    cur = conn.cursor()
    out: dict[str, int] = {}
    try:
        db.execute_query(cur,
            "SELECT full_path, odoo_categ_id FROM odoo_product_categories_cache")
        for row in cur.fetchall():
            out[row[0]] = row[1]
    except Exception:
        pass
    finally:
        conn.close()
    return out


def _load_stock_by_isbn() -> dict[str, int]:
    """Mapa isbn -> stock_disponible (cap 50) desde libros_proveedor AZETA."""
    conn = db.get_connection()
    cur = conn.cursor()
    out: dict[str, int] = {}
    try:
        db.execute_query(cur, """
            SELECT isbn, COALESCE(stock_disponible, 0)
            FROM libros_proveedor
            WHERE proveedor_email = ?
        """, (AZETA_PROVEEDOR_EMAIL,))
        for row in cur.fetchall():
            qty = int(row[1] or 0)
            if qty > STOCK_CAP:
                qty = STOCK_CAP
            out[row[0]] = qty
    except Exception:
        pass
    finally:
        conn.close()
    return out


# ── Push helpers (Odoo) ───────────────────────────────────────────────
async def _push_template_fields(odoo: OdooClient, book: dict, job: dict):
    """Escribe description, weight, list_price en product.template."""
    values: dict[str, Any] = {}

    desc = _safe_truncate_html(book.get("description"))
    if desc:
        values["description"] = desc

    weight_kg = _parse_weight_to_kg(book.get("cdl_weight"))
    if weight_kg is not None and weight_kg > 0:
        values["weight"] = weight_kg

    price = book.get("azeta_price_eur")
    if price is not None:
        try:
            values["list_price"] = float(price)
        except (TypeError, ValueError):
            pass

    if not values:
        return

    try:
        await odoo.write("product.template", [book["odoo_id"]], values)
        job["template_written"] += 1
    except Exception as e:
        job["template_errors"] += 1
        err = f"tmpl {book['odoo_id']}: {type(e).__name__}: {str(e)[:120]}"
        job["errors"].append(err)


async def _push_categ_id(odoo: OdooClient, book: dict,
                         categ_cache: dict[str, int], job: dict):
    """Asigna categ_id si tenemos el path en cache."""
    path = book.get("inferred_categories")
    if not path:
        return
    cat_id = categ_cache.get(path)
    if not cat_id:
        job["categ_no_cache"] += 1
        return
    try:
        await odoo.write("product.template", [book["odoo_id"]], {"categ_id": cat_id})
        job["categ_assigned"] += 1
    except Exception as e:
        job["categ_errors"] += 1
        err = f"categ tmpl {book['odoo_id']}: {type(e).__name__}: {str(e)[:120]}"
        job["errors"].append(err)


async def _get_product_id(odoo: OdooClient, template_id: int) -> int | None:
    """product.template.id -> product.product.id (variante default)."""
    res = await odoo.search_read(
        "product.product",
        [["product_tmpl_id", "=", template_id]],
        ["id"], limit=1,
    )
    if res:
        return res[0]["id"]
    return None


async def _push_stock_quant(odoo: OdooClient, book: dict,
                            stock_map: dict[str, int], job: dict):
    """
    Escribe stock.quant en location AZE01 (14):
    - Si no hay quant, crea uno con inventory_quantity = stock
    - Si existe, write inventory_quantity = stock
    - Llama action_apply_inventory (con workaround Fault)
    - Verifica releyendo el quant

    stock = 0 SE ESCRIBE (no borra). Si no existe quant, crea uno en 0.
    """
    isbn = book.get("barcode")
    if not isbn:
        return
    qty = stock_map.get(isbn)
    if qty is None:
        # ISBN no en libros_proveedor AZETA: no hay info de stock, skip silencioso
        job["stock_no_data"] += 1
        return

    pid = await _get_product_id(odoo, book["odoo_id"])
    if not pid:
        job["stock_no_product"] += 1
        return

    # Buscar quant existente en location AZE01
    quants = await odoo.search_read(
        "stock.quant",
        [["product_id", "=", pid], ["location_id", "=", AZE01_LOCATION_ID]],
        ["id", "quantity", "inventory_quantity"], limit=1,
    )

    try:
        if quants:
            quant_id = quants[0]["id"]
            await odoo.write("stock.quant", [quant_id],
                             {"inventory_quantity": qty})
        else:
            quant_id = await odoo.execute_kw(
                "stock.quant", "create",
                [{"product_id": pid, "location_id": AZE01_LOCATION_ID,
                  "inventory_quantity": qty}],
            )
        # Workaround Fault/OdooError. Ejecutar y capturar.
        try:
            await odoo.execute_kw(
                "stock.quant", "action_apply_inventory", [[quant_id]],
            )
        except (OdooError, Exception) as apply_err:
            # Verificar releyendo si en realidad si aplicó
            verify = await odoo.read("stock.quant", [quant_id], ["quantity"])
            if verify and abs(float(verify[0].get("quantity") or 0) - qty) < 0.01:
                # OK — solo fue el bug de serialización
                pass
            else:
                raise apply_err

        job["stock_written"] += 1
    except Exception as e:
        job["stock_errors"] += 1
        err = f"stock pid={pid}: {type(e).__name__}: {str(e)[:120]}"
        job["errors"].append(err)


# ── Orchestrator principal ────────────────────────────────────────────
async def run_azeta_push(batch_size: int = DEFAULT_BATCH_SIZE,
                         test_isbn: str | None = None,
                         max_books: int | None = None) -> dict:
    """
    Empuja datos AZETA del mirror a Odoo.

    test_isbn: si se pasa, procesa SOLO ese ISBN (validación manual).
    max_books: si se pasa, tope de libros procesados (None = todos).
    """
    global push_job
    push_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "stage": "starting",
        "test_isbn": test_isbn,
        "total_to_process": 0,
        "processed": 0,
        "template_written": 0,
        "template_errors": 0,
        "categ_assigned": 0,
        "categ_no_cache": 0,
        "categ_errors": 0,
        "stock_written": 0,
        "stock_errors": 0,
        "stock_no_data": 0,
        "stock_no_product": 0,
        "elapsed_s": 0,
        "errors": [],
    }
    job = push_job
    t_start = time.monotonic()

    try:
        # Modo test: 1 ISBN
        if test_isbn:
            job["stage"] = "test_single"
            books = _read_azeta_books_batch(0, 1, only_isbn=test_isbn)
            if not books:
                job["errors"].append(
                    f"ISBN {test_isbn} no encontrado en mirror "
                    "o sin azeta_fetched_at"
                )
                job["status"] = "error"
                return job
            job["total_to_process"] = 1
        else:
            job["stage"] = "counting"
            total = _count_azeta_books_to_push()
            if max_books:
                total = min(total, max_books)
            job["total_to_process"] = total
            print(f"[AzetaPush] {total:,} libros AZETA a procesar")

        # Pre-carga de cache de categorías y stock
        job["stage"] = "loading_caches"
        categ_cache = _load_categ_cache()
        stock_map = _load_stock_by_isbn()
        print(f"[AzetaPush] Cache categorías: {len(categ_cache):,} paths · "
              f"stock map: {len(stock_map):,} ISBNs")

        if not categ_cache:
            job["errors"].append(
                "Cache de categorías vacío. Corre push_categories_to_odoo "
                "antes para que categ_id pueda resolverse."
            )

        # Loop principal
        job["stage"] = "pushing"
        async with OdooClient() as odoo:
            offset = 0
            while job["status"] == "running":
                if test_isbn:
                    books = _read_azeta_books_batch(0, 1, only_isbn=test_isbn)
                    if not books:
                        break
                else:
                    if max_books and offset >= max_books:
                        break
                    remaining = (max_books - offset) if max_books else batch_size
                    chunk_size = min(batch_size, remaining)
                    books = _read_azeta_books_batch(offset, chunk_size)
                    if not books:
                        break

                for book in books:
                    if job["status"] != "running":
                        break
                    await _push_template_fields(odoo, book, job)
                    await _push_categ_id(odoo, book, categ_cache, job)
                    await _push_stock_quant(odoo, book, stock_map, job)
                    job["processed"] += 1
                    if job["processed"] % 50 == 0:
                        print(f"[AzetaPush] {job['processed']:,}/"
                              f"{job['total_to_process']:,} "
                              f"tmpl:{job['template_written']} "
                              f"cat:{job['categ_assigned']} "
                              f"stk:{job['stock_written']}")

                if test_isbn:
                    break
                offset += len(books)

        if job["status"] == "running":
            job["status"] = "completed"
        job["stage"] = "done"
        job["elapsed_s"] = round(time.monotonic() - t_start, 2)
        print(f"[AzetaPush] DONE proc={job['processed']:,} "
              f"tmpl={job['template_written']:,} cat={job['categ_assigned']:,} "
              f"stk={job['stock_written']:,} en {job['elapsed_s']}s")
    except Exception as e:
        job["status"] = "error"
        err = f"{type(e).__name__}: {e!r}"
        job["errors"].append(err[:300])
        print(f"[AzetaPush] Fatal: {err}")

    return job


# ─────────────────────────────────────────────────────────────────────
# STOCK-ONLY PUSH (modo rapido, solo escribe stock.quant en AZE01)
# ─────────────────────────────────────────────────────────────────────

stock_push_job: dict | None = None


def get_stock_push_status() -> dict:
    job = dict(stock_push_job) if stock_push_job else {"status": "idle"}
    if "errors" in job:
        job["errors"] = job["errors"][-15:]
    return job


def stop_stock_push():
    global stock_push_job
    if stock_push_job and stock_push_job.get("status") == "running":
        stock_push_job["status"] = "stopped"
        return True
    return False


def _read_stock_targets(test_isbn: str | None = None,
                       max_books: int | None = None,
                       only_since=None) -> list[dict]:
    """
    Lee odoo_id + barcode + qty + stock_actualizado_en cruzando mirror con
    libros_proveedor AZETA.
    only_since: si se pasa, filtra por stock_actualizado_en > only_since.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        if test_isbn:
            sql = """
                SELECT m.odoo_id, m.barcode,
                       COALESCE(lp.stock_disponible, 0),
                       lp.stock_actualizado_en
                FROM odoo_books_mirror m
                JOIN libros_proveedor lp ON lp.isbn = m.barcode
                WHERE lp.proveedor_email = ?
                  AND m.odoo_id IS NOT NULL
                  AND m.barcode = ?
                LIMIT 1
            """
            db.execute_query(cur, sql, (AZETA_PROVEEDOR_EMAIL, test_isbn))
        elif only_since:
            sql = """
                SELECT m.odoo_id, m.barcode,
                       COALESCE(lp.stock_disponible, 0),
                       lp.stock_actualizado_en
                FROM odoo_books_mirror m
                JOIN libros_proveedor lp ON lp.isbn = m.barcode
                WHERE lp.proveedor_email = ?
                  AND m.odoo_id IS NOT NULL
                  AND lp.stock_actualizado_en > ?
                ORDER BY lp.stock_actualizado_en
            """
            if max_books:
                sql += f"\n                LIMIT {int(max_books)}"
            db.execute_query(cur, sql, (AZETA_PROVEEDOR_EMAIL, only_since))
        else:
            sql = """
                SELECT m.odoo_id, m.barcode,
                       COALESCE(lp.stock_disponible, 0),
                       lp.stock_actualizado_en
                FROM odoo_books_mirror m
                JOIN libros_proveedor lp ON lp.isbn = m.barcode
                WHERE lp.proveedor_email = ?
                  AND m.odoo_id IS NOT NULL
                ORDER BY m.odoo_id
            """
            if max_books:
                sql += f"\n                LIMIT {int(max_books)}"
            db.execute_query(cur, sql, (AZETA_PROVEEDOR_EMAIL,))
        rows = cur.fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        qty = int(r[2] or 0)
        if qty > STOCK_CAP:
            qty = STOCK_CAP
        out.append({
            "odoo_id": r[0],
            "barcode": r[1],
            "qty": qty,
            "stock_actualizado_en": r[3],
        })
    return out


# Tamaño de lote para batch operations.
# - SEARCH_CHUNK: cuántos IDs por search_read [in [...]] (500-1000 OK)
# - WRITE_CHUNK: cuántos quant_ids por write/apply (500-1000 OK, evita 502)
# - CREATE_CHUNK: cuántos dicts por create (más bajo, 200-500)
SEARCH_CHUNK = int(os.environ.get("AZETA_PUSH_SEARCH_CHUNK", "1000"))
WRITE_CHUNK  = int(os.environ.get("AZETA_PUSH_WRITE_CHUNK", "500"))
CREATE_CHUNK = int(os.environ.get("AZETA_PUSH_CREATE_CHUNK", "200"))


def _chunks(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


async def _resolve_product_ids_batch(odoo: OdooClient,
                                      template_ids: list[int]) -> dict[int, int]:
    """
    Una sola search_read por chunk: pid = {tmpl_id: product.product.id}.
    De N llamadas individuales a N/SEARCH_CHUNK llamadas.
    """
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
            # Multiples variantes por tmpl → quedarnos con la primera
            if tmpl_id not in out:
                out[tmpl_id] = r["id"]
    return out


async def _resolve_quants_batch(odoo: OdooClient,
                                 product_ids: list[int],
                                 location_id: int) -> dict[int, int]:
    """
    Una sola search_read por chunk: {product_id: quant_id} para los que
    YA existen en la location. Los que no aparecen → hay que crearlos.
    """
    out: dict[int, int] = {}
    unique = list(set(product_ids))
    for chunk in _chunks(unique, SEARCH_CHUNK):
        rows = await odoo.search_read(
            "stock.quant",
            [["product_id", "in", chunk],
             ["location_id", "=", location_id]],
            ["id", "product_id"],
        )
        for r in rows:
            pid = r.get("product_id")
            pid_v = pid[0] if isinstance(pid, list) else pid
            if pid_v not in out:
                out[pid_v] = r["id"]
    return out


async def run_azeta_stock_push_only(test_isbn: str | None = None,
                                     max_books: int | None = None,
                                     concurrency: int | None = None,
                                     use_marker: bool = False) -> dict:
    """
    Push SOLO de stock.quant en AZE01 (location 14) — VERSION BATCH.

    Estrategia: agrupar por valor de qty (cap 50 → solo 51 valores únicos
    posibles), una sola write() por grupo. action_apply_inventory en lotes
    de 500. Reduce llamadas de ~700k a ~1-2k (speed-up ~1000x).

    test_isbn: procesa solo 1 ISBN (validacion)
    max_books: tope total
    concurrency: ignorado en modo batch (lo dejamos para retrocompatibilidad)
    use_marker: si True, solo procesa libros cuyo stock_actualizado_en >
        marker (sync_state.entidad='azeta_stock_to_odoo'). Después avanza
        marker. Use marker=True solo desde el cron (no manual full push).
    """
    global stock_push_job
    marker = None
    if use_marker and not test_isbn:
        _ensure_azeta_marker_row()
        marker = _get_azeta_marker()
    stock_push_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "stage": "starting",
        "test_isbn": test_isbn,
        "mode": "batch-incremental" if use_marker else "batch",
        "marker": str(marker) if marker else None,
        "total_to_process": 0,
        "processed": 0,
        "stock_written": 0,
        "stock_created": 0,
        "stock_updated": 0,
        "stock_applied": 0,
        "stock_errors": 0,
        "stock_no_product": 0,
        "unique_qty_values": 0,
        "calls_made": 0,
        "elapsed_s": 0,
        "elapsed_load_s": 0,
        "elapsed_resolve_s": 0,
        "elapsed_write_s": 0,
        "elapsed_apply_s": 0,
        "errors": [],
    }
    job = stock_push_job
    t_start = time.monotonic()
    max_ts_seen = marker  # para avanzar marker tras éxito

    try:
        # ── 1. Cargar targets ─────────────────────────────────────────
        job["stage"] = "loading_targets"
        targets = _read_stock_targets(
            test_isbn=test_isbn, max_books=max_books,
            only_since=marker if use_marker else None,
        )
        job["total_to_process"] = len(targets)
        # Calcular max timestamp para avanzar marker después
        if targets:
            for t in targets:
                ts = t.get("stock_actualizado_en")
                if ts and (max_ts_seen is None or ts > max_ts_seen):
                    max_ts_seen = ts
        job["elapsed_load_s"] = round(time.monotonic() - t_start, 2)
        print(f"[AzetaBatch] {len(targets):,} libros AZETA a actualizar "
              f"(marker={marker})")

        if not targets:
            job["status"] = "completed"
            job["stage"] = "done"
            return job

        async with OdooClient() as odoo:
            # ── 2. Pre-resolver product_ids (batch) ──────────────────
            t0 = time.monotonic()
            job["stage"] = "resolving_product_ids"
            template_ids = [t["odoo_id"] for t in targets]
            pid_cache = await _resolve_product_ids_batch(odoo, template_ids)
            job["calls_made"] += (len(set(template_ids)) // SEARCH_CHUNK) + 1
            print(f"[AzetaBatch] product_ids resueltos: {len(pid_cache):,}/"
                  f"{len(set(template_ids)):,} en {round(time.monotonic()-t0,2)}s")

            # ── 3. Pre-resolver quants existentes en AZE01 ────────────
            job["stage"] = "resolving_quants"
            product_ids = list(pid_cache.values())
            quant_cache = await _resolve_quants_batch(
                odoo, product_ids, AZE01_LOCATION_ID
            )
            job["calls_made"] += (len(set(product_ids)) // SEARCH_CHUNK) + 1
            job["elapsed_resolve_s"] = round(time.monotonic() - t0, 2)
            print(f"[AzetaBatch] quants existentes: {len(quant_cache):,} "
                  f"en {job['elapsed_resolve_s']}s")

            # ── 4. Particionar: create vs update-by-qty ──────────────
            job["stage"] = "partitioning"
            to_create: list[dict] = []
            to_update_by_qty: dict[int, list[int]] = {}

            for t in targets:
                pid = pid_cache.get(t["odoo_id"])
                if not pid:
                    job["stock_no_product"] += 1
                    job["processed"] += 1
                    continue
                qty = t["qty"]
                if pid in quant_cache:
                    quant_id = quant_cache[pid]
                    to_update_by_qty.setdefault(qty, []).append(quant_id)
                else:
                    to_create.append({
                        "product_id": pid,
                        "location_id": AZE01_LOCATION_ID,
                        "inventory_quantity": qty,
                    })
                job["processed"] += 1

            job["unique_qty_values"] = len(to_update_by_qty)
            print(f"[AzetaBatch] particion: {len(to_create):,} crear, "
                  f"{sum(len(v) for v in to_update_by_qty.values()):,} update "
                  f"en {len(to_update_by_qty)} grupos de qty")

            # ── 5. CREATE en lotes ────────────────────────────────────
            t0 = time.monotonic()
            job["stage"] = "creating_quants"
            new_quant_ids: list[int] = []
            for chunk in _chunks(to_create, CREATE_CHUNK):
                if job["status"] != "running":
                    break
                try:
                    res = await odoo.execute_kw(
                        "stock.quant", "create", [chunk]
                    )
                    job["calls_made"] += 1
                    if isinstance(res, list):
                        new_quant_ids.extend(res)
                    elif isinstance(res, int):
                        new_quant_ids.append(res)
                    job["stock_created"] += len(chunk)
                except Exception as e:
                    job["stock_errors"] += len(chunk)
                    err = f"create chunk: {type(e).__name__}: {str(e)[:150]}"
                    job["errors"].append(err)
                    print(f"[AzetaBatch] {err}")

            # ── 6. UPDATE agrupados por qty (1 llamada por valor) ─────
            job["stage"] = "updating_quants"
            for qty, quant_ids in to_update_by_qty.items():
                if job["status"] != "running":
                    break
                # Sub-chunkar por seguridad (puede haber miles con qty=0)
                for chunk in _chunks(quant_ids, WRITE_CHUNK):
                    try:
                        await odoo.write("stock.quant", chunk,
                                         {"inventory_quantity": qty})
                        job["calls_made"] += 1
                        job["stock_updated"] += len(chunk)
                    except Exception as e:
                        job["stock_errors"] += len(chunk)
                        err = f"update qty={qty}: {type(e).__name__}: {str(e)[:150]}"
                        job["errors"].append(err)
                        print(f"[AzetaBatch] {err}")
            job["elapsed_write_s"] = round(time.monotonic() - t0, 2)

            # ── 7. APPLY en lotes ─────────────────────────────────────
            t0 = time.monotonic()
            job["stage"] = "applying_inventory"
            all_quant_ids = new_quant_ids + [
                qid for qids in to_update_by_qty.values() for qid in qids
            ]
            print(f"[AzetaBatch] aplicando inventory a {len(all_quant_ids):,} quants")

            for chunk in _chunks(all_quant_ids, WRITE_CHUNK):
                if job["status"] != "running":
                    break
                try:
                    await odoo.execute_kw(
                        "stock.quant", "action_apply_inventory", [chunk]
                    )
                    job["calls_made"] += 1
                except Exception as apply_err:
                    # Workaround Fault: verificar releyendo
                    try:
                        verify = await odoo.read(
                            "stock.quant", chunk, ["quantity", "inventory_quantity"]
                        )
                        # Si todos los quants tienen quantity == inventory_quantity, OK
                        ok = all(
                            abs(float(v.get("quantity") or 0)
                                - float(v.get("inventory_quantity") or 0)) < 0.01
                            for v in verify
                        )
                        if not ok:
                            job["stock_errors"] += 1
                            err = f"apply chunk: {type(apply_err).__name__}: {str(apply_err)[:150]}"
                            job["errors"].append(err)
                            print(f"[AzetaBatch] {err}")
                    except Exception as verify_err:
                        job["stock_errors"] += 1
                        err = f"verify chunk: {type(verify_err).__name__}: {str(verify_err)[:150]}"
                        job["errors"].append(err)
                        print(f"[AzetaBatch] {err}")

                job["stock_applied"] += len(chunk)
                job["stock_written"] = job["stock_applied"]  # alias UI
                print(f"[AzetaBatch] applied {job['stock_applied']:,}/{len(all_quant_ids):,}")

            job["elapsed_apply_s"] = round(time.monotonic() - t0, 2)

        if job["status"] == "running":
            job["status"] = "completed"
            # Avanzar marker si usó marker (modo cron) y no hubo errores graves
            if use_marker and not test_isbn and max_ts_seen and \
                    job["stock_errors"] == 0:
                _advance_azeta_marker(max_ts_seen, job["processed"])
                job["marker_advanced_to"] = str(max_ts_seen)
                print(f"[AzetaBatch] Marker avanzado a {max_ts_seen}")
        job["stage"] = "done"
        job["elapsed_s"] = round(time.monotonic() - t_start, 2)
        print(f"[AzetaStockPush] DONE proc={job['processed']:,} "
              f"ok={job['stock_written']:,} err={job['stock_errors']:,} "
              f"en {job['elapsed_s']}s")
        try:
            import audit_log
            audit_log.log_event(
                "azeta_stock_push", "push_done",
                f"Actualizados {job['stock_written']:,} quants en AZE01 "
                f"({job['processed']:,} procesados, {job['stock_errors']} errores, "
                f"{job['elapsed_s']}s, modo {job.get('mode')})",
                detalle={k: job.get(k) for k in ("processed", "stock_written",
                         "stock_created", "stock_updated", "stock_errors",
                         "stock_no_product", "mode", "marker", "elapsed_s")},
                nivel="error" if job["stock_errors"] > 0 else "info",
            )
        except Exception:
            pass
    except Exception as e:
        job["status"] = "error"
        err = f"{type(e).__name__}: {e!r}"
        job["errors"].append(err[:300])
        print(f"[AzetaStockPush] Fatal: {err}")
        try:
            import audit_log
            audit_log.log_event("azeta_stock_push", "push_error",
                                err[:300], nivel="error")
        except Exception:
            pass

    return job


# ─────────────────────────────────────────────────────────────────────
# APAGADO POR AUSENCIA AZETA (acordado con Server A 2026-07-16)
#
# El CSV de stock de AZETA es "solo-stock": si un ISBN esta, hay stock;
# si NO viene, esta agotado. El fetcher (azeta_stock._upsert_batch) pone
# stock_actualizado_en = NOW() SIEMPRE para TODOS los presentes del CSV
# (cambien o no) — a diferencia del upsert n8n de SINLI. Por eso aqui SI
# tenemos registro de presencia por identidad: presentes de la ultima
# corrida = stock_actualizado_en dentro de la ventana de esa corrida.
#
# GUARDAS (todas abortan el apagado, nunca el ciclo):
# 1. Frescura: ultima corrida > AZETA_ABS_FRESCURA_H horas -> abortar
# 2. Completitud ABSOLUTA: presentes < AZETA_ABS_MIN_PRESENTES -> abortar
#    (proteccion contra CSV truncado o fetch muerto a medias: el fetch
#    del 14/07 murio al 82% — 214k de 262k; con esta guarda un apagado
#    contra esa corrida habria abortado)
# 3. Tope de apagado: ausentes > AZETA_ABS_MAX_APAGADO_PCT % del universo
#    en Odoo -> abortar (algo huele mal, revisar a mano)
# ─────────────────────────────────────────────────────────────────────

AZETA_ABS_VENTANA_H = float(os.environ.get("AZETA_ABS_VENTANA_H", "3"))
AZETA_ABS_FRESCURA_H = float(os.environ.get("AZETA_ABS_FRESCURA_H", "6"))
AZETA_ABS_MIN_PRESENTES = int(os.environ.get("AZETA_ABS_MIN_PRESENTES", "250000"))
AZETA_ABS_MAX_APAGADO_PCT = float(os.environ.get("AZETA_ABS_MAX_APAGADO_PCT", "15"))

absence_job: dict | None = None


def get_absence_status() -> dict:
    job = dict(absence_job) if absence_job else {"status": "idle"}
    if "errors" in job:
        job["errors"] = job["errors"][-15:]
    if "ausentes_sample" in job:
        job["ausentes_sample"] = job["ausentes_sample"][:50]
    return job


def stop_absence():
    global absence_job
    if absence_job and absence_job.get("status") == "running":
        absence_job["status"] = "stopped"
        return True
    return False


def _azeta_presentes() -> tuple[set[str], str | None]:
    """ISBNs del ultimo CSV completo (ventana respecto al MAX)."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT MAX(stock_actualizado_en) FROM libros_proveedor
            WHERE proveedor_email = ?
        """, (AZETA_PROVEEDOR_EMAIL,))
        r = cur.fetchone()
        max_ts = r[0] if r else None
        if not max_ts:
            return set(), None
        db.execute_query(cur, f"""
            SELECT isbn FROM libros_proveedor
            WHERE proveedor_email = ?
              AND isbn IS NOT NULL
              AND stock_actualizado_en >= (
                  (SELECT MAX(stock_actualizado_en) FROM libros_proveedor
                   WHERE proveedor_email = ?)
                  - INTERVAL '{AZETA_ABS_VENTANA_H} hours'
              )
        """, (AZETA_PROVEEDOR_EMAIL, AZETA_PROVEEDOR_EMAIL))
        return {row[0] for row in cur.fetchall()}, str(max_ts)
    finally:
        conn.close()


async def run_azeta_absence_shutdown(dry_run: bool = True) -> dict:
    """
    Apagado por ausencia AZETA: libros con stock > 0 en AZE01 (Odoo) que
    NO vinieron en el ultimo CSV de stock -> inventory_quantity = 0.
    Solo location 14. dry_run=True por defecto (obligatorio validar antes).
    """
    global absence_job
    absence_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "stage": "starting",
        "dry_run": dry_run,
        "csv_max_ts": None,
        "presentes": 0,
        "con_stock_odoo": 0,
        "ausentes": 0,
        "apagados": 0,
        "salvaguarda_activada": False,
        "salvaguarda_motivo": None,
        "ausentes_sample": [],
        "errors": [],
        "elapsed_s": 0,
    }
    job = absence_job
    t_start = time.monotonic()

    try:
        # 1. Presentes del ultimo CSV
        job["stage"] = "reading_csv_presence"
        presentes, max_ts = _azeta_presentes()
        job["csv_max_ts"] = max_ts
        job["presentes"] = len(presentes)
        if not presentes:
            raise RuntimeError("Sin datos de presencia AZETA")

        # GUARDA 1: frescura
        from datetime import timezone as _tz
        max_dt = datetime.fromisoformat(max_ts)
        if max_dt.tzinfo is not None:
            max_dt = max_dt.astimezone(_tz.utc).replace(tzinfo=None)
        age_h = (datetime.utcnow() - max_dt).total_seconds() / 3600
        job["csv_age_hours"] = round(age_h, 1)
        if age_h > AZETA_ABS_FRESCURA_H:
            job["salvaguarda_activada"] = True
            job["salvaguarda_motivo"] = (
                f"CSV viejo ({age_h:.0f}h > {AZETA_ABS_FRESCURA_H:.0f}h). "
                "Apagado omitido.")

        # GUARDA 2 (CRITICA): completitud absoluta
        if not job["salvaguarda_activada"] and \
                len(presentes) < AZETA_ABS_MIN_PRESENTES:
            job["salvaguarda_activada"] = True
            job["salvaguarda_motivo"] = (
                f"CSV incompleto: {len(presentes):,} presentes < "
                f"{AZETA_ABS_MIN_PRESENTES:,} (linea base ~262k). "
                "Posible CSV truncado o fetch parcial. Apagado ABORTADO.")

        # 2. Universo: quants con stock en AZE01
        job["stage"] = "reading_odoo_stock"
        from sync_stock_sinli import _odoo_isbns_con_stock
        async with OdooClient() as odoo:
            isbn_quants = await _odoo_isbns_con_stock(odoo, AZE01_LOCATION_ID)
            job["con_stock_odoo"] = len(isbn_quants)

            # 3. Ausentes
            ausentes = set(isbn_quants.keys()) - presentes
            job["ausentes"] = len(ausentes)
            job["ausentes_sample"] = sorted(ausentes)[:100]

            # GUARDA 3: tope de apagado
            if not job["salvaguarda_activada"] and job["con_stock_odoo"] > 0:
                pct = 100 * len(ausentes) / job["con_stock_odoo"]
                job["ausentes_pct"] = round(pct, 1)
                if pct > AZETA_ABS_MAX_APAGADO_PCT:
                    job["salvaguarda_activada"] = True
                    job["salvaguarda_motivo"] = (
                        f"Apagado excesivo: {len(ausentes):,} ausentes = "
                        f"{pct:.0f}% del stock en AZE01 (tope "
                        f"{AZETA_ABS_MAX_APAGADO_PCT:.0f}%). Revisar a mano.")

            # 4. Apagar
            if dry_run:
                job["stage"] = "dry_run_done"
            elif job["salvaguarda_activada"]:
                job["stage"] = "skipped_by_safeguard"
            else:
                job["stage"] = "apagando"
                quant_ids = [qid for isbn in ausentes
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
                        print(f"[AzetaAbs] {err}")

        if job["status"] == "running":
            job["status"] = "completed"
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e!r}"[:300])
        print(f"[AzetaAbs] Fatal: {e!r}")
    finally:
        job["elapsed_s"] = round(time.monotonic() - t_start, 2)
        resumen = (
            f"Apagado AZETA{' [DRY RUN]' if dry_run else ''}: "
            f"{job['presentes']:,} presentes en CSV, "
            f"{job['con_stock_odoo']:,} con stock en AZE01, "
            f"{job['ausentes']:,} ausentes"
            f"{', ' + str(job['apagados']) + ' apagados' if not dry_run else ''}. "
            f"Salvaguarda: "
            f"{job['salvaguarda_motivo'] if job['salvaguarda_activada'] else 'OK'}"
        )
        print(f"[AzetaAbs] {resumen}")
        try:
            import audit_log
            audit_log.log_event(
                "azeta_stock_push",
                "absence_dry_run" if dry_run else "absence_shutdown",
                resumen,
                detalle={k: job.get(k) for k in (
                    "presentes", "con_stock_odoo", "ausentes", "apagados",
                    "ausentes_pct", "salvaguarda_activada",
                    "salvaguarda_motivo", "csv_max_ts", "csv_age_hours",
                    "dry_run", "elapsed_s")},
                nivel="error" if (job["status"] == "error"
                                   or job["salvaguarda_activada"]) else "info",
            )
        except Exception:
            pass

    return job


# ─────────────────────────────────────────────────────────────────────
# CRON: fetcher CSV AZETA -> libros_proveedor -> push stock a Odoo
# ─────────────────────────────────────────────────────────────────────

CRON_INTERVAL_S = int(os.environ.get("AZETA_STOCK_CRON_INTERVAL_S", "3600"))

_cron_task: asyncio.Task | None = None
_cron_state: dict = {
    "enabled": False,
    "interval_s": CRON_INTERVAL_S,
    "last_run_at": None,
    "last_run_status": None,
    "last_fetcher_summary": None,
    "last_push_summary": None,
    "next_run_at": None,
    "runs_total": 0,
    "errors": [],
}


def get_cron_status() -> dict:
    out = dict(_cron_state)
    if "errors" in out:
        out["errors"] = out["errors"][-10:]
    out["task_running"] = bool(_cron_task and not _cron_task.done())
    return out


async def run_full_stock_cycle() -> dict:
    """
    Ciclo completo: descarga CSV de stock AZETA -> libros_proveedor ->
    push stock.quant a Odoo AZE01.
    """
    import azeta_stock  # import local: evita ciclo si alguien importa al reves

    summary = {"fetcher": None, "push": None,
               "started_at": datetime.now().isoformat()}
    try:
        fetcher_res = await azeta_stock.run_azeta_sync()
        summary["fetcher"] = {
            "status": fetcher_res.get("status"),
            "isbns_unique": fetcher_res.get("isbns_unique", 0),
            "stock_total": fetcher_res.get("stock_total", 0),
            "updated_changed": fetcher_res.get("updated_changed", 0),
            "elapsed_s": fetcher_res.get("elapsed_total_s", 0),
        }
        if fetcher_res.get("status") == "error":
            summary["error"] = "Fetcher AZETA fallo"
            return summary
    except Exception as e:
        summary["error"] = f"Fetcher: {type(e).__name__}: {e!r}"
        return summary

    try:
        # Cron usa marker incremental: solo procesa libros cuyo stock cambió
        # desde el último ciclo (típicamente <2k libros), evita reprocesos
        push_res = await run_azeta_stock_push_only(use_marker=True)
        summary["push"] = {
            "status": push_res.get("status"),
            "processed": push_res.get("processed", 0),
            "stock_written": push_res.get("stock_written", 0),
            "stock_errors": push_res.get("stock_errors", 0),
            "elapsed_s": push_res.get("elapsed_s", 0),
        }
    except Exception as e:
        summary["error"] = f"Push: {type(e).__name__}: {e!r}"

    return summary


async def _cron_loop():
    """Loop interno: ejecuta run_full_stock_cycle cada interval_s."""
    print(f"[StockCron] Arrancado, intervalo {_cron_state['interval_s']}s")
    while _cron_state["enabled"]:
        try:
            t0 = time.monotonic()
            res = await run_full_stock_cycle()
            elapsed = round(time.monotonic() - t0, 1)
            _cron_state["last_run_at"] = datetime.now().isoformat()
            _cron_state["last_run_status"] = "ok" if not res.get("error") else "error"
            _cron_state["last_fetcher_summary"] = res.get("fetcher")
            _cron_state["last_push_summary"] = res.get("push")
            _cron_state["runs_total"] += 1
            if res.get("error"):
                _cron_state["errors"].append(f"run #{_cron_state['runs_total']}: {res['error']}")
            print(f"[StockCron] Run #{_cron_state['runs_total']} OK en {elapsed}s")
            try:
                import audit_log
                f = res.get("fetcher") or {}
                p = res.get("push") or {}
                audit_log.log_event(
                    "cron", "azeta_stock_cycle",
                    f"Ciclo #{_cron_state['runs_total']}: recibidos "
                    f"{f.get('isbns_unique', 0):,} ISBNs, "
                    f"{f.get('updated_changed', 0)} cambiaron, "
                    f"push {p.get('stock_written', 0):,} quants en {elapsed}s",
                    detalle={"fetcher": f, "push": p, "elapsed_s": elapsed},
                    nivel="error" if res.get("error") else "info",
                )
            except Exception:
                pass
        except Exception as e:
            _cron_state["last_run_status"] = "error"
            err = f"{type(e).__name__}: {e!r}"
            _cron_state["errors"].append(err[:300])
            print(f"[StockCron] Fatal en ciclo: {err}")

        # Espera con check cada segundo (responsive al stop)
        from datetime import timedelta
        _cron_state["next_run_at"] = (
            datetime.now() + timedelta(seconds=_cron_state["interval_s"])
        ).isoformat()
        for _ in range(_cron_state["interval_s"]):
            if not _cron_state["enabled"]:
                break
            await asyncio.sleep(1)

    print("[StockCron] Detenido")
    _cron_state["next_run_at"] = None


def start_stock_cron() -> bool:
    """Arranca el cron task. False si ya estaba corriendo."""
    global _cron_task
    if _cron_task and not _cron_task.done():
        return False
    _cron_state["enabled"] = True
    _cron_state["errors"] = []
    try:
        _cron_task = asyncio.create_task(_cron_loop())
        return True
    except RuntimeError:
        # No hay event loop activo (ej. arranque fuera de FastAPI)
        _cron_state["enabled"] = False
        return False


def stop_stock_cron() -> bool:
    """Marca el cron para que termine. False si ya estaba detenido."""
    if not _cron_state["enabled"]:
        return False
    _cron_state["enabled"] = False
    return True


if __name__ == "__main__":
    # python azeta_push_odoo.py <isbn>     -> test 1 libro (push completo)
    # python azeta_push_odoo.py            -> push completo
    # python azeta_push_odoo.py stock      -> stock-only push
    # python azeta_push_odoo.py cycle      -> ciclo fetcher+push una vez
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "stock":
        asyncio.run(run_azeta_stock_push_only())
    elif arg == "cycle":
        asyncio.run(run_full_stock_cycle())
    else:
        asyncio.run(run_azeta_push(test_isbn=arg))
