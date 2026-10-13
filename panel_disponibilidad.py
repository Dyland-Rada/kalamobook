"""
Resumen de disponibilidad por proveedor para el panel.

Una sola llamada que devuelve todo lo que pinta la pestaña: los totales de
arriba, la barra por proveedor y la tabla con su frescura. Se hace aqui y no
en el navegador para no encadenar cuatro peticiones y que la pagina se vea a
trozos.

CACHE de 60 segundos. El recuento de ISBN con un solo proveedor recorre las
~650.000 filas de libros_proveedor y tarda unos segundos; con el panel
refrescando cada medio minuto y varias pestañas abiertas eso seria una
consulta pesada en bucle. Sesenta segundos es suficiente para un dato que
cambia cuando llega un fichero, no cada instante.
"""
import time
from datetime import datetime

import db
import proveedores_admin

CACHE_S = 60
_cache: dict | None = None
_cache_en = 0.0

# Los mismos cortes que manda el feed. Vive aqui duplicado a sabiendas: el
# original esta en el CASE de los workflows de n8n y hasta que no se unifique
# en un solo sitio, lo honesto es que el panel diga lo que de verdad se
# aplica, no lo que nos gustaria.
CORTE_LARGO = {"sinli@podiprint.com", "fandite@distriforma.es"}
DIAS_LARGO = 7
DIAS_NORMAL = 5


def _cuando(v):
    """Los ficheros vienen como {'cuando': ...} o sueltos."""
    if isinstance(v, dict):
        v = v.get("cuando")
    return str(v) if v else None


def _horas(desde) -> float | None:
    if not desde:
        return None
    try:
        t = datetime.fromisoformat(str(desde)[:19])
    except (TypeError, ValueError):
        return None
    return round((datetime.now() - t).total_seconds() / 3600, 1)


def _totales() -> dict:
    """Libros distintos con stock, unidades, y cuantos dependen de uno solo."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT COUNT(*), SUM(n), COUNT(*) FILTER (WHERE n = 1)
            FROM (SELECT isbn, COUNT(*) AS n
                  FROM libros_proveedor
                  WHERE stock_disponible > 0
                  GROUP BY isbn) t
        """)
        r = cur.fetchone()
        return {
            "libros_con_stock": int(r[0] or 0),
            "lineas_proveedor": int(r[1] or 0),
            "un_solo_proveedor": int(r[2] or 0),
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _unidades() -> dict[str, int]:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT proveedor_email, SUM(stock_disponible)
            FROM libros_proveedor WHERE stock_disponible > 0
            GROUP BY proveedor_email
        """)
        return {r[0]: int(r[1] or 0) for r in cur.fetchall()}
    except Exception:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def resumen(forzar: bool = False) -> dict:
    global _cache, _cache_en
    if _cache and not forzar and (time.time() - _cache_en) < CACHE_S:
        out = dict(_cache)
        out["de_cache"] = True
        out["edad_s"] = round(time.time() - _cache_en)
        return out

    t0 = time.monotonic()
    provs = proveedores_admin.listar(con_stats=True)
    uds = _unidades()
    tot = _totales()

    filas = []
    for p in provs:
        email = p.get("proveedor_email")
        libros = int(p.get("con_stock_bd") or 0)
        fichero = _cuando(p.get("ultimo_fichero")) or _cuando(p.get("ultimo_cegald"))
        h = _horas(fichero)
        corte = DIAS_LARGO if email in CORTE_LARGO else DIAS_NORMAL
        if h is None:
            estado = "sin_datos"
        elif h >= corte * 24:
            estado = "caido"
        elif h >= corte * 24 * 0.6:
            estado = "retraso"
        else:
            estado = "ok"
        filas.append({
            "email": email,
            "nombre": p.get("nombre") or email,
            "almacen": p.get("warehouse_code"),
            "activo": bool(p.get("activo", True)),
            "libros": libros,
            "unidades": uds.get(email, 0),
            "en_odoo": int(p.get("en_odoo_con_stock") or 0),
            "ultimo_fichero": fichero,
            "horas": h,
            "corte_dias": corte,
            "estado": estado,
        })
    filas.sort(key=lambda x: -x["libros"])

    out = {
        "generado_en": datetime.now().isoformat(),
        "totales": {
            **tot,
            "proveedores": len([f for f in filas if f["libros"] > 0]),
            "unidades": sum(f["unidades"] for f in filas),
            "caidos": len([f for f in filas if f["estado"] == "caido"]),
            "retrasados": len([f for f in filas if f["estado"] == "retraso"]),
        },
        "proveedores": filas,
        "elapsed_s": round(time.monotonic() - t0, 2),
        "de_cache": False,
        "cache_s": CACHE_S,
    }
    _cache, _cache_en = out, time.time()
    return out
