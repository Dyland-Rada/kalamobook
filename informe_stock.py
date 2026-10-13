"""
Excel con todos los libros que tienen stock y quien se lo puede servir.

Una fila por ISBN:

    ISBN | NOMBRE | PROVEEDORES | PROVEEDOR_1 | PROVEEDOR_2 | ...

`PROVEEDORES` es cuantos lo tienen, para poder ordenar y filtrar en Excel
sin contar columnas a ojo. Las columnas PROVEEDOR_n llevan el nombre
comercial, no el buzon: 'DISTRIFORMA' en vez de 'fandite@distriforma.es'.

Es una FOTO del momento en que se genera. No lee catalogo_publicable sino
libros_proveedor, a proposito: el catalogo se queda con UN proveedor por
libro —el mas barato— y aqui hacen falta todos.

Dos cuidados que no son opcionales a esta escala:

  * Cursor del lado del servidor. Son ~550.000 ISBN y mas de 650.000 filas
    de proveedor; traerlas de golpe se come la memoria del contenedor.
  * openpyxl en modo write_only, que vuelca cada fila segun llega en vez de
    construir la hoja entera en RAM.

Y la generacion NO debe esperarse desde el bucle de uvicorn: es trabajo
sincrono de varios minutos. Quien la llame, que la mande a un hilo. El
13/10/2026 un cron que no lo hizo dejo el panel entero sin responder.
"""
import os
import time
from datetime import datetime

import db

CARPETA = os.environ.get("INFORME_STOCK_DIR", "/tmp")
_job: dict | None = None


def get_status() -> dict:
    return dict(_job) if _job else {"status": "idle"}


def _nombres_proveedor() -> dict[str, str]:
    """{buzon: nombre comercial}. Si falta, se queda el buzon."""
    out: dict[str, str] = {}
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT proveedor_email, nombre_proveedor
            FROM proveedor_almacen_odoo
        """)
        for r in cur.fetchall():
            if r[0]:
                out[r[0]] = (r[1] or r[0]).strip()
    except Exception as e:
        print(f"[InformeStock] sin nombres de proveedor: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def _podar(actual: str, conservar: int = 3) -> int:
    """
    Deja solo los N informes mas recientes. Cada uno pesa decenas de MB y
    uno al dia llena el disco en un mes sin que nadie lo note.
    """
    try:
        carpeta = os.path.dirname(actual) or "."
        mios = sorted(
            (os.path.join(carpeta, f) for f in os.listdir(carpeta)
             if f.startswith("stock_kalamo_") and f.endswith(".xlsx")),
            key=os.path.getmtime, reverse=True)
        borrados = 0
        for viejo in mios[conservar:]:
            if os.path.abspath(viejo) == os.path.abspath(actual):
                continue
            try:
                os.remove(viejo)
                borrados += 1
            except OSError:
                pass
        return borrados
    except Exception:
        return 0


def generar(ruta: str | None = None, max_columnas: int | None = None) -> dict:
    """
    Escribe el Excel y devuelve el resumen. Sincrona: llamar desde un hilo.
    """
    global _job
    from openpyxl import Workbook

    sello = datetime.now()
    ruta = ruta or os.path.join(
        CARPETA, f"stock_kalamo_{sello:%Y%m%d_%H%M}.xlsx")
    _job = {"status": "running", "started_at": sello.isoformat(),
            "stage": "consultando", "ruta": ruta, "isbns": 0, "filas": 0,
            "max_proveedores": 0, "errors": [], "elapsed_s": 0}
    job = _job
    t0 = time.monotonic()
    conn = None
    try:
        nombres = _nombres_proveedor()
        conn = db.get_connection()
        # Cursor con nombre = del lado del servidor en Postgres: psycopg2 va
        # trayendo de mil en mil en vez de materializar 650.000 filas aqui.
        if db.IS_POSTGRES:
            cur = conn.cursor(name="informe_stock")
            cur.itersize = 2000
        else:
            cur = conn.cursor()
        cur.execute("""
            SELECT lp.isbn,
                   COALESCE(NULLIF(m.name, ''), '') AS nombre,
                   lp.proveedor_email,
                   lp.stock_disponible
            FROM libros_proveedor lp
            LEFT JOIN odoo_books_mirror m ON m.barcode = lp.isbn
            WHERE lp.stock_disponible > 0
            ORDER BY lp.isbn, lp.proveedor_email
        """)

        job["stage"] = "escribiendo"
        wb = Workbook(write_only=True)
        hoja = wb.create_sheet("Stock")

        tope = max_columnas or int(os.environ.get("INFORME_STOCK_MAX_PROV", "8"))
        hoja.append(["ISBN", "NOMBRE", "PROVEEDORES"]
                    + [f"PROVEEDOR_{i}" for i in range(1, tope + 1)])

        # El ORDER BY agrupa las filas del mismo ISBN, asi que se acumula el
        # grupo que esta pasando y se vuelca al cambiar. Nunca hay mas de un
        # libro en memoria.
        actual = None
        nombre_actual = ""
        provs: list[str] = []

        def volcar():
            if actual is None:
                return
            fila = [actual, nombre_actual, len(provs)] + provs[:tope]
            fila += [None] * (3 + tope - len(fila))
            hoja.append(fila)
            job["isbns"] += 1
            if len(provs) > job["max_proveedores"]:
                job["max_proveedores"] = len(provs)

        for isbn, nombre, email, _stock in cur:
            job["filas"] += 1
            if isbn != actual:
                volcar()
                actual, nombre_actual, provs = isbn, nombre or "", []
            elif nombre and not nombre_actual:
                nombre_actual = nombre
            provs.append(nombres.get(email, email))
        volcar()

        cur.close()
        job["stage"] = "guardando"
        wb.save(ruta)
        job["bytes"] = os.path.getsize(ruta)
        job["borrados"] = _podar(ruta)
        job["generado_en"] = sello.isoformat()
        job["stage"] = "done"
        job["status"] = "completed"
        print(f"[InformeStock] {job['isbns']:,} ISBN en {ruta} "
              f"({job['bytes']:,} bytes)")
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e}"[:300])
        print(f"[InformeStock] Fatal: {e!r}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        job["elapsed_s"] = round(time.monotonic() - t0, 2)
    return job
