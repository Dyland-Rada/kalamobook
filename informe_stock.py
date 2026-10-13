"""
Excel con todos los libros que tienen stock y quien se lo puede servir.

Una fila por ISBN y una COLUMNA FIJA POR PROVEEDOR:

    ISBN | NOMBRE | PROVEEDORES | AZETA | DISTRIFORMA | PODIPRINT | ...

Columna fija y no PROVEEDOR_1/2/3 porque eso no se puede filtrar: para
sacar "los que tiene Distriforma" habria que mirar en todas las columnas,
ya que cada libro los lista en el orden que le toque. Asi, cada proveedor
tiene su columna y el autofiltro de Excel vale directamente.

El libro trae DOS HOJAS con los mismos libros y las mismas columnas:

  * "Disponibilidad" -- SI / NO. Para filtrar de un vistazo.
  * "Unidades"       -- cuantas hay. Para decidir a quien pedirle.

Las dos sirven para filtrar; la segunda ademas dice cuanto. Van juntas
porque son la misma consulta y separarlas obligaba a elegir.

`PROVEEDORES` es cuantos lo tienen, para ordenar por disponibilidad.

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


def get_letra(n: int) -> str:
    """1 -> A, 27 -> AA. Se define aqui para no importar openpyxl al cargar
    el modulo: solo hace falta cuando se genera."""
    letras = ""
    while n:
        n, resto = divmod(n - 1, 26)
        letras = chr(65 + resto) + letras
    return letras

CARPETA = os.environ.get("INFORME_STOCK_DIR", "/tmp")
_job: dict | None = None


def get_status() -> dict:
    return dict(_job) if _job else {"status": "idle"}


def _apodo(email: str) -> str:
    """
    Nombre de columna para un proveedor que no esta en la tabla.

    'sinli.disal@grupodismalibro.com' -> 'DISAL'. Se quita el prefijo del
    buzon tecnico, que no identifica a nadie.
    """
    # Buzones tecnicos: no identifican a nadie, asi que si el nombre se
    # queda en uno de estos tira del dominio, que si dice quien es.
    GENERICOS = {"sinli", "info", "pedidos", "envios", "ventas", "admin",
                 "contacto", "administracion", "documentos", "correo"}
    local, _, dominio = (email or "").partition("@")
    trozos = [t for t in local.split(".") if t and t.lower() not in GENERICOS]
    if trozos:
        return ".".join(trozos).upper()
    etiqueta = dominio.split(".")[0] if dominio else ""
    return (etiqueta or local or email).upper()


def _nombres_proveedor() -> dict[str, str]:
    """
    {buzon: nombre de columna} de TODO proveedor con stock.

    Se cruzan dos fuentes porque ninguna basta sola: proveedor_almacen_odoo
    da el nombre comercial pero no los tiene a todos —DISAL, con unos 12.000
    libros, no figura ahi y por eso se quedaba sin columna el 13/10/2026— y
    libros_proveedor los tiene a todos pero sin nombre. Asi no se cae nadie,
    y un proveedor nuevo aparece solo.
    """
    out: dict[str, str] = {}
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT DISTINCT proveedor_email
            FROM libros_proveedor WHERE stock_disponible > 0
        """)
        for r in cur.fetchall():
            if r[0]:
                out[r[0]] = _apodo(r[0])
        db.execute_query(cur, """
            SELECT proveedor_email, nombre_proveedor
            FROM proveedor_almacen_odoo
        """)
        for r in cur.fetchall():
            if r[0]:
                # A MAYUSCULAS: la tabla los tiene mezclados -- 'AZETA' y
                # 'DISBOOK' junto a 'Ediciones Akal' e 'ICARO DISTRIBUIDORA'
                # -- y como las columnas van ordenadas por el nombre, la
                # mezcla descolocaba el orden ademas de verse mal.
                out[r[0]] = (r[1] or r[0]).strip().upper()
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


def generar(ruta: str | None = None) -> dict:
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
        hoja_si = wb.create_sheet("Disponibilidad")
        hoja_uds = wb.create_sheet("Unidades")
        # Antes de volcar nada: en modo write_only las filas se escriben
        # segun llegan y fijarlo despues no tiene efecto.
        for h in (hoja_si, hoja_uds):
            h.freeze_panes = "C2"

        # Las columnas se fijan ANTES de leer: salen de la tabla de
        # proveedores, no de los datos. Asi el fichero tiene las mismas
        # columnas cada dia aunque un proveedor se quede sin stock, y se
        # pueden comparar dos informes sin que se descoloquen.
        columnas = sorted(set(nombres.values()))
        job["columnas"] = len(columnas)
        indice = {n: i for i, n in enumerate(columnas)}
        job["columnas_proveedor"] = columnas
        cabecera = ["ISBN", "NOMBRE", "PROVEEDORES", "UNIDADES"] + columnas

        # El formato se aplica SOLO a la cabecera y por reglas de hoja. Pintar
        # celda a celda serian 550.000 x 19 = 10 millones de objetos de estilo:
        # tardaria muchisimo y dejaria un fichero enorme. Las reglas
        # condicionales las guarda Excel una vez y las aplica al abrir.
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.formatting.rule import CellIsRule, ColorScaleRule

        VINO = "7A1F2B"
        fondo = PatternFill("solid", fgColor=VINO)
        letra = Font(bold=True, color="FFFFFF", size=10)
        raya = Border(bottom=Side(style="thin", color="FFFFFF"))

        for h in (hoja_si, hoja_uds):
            # Anchos y alto ANTES de volcar: write_only escribe la fila en
            # cuanto se hace append y despues ya no hay donde aplicarlos.
            h.row_dimensions[1].height = 42
            for col, ancho in (("A", 15), ("B", 48), ("C", 13), ("D", 10)):
                h.column_dimensions[col].width = ancho
            for i in range(len(columnas)):
                h.column_dimensions[get_letra(5 + i)].width = 13

            fila = []
            for i, titulo in enumerate(cabecera):
                c = WriteOnlyCell(h, value=titulo)
                c.fill, c.font, c.border = fondo, letra, raya
                c.alignment = Alignment(
                    horizontal="left" if i < 2 else "center",
                    vertical="center",
                    wrap_text=i >= 4, shrink_to_fit=False)
                fila.append(c)
            h.append(fila)

        # El ORDER BY agrupa las filas del mismo ISBN, asi que se acumula el
        # grupo que esta pasando y se vuelca al cambiar. Nunca hay mas de un
        # libro en memoria.
        actual = None
        nombre_actual = ""
        # NO y 0 por defecto, nunca vacio: una celda vacia no se distingue de
        # un fallo al generar, y en el autofiltro sale como "(Vacias)".
        marcas: list = []
        uds: list = []
        cuantos = 0
        unidades = 0
        sueltos: list[str] = []

        def volcar():
            if actual is None:
                return
            cabeza = [actual, nombre_actual, cuantos, unidades]
            hoja_si.append(cabeza + marcas)
            hoja_uds.append(cabeza + uds)
            job["isbns"] += 1
            if cuantos > job["max_proveedores"]:
                job["max_proveedores"] = cuantos

        for isbn, nombre, email, stock in cur:
            job["filas"] += 1
            if isbn != actual:
                volcar()
                actual, nombre_actual = isbn, nombre or ""
                marcas = ["NO"] * len(columnas)
                uds = [0] * len(columnas)
                cuantos = unidades = 0
            elif nombre and not nombre_actual:
                nombre_actual = nombre
            n_uds = int(stock or 0)
            cuantos += 1
            unidades += n_uds
            quien = nombres.get(email)
            if quien is None:
                # Proveedor sin fila en proveedor_almacen_odoo: no tiene
                # columna. Se cuenta igual y se avisa, que perderlo en
                # silencio seria peor.
                if email not in sueltos:
                    sueltos.append(email)
                continue
            i = indice[quien]
            marcas[i] = "SI"
            uds[i] = n_uds
        volcar()
        if sueltos:
            job["proveedores_sin_columna"] = sueltos

        cur.close()

        # Autofiltro en la cabecera y primeras columnas congeladas: sin esto
        # hay que activarlos a mano en cada informe, y con 19 columnas y
        # medio millon de filas se navega fatal.
        from openpyxl.utils import get_column_letter
        ultima = get_column_letter(4 + len(columnas))
        rango = f"A1:{ultima}{job['isbns'] + 1}"
        hoja_si.auto_filter.ref = rango
        hoja_uds.auto_filter.ref = rango

        # Color por regla, no por celda: una sola entrada en el fichero.
        primera = get_letra(5)
        datos = f"{primera}2:{ultima}{job['isbns'] + 1}"
        hoja_si.conditional_formatting.add(datos, CellIsRule(
            operator="equal", formula=['"SI"'],
            fill=PatternFill("solid", fgColor="C6EFCE"),
            font=Font(color="106B33", bold=True)))
        hoja_si.conditional_formatting.add(datos, CellIsRule(
            operator="equal", formula=['"NO"'],
            font=Font(color="C8C8C8")))
        # En unidades, escala de color: se ve de un vistazo donde hay mas.
        hoja_uds.conditional_formatting.add(datos, ColorScaleRule(
            start_type="num", start_value=0, start_color="FFFFFF",
            mid_type="percentile", mid_value=70, mid_color="DDF0DC",
            end_type="percentile", end_value=99, end_color="63BE7B"))

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
