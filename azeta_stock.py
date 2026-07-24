"""
Fetcher de stock de AZETA via CSV HTTP.

AZETA es proveedor especial: el stock NO llega por SINLI email (como los otros
11) sino que se descarga de un endpoint HTTP plano. Este modulo:

1. Descarga el CSV (EAN;cantidad)
2. Parsea con dedup (suma de qty si un EAN aparece varias veces)
3. UPSERT a libros_proveedor con patron IS DISTINCT FROM (actualizado_en solo
   se mueve si stock realmente cambio, igual que SINLI v3.1)
4. Salvaguarda: crea unique index (isbn, proveedor_email) si no existe

NO escribe stock.quant en Odoo. Eso es del sync (otro Claude) por contrato.
Yo solo lleno la tabla intermedia. Cuando el sync corra (cada hora), recoge
estos cambios y los lleva a Odoo AZE01 (lot_stock_id 14).

Cap a 50: si AZETA tiene >50 unidades, devuelve 50. El sync lo refleja como
50 en stock.quant; documentado en el contrato.
"""
import asyncio
import os
import time
from datetime import datetime
from typing import Any

import aiohttp

import db

AZETA_URL = "http://www.azetadistribuciones.es/servicios_web/stock.php"
AZETA_USER = os.environ.get("AZETA_USER", "120153")
AZETA_PASS = os.environ.get("AZETA_PASS", "jalta4b")
AZETA_PROVEEDOR_EMAIL = "info@azetadistribuciones.es"
AZETA_PROVEEDOR_ID = 1  # id en tabla proveedores (AZETA DISTRIBUCIONES)

# Estado del job en memoria (se resetea con redeploy — el estado real esta
# en la BD via stock_actualizado_en)
azeta_job: dict | None = None


def get_azeta_status() -> dict:
    job = dict(azeta_job) if azeta_job else {"status": "idle"}
    # Stats persistentes: cuantos libros AZETA con stock en BD
    job["azeta_in_db"] = _count_azeta_in_db()
    job["azeta_with_stock"] = _count_azeta_with_stock()
    job["azeta_total_units"] = _sum_azeta_stock()
    job["last_sync_in_db"] = _last_azeta_sync()
    if "errors" in job:
        job["errors"] = job["errors"][-10:]
    return job


def stop_azeta_sync():
    global azeta_job
    if azeta_job and azeta_job.get("status") == "running":
        azeta_job["status"] = "stopped"
        return True
    return False


def _count_azeta_in_db() -> int:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT COUNT(*) FROM libros_proveedor
            WHERE proveedor_email = ?
        """, (AZETA_PROVEEDOR_EMAIL,))
        return cur.fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


def _count_azeta_with_stock() -> int:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT COUNT(*) FROM libros_proveedor
            WHERE proveedor_email = ? AND stock_disponible > 0
        """, (AZETA_PROVEEDOR_EMAIL,))
        return cur.fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


def _sum_azeta_stock() -> int:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT COALESCE(SUM(stock_disponible), 0) FROM libros_proveedor
            WHERE proveedor_email = ?
        """, (AZETA_PROVEEDOR_EMAIL,))
        v = cur.fetchone()[0]
        return int(v) if v is not None else 0
    except Exception:
        return 0
    finally:
        conn.close()


def _last_azeta_sync() -> str | None:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT MAX(stock_actualizado_en) FROM libros_proveedor
            WHERE proveedor_email = ?
        """, (AZETA_PROVEEDOR_EMAIL,))
        row = cur.fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass
    finally:
        conn.close()
    return None


def _ensure_safeguards():
    """Crea el unique index (isbn, proveedor_email) si no existe.
    Salvaguarda recomendada por el otro Claude antes del primer fetch."""
    if not db.IS_POSTGRES:
        return  # SQLite local solo, no critico
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_libros_proveedor_isbn_email
            ON public.libros_proveedor (isbn, proveedor_email)
        """)
        conn.commit()
        print("[AZETA] Safeguard OK: unique index (isbn, proveedor_email) garantizado")
    except Exception as e:
        # Puede fallar si la tabla no existe aun (n8n no ha corrido), o por permisos
        print(f"[AZETA] Safeguard FAIL (no critico): {type(e).__name__}: {e}")
        try: conn.rollback()
        except Exception: pass
    finally:
        conn.close()


def _parse_csv(text: str) -> dict[str, int]:
    """Parsea EAN;cantidad. Dedup: suma qty si un EAN aparece varias veces.
    Retorna dict {isbn: stock_total}."""
    out: dict[str, int] = {}
    invalid = 0
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ";" not in ln:
            invalid += 1
            continue
        parts = ln.split(";", 1)
        if len(parts) < 2:
            invalid += 1
            continue
        ean = parts[0].strip()
        try:
            qty = int(parts[1].strip())
        except ValueError:
            invalid += 1
            continue
        if not ean.isdigit() or len(ean) not in (10, 13):
            invalid += 1
            continue
        # Dedup: si aparece varias veces, sumamos las qtys
        out[ean] = out.get(ean, 0) + qty
    return out


async def _download_csv(timeout_s: int = 120) -> str:
    """Descarga el CSV de AZETA. Levanta excepcion en error HTTP."""
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    params = {"fr_usuario": AZETA_USER, "fr_clave": AZETA_PASS}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(AZETA_URL, params=params) as resp:
            if resp.status != 200:
                raise RuntimeError(f"AZETA HTTP {resp.status}")
            return await resp.text()


def _upsert_batch(rows: list[tuple[str, int]]) -> dict:
    """UPSERT a libros_proveedor con patron IS DISTINCT FROM.
    actualizado_en solo se mueve si stock_disponible cambio.
    Retorna {inserted, updated_changed, updated_unchanged}.
    """
    if not rows:
        return {"inserted": 0, "updated_changed": 0, "updated_unchanged": 0}

    conn = db.get_connection()
    cur = conn.cursor()
    inserted = 0
    updated_changed = 0
    updated_unchanged = 0

    if db.IS_POSTGRES:
        # ON CONFLICT con IS DISTINCT FROM: actualizado_en SOLO si cambio
        # el stock; stock_actualizado_en siempre se mueve a NOW (refleja
        # que fetcher confirmo el stock).
        # Usamos xmax = 0 para distinguir INSERT vs UPDATE.
        # execute_values: UN solo round-trip por lote (antes era cur.execute
        # por fila = 257k round-trips → ~2.5h y bloqueo del event loop cuando
        # la BD tiene latencia de red. Con execute_values la latencia deja de
        # importar: el lote entero viaja en una llamada).
        from psycopg2.extras import execute_values
        sql = """
            INSERT INTO libros_proveedor
                (isbn, proveedor_email, proveedor_id, stock_disponible,
                 stock_actualizado_en, actualizado_en)
            VALUES %s
            ON CONFLICT (isbn, proveedor_email) DO UPDATE
            SET stock_disponible = EXCLUDED.stock_disponible,
                proveedor_id = COALESCE(libros_proveedor.proveedor_id, EXCLUDED.proveedor_id),
                stock_actualizado_en = NOW(),
                actualizado_en = CASE
                    WHEN libros_proveedor.stock_disponible IS DISTINCT FROM EXCLUDED.stock_disponible
                    THEN NOW()
                    ELSE libros_proveedor.actualizado_en
                END
            RETURNING (xmax = 0) AS was_insert,
                      (stock_actualizado_en = actualizado_en) AS stock_changed
        """
        template = "(%s, %s, %s, %s, NOW(), NOW())"
        args = [(isbn, AZETA_PROVEEDOR_EMAIL, AZETA_PROVEEDOR_ID, qty)
                for isbn, qty in rows]
        try:
            results = execute_values(cur, sql, args, template=template,
                                     page_size=len(args), fetch=True)
            for was_insert, stock_changed in results:
                if was_insert:
                    inserted += 1
                elif stock_changed:
                    updated_changed += 1
                else:
                    updated_unchanged += 1
            conn.commit()
        except Exception as e:
            try: conn.rollback()
            except Exception: pass
            print(f"[AZETA] UPSERT batch FAIL: {type(e).__name__}: {e}")
            raise
        finally:
            conn.close()
    else:
        # SQLite (test local) — version simplificada sin IS DISTINCT FROM
        try:
            for isbn, qty in rows:
                cur.execute("""
                    INSERT INTO libros_proveedor
                        (isbn, proveedor_email, stock_disponible,
                         stock_actualizado_en, actualizado_en)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (isbn, proveedor_email) DO UPDATE
                    SET stock_disponible = excluded.stock_disponible,
                        stock_actualizado_en = CURRENT_TIMESTAMP,
                        actualizado_en = CURRENT_TIMESTAMP
                """, (isbn, AZETA_PROVEEDOR_EMAIL, qty))
                updated_changed += 1
            conn.commit()
        except Exception as e:
            try: conn.rollback()
            except Exception: pass
            raise
        finally:
            conn.close()

    return {
        "inserted": inserted,
        "updated_changed": updated_changed,
        "updated_unchanged": updated_unchanged,
    }


async def run_azeta_sync(batch_size: int = 500) -> dict:
    """
    Ejecuta el sync completo de AZETA:
      1. Salvaguarda del unique index
      2. Descarga CSV
      3. Parsea con dedup
      4. UPSERT a libros_proveedor

    Idempotente. Llamar cada hora desde cron o boton UI.
    """
    global azeta_job
    azeta_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "stage": "starting",
        "downloaded_bytes": 0,
        "lines_total": 0,
        "lines_valid": 0,
        "lines_invalid": 0,
        "isbns_unique": 0,
        "duplicates_summed": 0,
        "stock_total": 0,
        "inserted": 0,
        "updated_changed": 0,
        "updated_unchanged": 0,
        "elapsed_download_s": 0,
        "elapsed_upsert_s": 0,
        "elapsed_total_s": 0,
        "errors": [],
    }
    job = azeta_job
    t_start = time.monotonic()

    try:
        # 1. Salvaguarda
        job["stage"] = "ensuring_safeguards"
        _ensure_safeguards()

        # 2. Descarga
        job["stage"] = "downloading"
        print(f"[AZETA] Descargando CSV de {AZETA_URL}...")
        t0 = time.monotonic()
        text = await _download_csv()
        job["elapsed_download_s"] = round(time.monotonic() - t0, 2)
        job["downloaded_bytes"] = len(text)
        job["lines_total"] = text.count("\n")
        print(f"[AZETA] Descarga OK: {len(text):,} bytes en {job['elapsed_download_s']}s")

        # Detectar 0 filas (credenciales caducadas / endpoint cambiado)
        if len(text) < 100:
            job["errors"].append(f"CSV sospechosamente pequeño ({len(text)} bytes). Posible credencial caducada.")
            job["status"] = "error"
            return job

        # 3. Parse
        job["stage"] = "parsing"
        isbn_map = _parse_csv(text)
        job["isbns_unique"] = len(isbn_map)
        job["stock_total"] = sum(isbn_map.values())
        # Calcular invalid/valid:
        # lines_total - lineas_validas distintas - duplicados sumados = invalidas
        # mas facil: contamos valid + invalid en _parse_csv si quisieramos exact
        # Aqui aproximamos: valid lines = lines con ';' y qty entero y len(EAN) ok
        # Pero tendriamos que parsear 2 veces. Mejor: stat aproximada
        job["lines_valid"] = sum(1 for ln in text.splitlines() if ln.strip() and ";" in ln)
        job["lines_invalid"] = job["lines_total"] - job["lines_valid"]
        job["duplicates_summed"] = max(0, job["lines_valid"] - len(isbn_map))
        print(f"[AZETA] Parseado: {len(isbn_map):,} ISBNs unicos, "
              f"{job['stock_total']:,} unidades totales, "
              f"{job['duplicates_summed']:,} duplicados sumados")

        if not isbn_map:
            job["errors"].append("0 ISBNs validos en CSV. Posible cambio de formato.")
            job["status"] = "error"
            return job

        # 4. UPSERT en batches
        job["stage"] = "upserting"
        rows = list(isbn_map.items())
        t1 = time.monotonic()
        for i in range(0, len(rows), batch_size):
            if job["status"] != "running":
                print("[AZETA] Detenido por usuario")
                break
            batch = rows[i:i + batch_size]
            try:
                res = _upsert_batch(batch)
                job["inserted"] += res["inserted"]
                job["updated_changed"] += res["updated_changed"]
                job["updated_unchanged"] += res["updated_unchanged"]
            except Exception as e:
                job["errors"].append(f"batch @ {i}: {type(e).__name__}: {str(e)[:100]}")
                continue
            done = i + len(batch)
            if done % 5000 == 0 or done == len(rows):
                print(f"[AZETA] UPSERT {done:,}/{len(rows):,} "
                      f"(ins:{job['inserted']} chg:{job['updated_changed']} same:{job['updated_unchanged']})")
            # Ceder el control al event loop entre lotes: _upsert_batch es
            # sincrono/bloqueante, sin esto uvicorn no atiende HTTP mientras
            # corre el sync (n8n y la web se cuelgan).
            await asyncio.sleep(0)

        job["elapsed_upsert_s"] = round(time.monotonic() - t1, 2)
        job["elapsed_total_s"] = round(time.monotonic() - t_start, 2)
        job["stage"] = "done"
        if job["status"] == "running":
            job["status"] = "completed"
        print(f"[AZETA] DONE: ins={job['inserted']} chg={job['updated_changed']} "
              f"same={job['updated_unchanged']} en {job['elapsed_total_s']}s")
        try:
            import audit_log
            audit_log.log_event(
                "azeta_stock_fetch", "csv_sync_done",
                f"Recibidos {job['isbns_unique']:,} ISBNs ({job['stock_total']:,} unid): "
                f"{job['inserted']} nuevos, {job['updated_changed']} cambiaron, "
                f"{job['updated_unchanged']} sin cambio",
                detalle={k: job[k] for k in ("isbns_unique", "stock_total",
                         "inserted", "updated_changed", "updated_unchanged",
                         "elapsed_total_s")},
            )
        except Exception:
            pass
    except Exception as e:
        job["status"] = "error"
        err = f"{type(e).__name__}: {e!r}"
        job["errors"].append(err[:300])
        print(f"[AZETA] Fatal: {err}")
        try:
            import audit_log
            audit_log.log_event("azeta_stock_fetch", "csv_sync_error",
                                err[:300], nivel="error")
        except Exception:
            pass

    return job