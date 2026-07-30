"""
Gestion de proveedores / almacenes: pausar, reactivar y dar de alta.

PAUSAR un proveedor (p.ej. cierran por verano):
  1. Se marca activo=false en proveedor_almacen_odoo.
  2. Se pone a 0 el stock de TODOS sus quants en SU almacen de Odoo.
Desde ese momento el sync SINLI (y el push AZETA) ignoran sus libros, asi
que aunque siga entrando su fichero no se vuelve a encender nada.

REACTIVAR: solo se quita la marca. NO se re-empuja stock: vuelve solo
cuando entre el proximo archivo de stock del proveedor (decision cliente
2026-07-30). Nota: el marcapaginas del sync avanzo durante la pausa, asi
que las filas viejas no se reprocesan — hace falta fichero nuevo.

ALTA: crea el almacen en Odoo (si no existe), guarda el mapeo
proveedor_email -> warehouse_code y corrige el nombre del proveedor.

El apagado usa el mismo workaround de Odoo v19 SaaS que el resto del
proyecto: action_apply_inventory lanza Fault pero ejecuta — se verifica
releyendo quantity.
"""
import asyncio
import os
import time
from datetime import datetime

import db
from odoo_client import OdooClient

AZETA_EMAIL = "info@azetadistribuciones.es"
CHUNK = int(os.environ.get("PROV_PAUSA_CHUNK", "500"))
PAGE = int(os.environ.get("PROV_PAUSA_PAGE", "40000"))
# Tope de reintentos individuales cuando falla un chunk entero (evita que
# un almacen con 100k quants rotos degenere en 100k llamadas sueltas).
MAX_FALLBACK_INDIVIDUAL = int(os.environ.get("PROV_PAUSA_MAX_FALLBACK", "200"))

_job: dict | None = None


def get_status() -> dict:
    job = dict(_job) if _job else {"status": "idle"}
    if "errors" in job:
        job["errors"] = job["errors"][-15:]
    return job


def stop() -> bool:
    if _job and _job.get("status") == "running":
        _job["status"] = "stopped"
        return True
    return False


# ─── Schema ───────────────────────────────────────────────────────────
# El estado de pausa vive en tabla PROPIA, no en columnas de
# proveedor_almacen_odoo: esa tabla es de supabase_admin y nuestro rol
# (postgres, sin superuser) no puede hacerle ALTER — solo INSERT/UPDATE.
TABLA_PAUSA = "proveedor_pausa"

_DDL = f"""
    CREATE TABLE IF NOT EXISTS {TABLA_PAUSA} (
        proveedor_email TEXT PRIMARY KEY,
        activo BOOLEAN NOT NULL DEFAULT TRUE,
        pausado_en TIMESTAMP,
        pausado_motivo TEXT,
        reactivado_en TIMESTAMP,
        actualizado_en TIMESTAMP
    )
"""

_schema_ok = False


def ensure_schema():
    """Crea la tabla de pausas. Idempotente, barato, sin migraciones."""
    global _schema_ok
    if _schema_ok:
        return
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        cur.execute(_DDL)
        conn.commit()
        _schema_ok = True
    except Exception as e:
        print(f"[ProvAdmin] ensure_schema FAIL: {e}")
        try: conn.rollback()
        except Exception: pass
    finally:
        conn.close()


def _columnas(tabla: str) -> set[str]:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = ?
        """, (tabla,))
        return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()
    finally:
        conn.close()


# ─── Lectura ──────────────────────────────────────────────────────────
def pausados() -> set[str]:
    """Emails de proveedores pausados. Lo consultan sync SINLI y push AZETA."""
    ensure_schema()
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT proveedor_email FROM {TABLA_PAUSA} WHERE activo = false
        """)
        return {r[0] for r in cur.fetchall() if r[0]}
    except Exception:
        return set()
    finally:
        conn.close()


def esta_pausado(proveedor_email: str) -> bool:
    return proveedor_email in pausados()


def _mapping(proveedor_email: str) -> dict | None:
    ensure_schema()
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT m.proveedor_email, m.warehouse_code, m.nombre_proveedor,
                   COALESCE(p.activo, true), p.pausado_en, p.pausado_motivo
            FROM proveedor_almacen_odoo m
            LEFT JOIN {TABLA_PAUSA} p ON p.proveedor_email = m.proveedor_email
            WHERE m.proveedor_email = ?
        """, (proveedor_email,))
        r = cur.fetchone()
        if not r:
            return None
        return {
            "proveedor_email": r[0], "warehouse_code": r[1],
            "nombre": r[2], "activo": bool(r[3]),
            "pausado_en": str(r[4]) if r[4] else None,
            "pausado_motivo": r[5],
        }
    finally:
        conn.close()


def listar(con_stats: bool = True) -> list[dict]:
    """
    Proveedores mapeados + su estado. con_stats anade lo que hay en BD
    (libros, cuantos con stock, cuando entro su ultimo archivo).
    """
    ensure_schema()
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT m.proveedor_email, m.warehouse_code, m.nombre_proveedor,
                   COALESCE(p.activo, true), p.pausado_en, p.pausado_motivo,
                   p.reactivado_en
            FROM proveedor_almacen_odoo m
            LEFT JOIN {TABLA_PAUSA} p ON p.proveedor_email = m.proveedor_email
            ORDER BY m.nombre_proveedor NULLS LAST, m.proveedor_email
        """)
        out = [{
            "proveedor_email": r[0],
            "warehouse_code": r[1],
            "nombre": r[2] or (r[0] or "").split("@")[0],
            "activo": bool(r[3]),
            "pausado_en": str(r[4]) if r[4] else None,
            "pausado_motivo": r[5],
            "reactivado_en": str(r[6]) if r[6] else None,
        } for r in cur.fetchall()]

        if con_stats and out:
            db.execute_query(cur, """
                SELECT proveedor_email, COUNT(*),
                       COUNT(*) FILTER (WHERE stock_disponible > 0),
                       MAX(stock_actualizado_en)
                FROM libros_proveedor
                GROUP BY proveedor_email
            """)
            stats = {r[0]: (int(r[1] or 0), int(r[2] or 0),
                            str(r[3]) if r[3] else None)
                     for r in cur.fetchall()}
            for p in out:
                n, con_stock, ultimo = stats.get(
                    p["proveedor_email"], (0, 0, None))
                p["libros_bd"] = n
                p["con_stock_bd"] = con_stock
                p["ultimo_archivo"] = ultimo
        return out
    finally:
        conn.close()


def sin_mapear() -> list[dict]:
    """
    Proveedores que mandan libros pero NO tienen almacen mapeado: su stock
    no llega a Odoo. Es la lista de candidatos a dar de alta.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT lp.proveedor_email, COUNT(*),
                   COUNT(*) FILTER (WHERE lp.stock_disponible > 0),
                   MAX(lp.stock_actualizado_en)
            FROM libros_proveedor lp
            LEFT JOIN proveedor_almacen_odoo m
                   ON m.proveedor_email = lp.proveedor_email
            WHERE m.proveedor_email IS NULL AND lp.proveedor_email IS NOT NULL
            GROUP BY lp.proveedor_email
            ORDER BY COUNT(*) DESC
        """)
        return [{
            "proveedor_email": r[0], "libros_bd": int(r[1] or 0),
            "con_stock_bd": int(r[2] or 0),
            "ultimo_archivo": str(r[3]) if r[3] else None,
        } for r in cur.fetchall()]
    except Exception as e:
        print(f"[ProvAdmin] sin_mapear FAIL: {e}")
        return []
    finally:
        conn.close()


# ─── Odoo helpers ─────────────────────────────────────────────────────
def _chunks(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


async def _location_de(odoo: OdooClient, warehouse_code: str) -> tuple[int, int]:
    """(warehouse_id, lot_stock_id) del almacen. Lanza si no existe."""
    rows = await odoo.search_read(
        "stock.warehouse", [["code", "=", warehouse_code]],
        ["id", "code", "name", "lot_stock_id"], limit=1)
    if not rows:
        raise RuntimeError(f"Almacen {warehouse_code} no existe en Odoo")
    lot = rows[0].get("lot_stock_id")
    lot_id = lot[0] if isinstance(lot, list) else lot
    if not lot_id:
        raise RuntimeError(f"Almacen {warehouse_code} sin lot_stock_id")
    return rows[0]["id"], int(lot_id)


async def _quants_con_stock(odoo: OdooClient, location_id: int) -> list[int]:
    """IDs de todos los quants con quantity > 0 en la location (paginado)."""
    out: list[int] = []
    offset = 0
    while True:
        page = await odoo.search_read(
            "stock.quant",
            [["location_id", "=", location_id], ["quantity", ">", 0]],
            ["id"], offset=offset, limit=PAGE, order="id")
        out.extend(q["id"] for q in page)
        if len(page) < PAGE:
            break
        offset += PAGE
    return out


async def _apagar_chunk(odoo: OdooClient, quant_ids: list[int]) -> bool:
    """inventory_quantity=0 + apply. True si quedo confirmado a 0."""
    await odoo.write("stock.quant", quant_ids, {"inventory_quantity": 0})
    try:
        await odoo.execute_kw("stock.quant", "action_apply_inventory",
                              [quant_ids])
        return True
    except Exception:
        # Fault "cannot marshal None" de Odoo v19 SaaS: ejecuta igual.
        verify = await odoo.read("stock.quant", quant_ids, ["quantity"])
        return all(abs(float(v.get("quantity") or 0)) < 0.01 for v in verify)


async def _apagar_todo(odoo: OdooClient, quant_ids: list[int], job: dict):
    fallback_usados = 0
    for chunk in _chunks(quant_ids, CHUNK):
        if job["status"] != "running":
            break
        try:
            if await _apagar_chunk(odoo, chunk):
                job["apagados"] += len(chunk)
                continue
            raise RuntimeError("apply no confirmo 0")
        except Exception as e:
            job["errors"].append(
                f"chunk {chunk[0]}..{chunk[-1]}: {type(e).__name__}: {str(e)[:120]}")
            # Reintento uno a uno: normalmente falla el chunk entero por
            # 1-2 quants de productos archivados.
            if fallback_usados >= MAX_FALLBACK_INDIVIDUAL:
                job["err_apagar"] += len(chunk)
                continue
            for qid in chunk:
                if fallback_usados >= MAX_FALLBACK_INDIVIDUAL:
                    job["err_apagar"] += 1
                    continue
                fallback_usados += 1
                try:
                    if await _apagar_chunk(odoo, [qid]):
                        job["apagados"] += 1
                    else:
                        job["err_apagar"] += 1
                except Exception:
                    job["err_apagar"] += 1
    job["fallback_individuales"] = fallback_usados


# ─── Pausar / reactivar ───────────────────────────────────────────────
def _marcar_pausa(proveedor_email: str, motivo: str | None):
    ensure_schema()
    conn = db.get_connection()
    cur = conn.cursor()
    ahora = "NOW()" if db.IS_POSTGRES else "CURRENT_TIMESTAMP"
    try:
        db.execute_query(cur, f"""
            INSERT INTO {TABLA_PAUSA}
                (proveedor_email, activo, pausado_en, pausado_motivo,
                 actualizado_en)
            VALUES (?, false, {ahora}, ?, {ahora})
            ON CONFLICT (proveedor_email) DO UPDATE
            SET activo = false, pausado_en = {ahora},
                pausado_motivo = EXCLUDED.pausado_motivo,
                actualizado_en = {ahora}
        """, (proveedor_email, motivo))
        conn.commit()
    finally:
        conn.close()


async def pausar(proveedor_email: str, dry_run: bool = True,
                 motivo: str | None = None) -> dict:
    """
    Pausa un proveedor: deja de sincronizarse y su stock en Odoo va a 0.
    dry_run=True solo cuenta cuantos quants se apagarian.
    """
    global _job
    ensure_schema()
    _job = {
        "status": "running",
        "accion": "pausar",
        "proveedor": proveedor_email,
        "dry_run": dry_run,
        "motivo": motivo,
        "started_at": datetime.now().isoformat(),
        "stage": "starting",
        "warehouse": None,
        "location_id": None,
        "con_stock": 0,
        "apagados": 0,
        "err_apagar": 0,
        "errors": [],
        "elapsed_s": 0,
    }
    job = _job
    t0 = time.monotonic()
    nombre = proveedor_email
    try:
        m = _mapping(proveedor_email)
        if not m:
            raise RuntimeError(
                f"{proveedor_email} no tiene almacen mapeado — nada que pausar")
        job["warehouse"] = m["warehouse_code"]
        nombre = m["nombre"] or proveedor_email

        async with OdooClient() as odoo:
            job["stage"] = "resolviendo_almacen"
            _, location_id = await _location_de(odoo, m["warehouse_code"])
            job["location_id"] = location_id

            job["stage"] = "leyendo_stock_odoo"
            quant_ids = await _quants_con_stock(odoo, location_id)
            job["con_stock"] = len(quant_ids)

            if dry_run:
                job["stage"] = "dry_run_done"
            else:
                # Marcar ANTES de apagar: si el sync corre a mitad del
                # apagado, ya no vuelve a encender nada de este proveedor.
                _marcar_pausa(proveedor_email, motivo)
                job["stage"] = "apagando"
                await _apagar_todo(odoo, quant_ids, job)
                job["stage"] = "done"

        if job["status"] == "running":
            job["status"] = "completed"
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e}"[:300])
        print(f"[ProvAdmin] pausar FAIL: {e!r}")
    finally:
        job["elapsed_s"] = round(time.monotonic() - t0, 2)
        _audit(
            "pausa_dry_run" if dry_run else "pausa",
            f"Pausar {nombre}{' [DRY RUN]' if dry_run else ''}: "
            f"{job['con_stock']:,} con stock en {job.get('warehouse')}, "
            f"{job['apagados']:,} apagados, {job['err_apagar']} errores "
            f"({job['elapsed_s']}s)",
            job, error=(job["status"] == "error" or job["err_apagar"] > 0))
    return job


def reactivar(proveedor_email: str, empujar: bool = True) -> dict:
    """
    Quita la pausa y deja su stock listo para volver a Odoo.

    empujar=True (default) marca sus libros para re-empuje. Es necesario:
    la ingesta solo mueve actualizado_en cuando el stock CAMBIA (medido
    2026-07-30: AKAL movio 1 fila de 3.808 en un dia), asi que esperar al
    proximo fichero dejaria a 0 para siempre todo lo que no cambie de
    cantidad — justo el caso de un proveedor que vuelve de vacaciones con
    el mismo stock, o de un print-on-demand con cantidad fija.
    """
    ensure_schema()
    m = _mapping(proveedor_email)
    if not m:
        return {"status": "error",
                "message": f"{proveedor_email} no tiene almacen mapeado"}
    conn = db.get_connection()
    cur = conn.cursor()
    ahora = "NOW()" if db.IS_POSTGRES else "CURRENT_TIMESTAMP"
    try:
        db.execute_query(cur, f"""
            INSERT INTO {TABLA_PAUSA}
                (proveedor_email, activo, reactivado_en, actualizado_en)
            VALUES (?, true, {ahora}, {ahora})
            ON CONFLICT (proveedor_email) DO UPDATE
            SET activo = true, reactivado_en = {ahora},
                pausado_motivo = NULL, actualizado_en = {ahora}
        """, (proveedor_email,))
        conn.commit()
    finally:
        conn.close()
    out = {"status": "ok", "proveedor": proveedor_email,
           "warehouse": m["warehouse_code"], "marcados": 0}
    if empujar:
        res = forzar_resync(proveedor_email)
        out["marcados"] = res.get("marcados", 0)
        if res.get("status") == "error":
            out["aviso"] = f"Reactivado, pero el re-empuje fallo: {res.get('message')}"
    out["nota"] = (f"{out['marcados']:,} libros marcados; el sync los sube en "
                   f"su proxima pasada (cron 1h).") if empujar else \
                  "Sin re-empuje: el stock solo volvera si cambia de cantidad."
    _audit("reactivacion",
           f"Reactivado {m['nombre']} ({m['warehouse_code']}): "
           f"{out['marcados']:,} libros marcados para re-empuje.", out)
    return out


def forzar_resync(proveedor_email: str) -> dict:
    """
    Marca los libros del proveedor como "cambiados ahora" para que el sync
    los vuelva a empujar sin esperar a su proximo fichero.

    Solo toca las filas cuyo ISBN YA existe en Odoo (las demas no las
    empujaria igualmente) y no cambia ningun stock ni precio: solo el
    timestamp que el sync usa como marcapaginas.

    Util tras dar de alta un almacen (las filas viejas quedaron detras del
    marcapaginas) o tras reactivar un proveedor si no quieres esperar.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    ahora = "NOW()" if db.IS_POSTGRES else "CURRENT_TIMESTAMP"
    try:
        db.execute_query(cur, f"""
            UPDATE libros_proveedor lp
            SET actualizado_en = {ahora}
            FROM odoo_books_mirror m
            WHERE m.barcode = lp.isbn
              AND lp.proveedor_email = ?
        """, (proveedor_email,))
        n = cur.rowcount
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return {"status": "error", "message": f"{type(e).__name__}: {e}"[:200]}
    finally:
        conn.close()
    out = {"status": "ok", "proveedor": proveedor_email, "marcados": n,
           "nota": "El sync los empujara en su proxima pasada (cron 1h) o "
                   "lanzando run-once."}
    _audit("forzar_resync",
           f"Re-empuje forzado de {proveedor_email}: {n:,} libros marcados "
           f"para la proxima pasada del sync.", out)
    return out


# ─── Alta de proveedor ────────────────────────────────────────────────
async def alta(proveedor_email: str, nombre: str, warehouse_code: str,
               warehouse_name: str | None = None,
               corregir_nombre_bd: bool = True) -> dict:
    """
    Da de alta un proveedor: crea su almacen en Odoo si no existe, guarda
    el mapeo y corrige el nombre en la tabla proveedores.
    Idempotente: si el almacen o el mapeo ya existen, los reutiliza.
    """
    ensure_schema()
    out: dict = {
        "status": "ok", "proveedor_email": proveedor_email,
        "nombre": nombre, "warehouse_code": warehouse_code,
        "almacen_creado": False, "mapeo_creado": False,
        "nombre_corregido": None,
    }
    async with OdooClient() as odoo:
        rows = await odoo.search_read(
            "stock.warehouse", [["code", "=", warehouse_code]],
            ["id", "name", "lot_stock_id"], limit=1)
        if rows:
            wh_id = rows[0]["id"]
        else:
            wh_id = await odoo.execute_kw("stock.warehouse", "create", [{
                "name": warehouse_name or nombre,
                "code": warehouse_code,
            }])
            out["almacen_creado"] = True
        wh = await odoo.read("stock.warehouse", [wh_id],
                             ["id", "name", "code", "lot_stock_id"])
        lot = wh[0].get("lot_stock_id")
        out["warehouse_id"] = wh_id
        out["warehouse_name"] = wh[0].get("name")
        out["location_id"] = lot[0] if isinstance(lot, list) else lot
        out["location_name"] = lot[1] if isinstance(lot, list) and len(lot) > 1 else None

    if out["location_id"] == 14:
        out["status"] = "error"
        out["message"] = "Anti-colision: location 14 es de AZETA"
        return out

    # Mapeo en BD (solo con las columnas que existan realmente)
    cols = _columnas("proveedor_almacen_odoo")
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT warehouse_code FROM proveedor_almacen_odoo
            WHERE proveedor_email = ?
        """, (proveedor_email,))
        existe = cur.fetchone()
        if existe:
            db.execute_query(cur, """
                UPDATE proveedor_almacen_odoo
                SET warehouse_code = ?, nombre_proveedor = ?
                WHERE proveedor_email = ?
            """, (warehouse_code, nombre, proveedor_email))
            out["mapeo_actualizado"] = True
            out["mapeo_anterior"] = existe[0]
        else:
            campos = ["proveedor_email", "warehouse_code"]
            valores = [proveedor_email, warehouse_code]
            if "nombre_proveedor" in cols:
                campos.append("nombre_proveedor")
                valores.append(nombre)
            if "proveedor_id" in cols:
                pid = _proveedor_id(proveedor_email)
                if pid is not None:
                    campos.append("proveedor_id")
                    valores.append(pid)
            ph = ", ".join("?" for _ in campos)
            db.execute_query(cur, f"""
                INSERT INTO proveedor_almacen_odoo ({", ".join(campos)})
                VALUES ({ph})
            """, tuple(valores))
            out["mapeo_creado"] = True
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        out["status"] = "error"
        out["message"] = f"mapeo: {type(e).__name__}: {e}"
        conn.close()
        return out
    finally:
        try: conn.close()
        except Exception: pass

    if corregir_nombre_bd:
        out["nombre_corregido"] = _corregir_nombre_proveedor(
            proveedor_email, nombre)

    _audit("alta_proveedor",
           f"Alta {nombre}: almacen {warehouse_code} "
           f"(location {out.get('location_id')}), mapeo "
           f"{proveedor_email} -> {warehouse_code}", out)
    return out


def _proveedor_id(proveedor_email: str) -> int | None:
    """id en la tabla proveedores, si esa tabla lo tiene mapeado por email."""
    cols = _columnas("proveedores")
    col_email = next((c for c in ("email", "proveedor_email", "correo",
                                  "email_sinli") if c in cols), None)
    if not col_email:
        return None
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur,
            f"SELECT id FROM proveedores WHERE {col_email} = ?",
            (proveedor_email,))
        r = cur.fetchone()
        return int(r[0]) if r else None
    except Exception:
        return None
    finally:
        conn.close()


def _corregir_nombre_proveedor(proveedor_email: str, nombre: str) -> str | None:
    """
    Corrige proveedores.nombre buscando por su columna de email. El nombre
    lo pone el parser del SINLI y a veces coge el del destinatario
    (PODIPRINT llego como 'KALAMO').
    """
    cols = _columnas("proveedores")
    if not cols or "nombre" not in cols:
        return None
    col_email = next((c for c in ("email", "proveedor_email", "correo",
                                  "email_sinli") if c in cols), None)
    if not col_email:
        return None
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT nombre FROM proveedores WHERE {col_email} = ?
        """, (proveedor_email,))
        r = cur.fetchone()
        if not r:
            return None
        anterior = r[0]
        if anterior == nombre:
            return anterior
        db.execute_query(cur, f"""
            UPDATE proveedores SET nombre = ? WHERE {col_email} = ?
        """, (nombre, proveedor_email))
        conn.commit()
        return f"{anterior} -> {nombre}"
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        print(f"[ProvAdmin] corregir_nombre FAIL: {e}")
        return None
    finally:
        conn.close()


# ─── Auditoria ────────────────────────────────────────────────────────
def _audit(evento: str, resumen: str, detalle: dict, error: bool = False):
    try:
        import audit_log
        audit_log.log_event("proveedores", evento, resumen,
                            detalle={k: v for k, v in detalle.items()
                                     if k != "errors"},
                            nivel="error" if error else "info")
    except Exception:
        pass


if __name__ == "__main__":
    import sys
    accion = sys.argv[1] if len(sys.argv) > 1 else "listar"
    if accion == "listar":
        for p in listar():
            print(p)
    elif accion == "pausar":
        print(asyncio.run(pausar(sys.argv[2],
                                 dry_run="--apply" not in sys.argv)))
    elif accion == "reactivar":
        print(reactivar(sys.argv[2]))
    elif accion == "alta":
        # python proveedores_admin.py alta <email> <nombre> <WH_CODE>
        print(asyncio.run(alta(sys.argv[2], sys.argv[3], sys.argv[4])))
