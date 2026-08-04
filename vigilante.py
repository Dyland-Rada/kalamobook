"""
Vigilante: comprueba que el sistema esta vivo, arregla lo que es seguro
arreglar y deja constancia de lo que no puede tocar.

Es a proposito determinista, no una IA: lo que falla aqui es predecible y se
detecta con reglas. Un modelo que decida reiniciar cosas en produccion es un
riesgo que no compensa.

Lo que SI puede arreglar solo:
  - Un cron parado tras un reinicio -> lo arranca
  - Un lock del sync atascado por un deploy -> lo libera

Lo que solo puede reportar:
  - Un proveedor que dejo de mandar ficheros (el problema esta en Server A)
  - Odoo, Shopify o la base de datos caidos
  - Errores repetidos del sync

Cada revision queda en event_log, y las incidencias se ven en la pestana
Salud del panel. No manda nada a ningun sitio: se consulta cuando se
quiere, no interrumpe.
"""
import asyncio
import os
import time
from datetime import datetime

import db

INTERVALO_S = int(os.environ.get("VIGILANTE_INTERVALO_S", "43200"))  # 12 h
CEGALD_DIAS_AVISO = float(os.environ.get("VIGILANTE_CEGALD_DIAS", "3"))
LOCK_MIN_AVISO = int(os.environ.get("VIGILANTE_LOCK_MIN", "45"))
ERRORES_AVISO = int(os.environ.get("VIGILANTE_ERRORES", "50"))

OK, AVISO, ERROR = "ok", "aviso", "error"

_ultima: dict | None = None
_cron_task = None
_cron_state: dict = {
    "enabled": False, "interval_s": INTERVALO_S, "last_run_at": None,
    "runs_total": 0, "errors": [],
}
_estado_previo: dict[str, str] = {}


def get_estado() -> dict:
    return dict(_ultima) if _ultima else {"status": "sin_revisar"}


def get_cron_status() -> dict:
    out = dict(_cron_state)
    out["task_running"] = bool(_cron_task and not _cron_task.done())
    return out


def _chequeo(clave, titulo, estado, detalle, arreglado=None, accion=None):
    return {"clave": clave, "titulo": titulo, "estado": estado,
            "detalle": detalle, "arreglado": arreglado, "accion": accion}


# ─── Comprobaciones ──────────────────────────────────────────────────
def _revisar_crones(arreglar: bool) -> list[dict]:
    """Los tres crones que deben estar siempre en marcha."""
    salida = []
    definicion = [
        ("cron_azeta", "Cron de stock AZETA", "azeta_push_odoo",
         "get_cron_status", "start_stock_cron"),
        ("cron_sinli", "Cron del sync SINLI", "sync_stock_sinli",
         "get_cron_status", "start_cron"),
        ("cron_nuevos", "Cron de libros nuevos", "auto_scrape",
         "get_cron_status", "start_cron"),
    ]
    for clave, titulo, modulo, fn_estado, fn_arrancar in definicion:
        try:
            mod = __import__(modulo)
            est = getattr(mod, fn_estado)()
            vivo = bool(est.get("enabled") and est.get("task_running"))
            if vivo:
                salida.append(_chequeo(clave, titulo, OK, "en marcha"))
                continue
            arreglado = False
            if arreglar:
                try:
                    arreglado = bool(getattr(mod, fn_arrancar)())
                except Exception as e:
                    print(f"[Vigilante] no pude arrancar {clave}: {e}")
            salida.append(_chequeo(
                clave, titulo, OK if arreglado else ERROR,
                "estaba parado, lo he arrancado" if arreglado else "PARADO",
                arreglado=arreglado,
                accion=None if arreglado else "Arrancalo desde su tarjeta"))
        except Exception as e:
            salida.append(_chequeo(clave, titulo, ERROR,
                                   f"no se pudo consultar: {type(e).__name__}"))
    return salida


def _revisar_lock(arreglar: bool) -> dict:
    """Un lock cogido mucho rato suele ser un proceso muerto en un deploy."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT entidad, lock_activo,
                   EXTRACT(EPOCH FROM (NOW() - lock_desde))/60
            FROM sync_state WHERE lock_activo = true
        """)
        filas = cur.fetchall()
        atascados = [(r[0], int(r[2] or 0)) for r in filas
                     if (r[2] or 0) > LOCK_MIN_AVISO]
        if not atascados:
            return _chequeo("lock", "Locks de sincronizacion", OK,
                            "ninguno atascado" if not filas
                            else f"{len(filas)} en uso normal")
        nombres = ", ".join(f"{e} ({m} min)" for e, m in atascados)
        if arreglar:
            for entidad, _ in atascados:
                db.execute_query(cur, """
                    UPDATE sync_state SET lock_activo = false WHERE entidad = ?
                """, (entidad,))
            conn.commit()
            return _chequeo("lock", "Locks de sincronizacion", OK,
                            f"liberado: {nombres}", arreglado=True)
        return _chequeo("lock", "Locks de sincronizacion", ERROR,
                        f"atascado: {nombres}",
                        accion="Se libera solo, o desde aqui con Revisar ahora")
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return _chequeo("lock", "Locks de sincronizacion", ERROR,
                        f"{type(e).__name__}: {str(e)[:80]}")
    finally:
        conn.close()


def _revisar_proveedores() -> dict:
    """Proveedores que llevan dias sin mandar nada. Solo se puede avisar."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT m.nombre_proveedor,
                   EXTRACT(EPOCH FROM (NOW() - MAX(a.procesado_en)))/86400 AS dias
            FROM proveedor_almacen_odoo m
            LEFT JOIN sinli_auditoria a ON a.email_canonico = m.proveedor_email
            GROUP BY m.nombre_proveedor
            HAVING MAX(a.procesado_en) IS NOT NULL
               AND EXTRACT(EPOCH FROM (NOW() - MAX(a.procesado_en)))/86400
                   > {CEGALD_DIAS_AVISO}
            ORDER BY 2 DESC
        """)
        mudos = [(r[0], int(r[1])) for r in cur.fetchall()]
        if not mudos:
            return _chequeo("proveedores", "Entrada de ficheros", OK,
                            "todos han mandado algo estos dias")
        txt = ", ".join(f"{n} ({d}d)" for n, d in mudos[:6])
        return _chequeo("proveedores", "Entrada de ficheros", AVISO,
                        f"{len(mudos)} sin mandar nada: {txt}",
                        accion="El problema esta en la entrada (Server A), no aqui")
    except Exception as e:
        return _chequeo("proveedores", "Entrada de ficheros", AVISO,
                        f"no se pudo comprobar: {type(e).__name__}")
    finally:
        conn.close()


def _revisar_sync() -> dict:
    """Errores recientes del sync y pendientes acumulados."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT COUNT(*) FROM sync_errores
            WHERE creado_en > NOW() - INTERVAL '2 hours'
        """)
        errores = int(cur.fetchone()[0])
        if errores > ERRORES_AVISO:
            return _chequeo("sync", "Sync de stock", AVISO,
                            f"{errores:,} errores en las ultimas 2 horas",
                            accion="Mira los errores recientes en su tarjeta")
        return _chequeo("sync", "Sync de stock", OK,
                        "sin errores" if not errores else f"{errores} errores, normal")
    except Exception as e:
        return _chequeo("sync", "Sync de stock", AVISO,
                        f"no se pudo comprobar: {type(e).__name__}")
    finally:
        conn.close()


async def _revisar_odoo() -> dict:
    try:
        from odoo_client import OdooClient
        async with OdooClient() as o:
            n = await o.search_count("product.template", [["active", "=", True]])
        return _chequeo("odoo", "Conexion con Odoo", OK,
                        f"{n:,} productos activos")
    except Exception as e:
        return _chequeo("odoo", "Conexion con Odoo", ERROR,
                        f"{type(e).__name__}: {str(e)[:90]}",
                        accion="Sin Odoo no se puede sincronizar nada")


def _revisar_shopify() -> dict:
    try:
        import shopify_api as sa
        if not (sa.CLIENT_ID and sa.CLIENT_SECRET):
            return _chequeo("shopify", "Conexion con Shopify", AVISO,
                            "sin credenciales configuradas")
        return _chequeo("shopify", "Conexion con Shopify", OK,
                        f"{sa.contar_productos():,} productos en la tienda")
    except Exception as e:
        return _chequeo("shopify", "Conexion con Shopify", ERROR,
                        f"{type(e).__name__}: {str(e)[:90]}")


def _revisar_bd() -> dict:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        t0 = time.monotonic()
        db.execute_query(cur, "SELECT 1")
        cur.fetchone()
        ms = int((time.monotonic() - t0) * 1000)
        db.execute_query(cur, "SELECT pg_size_pretty(pg_database_size(current_database()))")
        tam = cur.fetchone()[0]
        return _chequeo("bd", "Base de datos", OK, f"{tam}, responde en {ms} ms")
    except Exception as e:
        return _chequeo("bd", "Base de datos", ERROR,
                        f"{type(e).__name__}: {str(e)[:90]}")
    finally:
        try: conn.close()
        except Exception: pass


# ─── Revision completa ───────────────────────────────────────────────
async def revisar(arreglar: bool = True) -> dict:
    """Pasa todas las comprobaciones. Devuelve el parte completo."""
    global _ultima
    t0 = time.monotonic()
    chequeos = []
    chequeos += _revisar_crones(arreglar)
    chequeos.append(_revisar_lock(arreglar))
    chequeos.append(_revisar_bd())
    chequeos.append(await _revisar_odoo())
    chequeos.append(_revisar_shopify())
    chequeos.append(_revisar_proveedores())
    chequeos.append(_revisar_sync())

    errores = [c for c in chequeos if c["estado"] == ERROR]
    avisos = [c for c in chequeos if c["estado"] == AVISO]
    arreglados = [c for c in chequeos if c.get("arreglado")]
    salud = ERROR if errores else (AVISO if avisos else OK)

    _ultima = {
        "status": "completado", "salud": salud,
        "cuando": datetime.now().isoformat(),
        "chequeos": chequeos,
        "resumen": {"ok": len(chequeos) - len(errores) - len(avisos),
                    "avisos": len(avisos), "errores": len(errores),
                    "arreglados": len(arreglados)},
        "elapsed_s": round(time.monotonic() - t0, 1),
    }

    cambios = _registrar_cambios(chequeos, arreglados)
    _ultima["cambios"] = cambios

    try:
        import audit_log
        audit_log.log_event(
            "vigilante", f"revision_{salud}",
            f"Vigilante: {len(errores)} errores, {len(avisos)} avisos, "
            f"{len(arreglados)} arreglados solos",
            detalle={"salud": salud, "cambios": cambios,
                     "problemas": [f"{c['titulo']}: {c['detalle']}"
                                   for c in errores + avisos]},
            nivel="error" if errores else "info")
    except Exception:
        pass
    return _ultima


def _registrar_cambios(chequeos: list[dict], arreglados: list[dict]) -> list[str]:
    """
    Guarda los cambios de estado para poder verlos en el panel. Solo se
    apunta lo que CAMBIA: si no, un proveedor mudo llenaria el historial de
    la misma linea cada revision.
    """
    global _estado_previo
    cambios = []
    for c in chequeos:
        previo = _estado_previo.get(c["clave"])
        if previo != c["estado"]:
            if c["estado"] == ERROR:
                cambios.append(f"ERROR — {c['titulo']}: {c['detalle']}")
            elif c["estado"] == AVISO:
                cambios.append(f"AVISO — {c['titulo']}: {c['detalle']}")
            elif previo in (ERROR, AVISO):
                cambios.append(f"RESUELTO — {c['titulo']}")
            _estado_previo[c["clave"]] = c["estado"]
    for c in arreglados:
        cambios.append(f"ARREGLADO — {c['titulo']}: {c['detalle']}")
    return cambios


# ─── Cron ────────────────────────────────────────────────────────────
async def _cron_loop():
    from datetime import timedelta
    print(f"[Vigilante] Arrancado, cada {_cron_state['interval_s']}s")
    while _cron_state["enabled"]:
        try:
            r = await revisar(arreglar=True)
            _cron_state["last_run_at"] = datetime.now().isoformat()
            _cron_state["runs_total"] += 1
            print(f"[Vigilante] revision #{_cron_state['runs_total']}: "
                  f"{r['salud']} ({r['resumen']})")
        except Exception as e:
            _cron_state["errors"].append(f"{type(e).__name__}: {e!r}"[:200])
            print(f"[Vigilante] fallo: {e!r}")
        _cron_state["next_run_at"] = (
            datetime.now() + timedelta(seconds=_cron_state["interval_s"])).isoformat()
        for _ in range(_cron_state["interval_s"]):
            if not _cron_state["enabled"]:
                break
            await asyncio.sleep(1)
    print("[Vigilante] Detenido")


def start_cron() -> bool:
    global _cron_task
    if _cron_task and not _cron_task.done():
        return False
    _cron_state["enabled"] = True
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


if __name__ == "__main__":
    r = asyncio.run(revisar(arreglar=False))
    print(f"SALUD: {r['salud']}  {r['resumen']}\n")
    for c in r["chequeos"]:
        icono = {"ok": "OK ", "aviso": "?? ", "error": "!! "}[c["estado"]]
        print(f"{icono}{c['titulo']:<28}{c['detalle']}")
