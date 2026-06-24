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

# Tope de libros por corrida (None = todos). Usar para tests con 1 libro.
DEFAULT_BATCH_SIZE = 200


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
                       max_books: int | None = None) -> list[dict]:
    """
    Lee odoo_id + barcode + qty (cap 50) cruzando mirror con
    libros_proveedor AZETA. Solo libros con odoo_id no nulo.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        if test_isbn:
            sql = """
                SELECT m.odoo_id, m.barcode, COALESCE(lp.stock_disponible, 0)
                FROM odoo_books_mirror m
                JOIN libros_proveedor lp ON lp.isbn = m.barcode
                WHERE lp.proveedor_email = ?
                  AND m.odoo_id IS NOT NULL
                  AND m.barcode = ?
                LIMIT 1
            """
            db.execute_query(cur, sql, (AZETA_PROVEEDOR_EMAIL, test_isbn))
        else:
            sql = """
                SELECT m.odoo_id, m.barcode, COALESCE(lp.stock_disponible, 0)
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
        out.append({"odoo_id": r[0], "barcode": r[1], "qty": qty})
    return out


async def run_azeta_stock_push_only(test_isbn: str | None = None,
                                     max_books: int | None = None) -> dict:
    """
    Push SOLO de stock.quant en AZE01 (location 14). No toca template
    ni categorias. Mucho mas rapido que run_azeta_push completo.

    test_isbn: procesa solo 1 ISBN (validacion)
    max_books: tope total
    """
    global stock_push_job
    stock_push_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "stage": "starting",
        "test_isbn": test_isbn,
        "total_to_process": 0,
        "processed": 0,
        "stock_written": 0,
        "stock_errors": 0,
        "stock_no_product": 0,
        "elapsed_s": 0,
        "errors": [],
    }
    job = stock_push_job
    t_start = time.monotonic()

    try:
        job["stage"] = "loading"
        targets = _read_stock_targets(test_isbn=test_isbn, max_books=max_books)
        job["total_to_process"] = len(targets)
        print(f"[AzetaStockPush] {len(targets):,} libros AZETA a actualizar stock")

        if not targets:
            job["status"] = "completed"
            job["stage"] = "done"
            return job

        job["stage"] = "pushing"
        async with OdooClient() as odoo:
            for t in targets:
                if job["status"] != "running":
                    break

                # Mismo workflow que _push_stock_quant pero con qty ya resuelto
                pid = await _get_product_id(odoo, t["odoo_id"])
                if not pid:
                    job["stock_no_product"] += 1
                    job["processed"] += 1
                    continue

                quants = await odoo.search_read(
                    "stock.quant",
                    [["product_id", "=", pid],
                     ["location_id", "=", AZE01_LOCATION_ID]],
                    ["id", "quantity"], limit=1,
                )

                try:
                    qty = t["qty"]
                    if quants:
                        quant_id = quants[0]["id"]
                        await odoo.write("stock.quant", [quant_id],
                                         {"inventory_quantity": qty})
                    else:
                        quant_id = await odoo.execute_kw(
                            "stock.quant", "create",
                            [{"product_id": pid,
                              "location_id": AZE01_LOCATION_ID,
                              "inventory_quantity": qty}],
                        )
                    try:
                        await odoo.execute_kw(
                            "stock.quant", "action_apply_inventory",
                            [[quant_id]],
                        )
                    except Exception as apply_err:
                        verify = await odoo.read(
                            "stock.quant", [quant_id], ["quantity"]
                        )
                        if not (verify and abs(
                            float(verify[0].get("quantity") or 0) - qty
                        ) < 0.01):
                            raise apply_err
                    job["stock_written"] += 1
                except Exception as e:
                    job["stock_errors"] += 1
                    err = f"pid={pid} isbn={t['barcode']}: {type(e).__name__}: {str(e)[:120]}"
                    job["errors"].append(err)

                job["processed"] += 1
                if job["processed"] % 200 == 0:
                    print(f"[AzetaStockPush] {job['processed']:,}/"
                          f"{job['total_to_process']:,} "
                          f"ok:{job['stock_written']} err:{job['stock_errors']}")

        if job["status"] == "running":
            job["status"] = "completed"
        job["stage"] = "done"
        job["elapsed_s"] = round(time.monotonic() - t_start, 2)
        print(f"[AzetaStockPush] DONE proc={job['processed']:,} "
              f"ok={job['stock_written']:,} err={job['stock_errors']:,} "
              f"en {job['elapsed_s']}s")
    except Exception as e:
        job["status"] = "error"
        err = f"{type(e).__name__}: {e!r}"
        job["errors"].append(err[:300])
        print(f"[AzetaStockPush] Fatal: {err}")

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
        push_res = await run_azeta_stock_push_only()
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
