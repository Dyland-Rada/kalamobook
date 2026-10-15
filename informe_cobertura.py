"""
Por que un libro se vende y otro no.

Nace de una pregunta de operaciones el 15/10/2026: en Shopify hay 747.861
fichas publicadas, el archivo de precios trae 589.448 ISBN y el catalogo
solo da 479.653 como vendibles. Los tres numeros son correctos y miden
cosas distintas, pero no habia forma de verlo libro a libro.

Este informe recorre el espejo entero de Odoo -los ~1,3 millones- y por
cada ISBN dice en que lista esta, en cual no, y cual es la primera
condicion que no cumple. Una fila por libro, ninguna fila escondida.

Las cuatro listas, de mas grande a mas chica:

    ESPEJO       todo lo que existe como producto en Odoo.
    SHOPIFY      lo que tiene ficha publicada. Nunca se vacia: una ficha
                 subida en marzo sigue ahi aunque hoy nadie la surta.
    PRECIOS      los que algun proveedor nos esta tarifando ahora mismo
                 (libros_proveedor con precio > 0). Es el archivo de
                 precios.
    VENDIBLES    los que ademas tienen existencias, proveedor que pueda
                 servir y un precio que pase los filtros.

Se genera en streaming con cursor del lado del servidor y hay que llamarlo
desde un hilo: son varios minutos de trabajo sincrono.

Excel no admite mas de 1.048.576 filas por hoja, y el espejo pasa de eso.
Por eso va repartido en hojas por situacion -se venden / no se venden /
sin proveedor-, que ademas es como se quiere leer. Si alguna hoja se
pasara del tope, se continua sola en otra.
"""
import os
import time
from datetime import datetime

import db
from informe_stock import get_letra, _podar

CARPETA = os.environ.get("INFORME_STOCK_DIR", "/tmp")
TOPE_HOJA = 1_000_000

UMBRAL = 2.90
BRECHA_MAX = 0.30

_job: dict | None = None


def get_status() -> dict:
    return dict(_job) if _job else {"status": "idle"}


COLUMNAS = [
    ("ISBN", 15),
    ("TITULO", 46),
    ("EN SHOPIFY", 11),
    ("PROVEEDORES QUE LO TIENEN", 13),
    ("PROVEEDORES QUE LE PONEN PRECIO", 14),
    ("PRECIO MAS BAJO QUE NOS MANDAN", 14),
    ("PVP MAS ALTO", 12),
    ("EN EL ARCHIVO DE PRECIOS", 12),
    ("UNIDADES PUBLICABLES", 12),
    ("PRECIO DE VENTA CALCULADO", 13),
    ("PRECIO DEL QUE SIRVE", 12),
    ("PROVEEDOR QUE SIRVE", 22),
    ("ULTIMA VEZ CONFIRMADO", 18),
    ("SE VENDE", 10),
    ("QUE LE FALTA", 60),
]

# Lo que se explica de cada columna en la hoja de instrucciones.
AYUDA = {
    "ISBN": "El codigo del libro. Es la clave de todo el informe.",
    "TITULO": "El titulo tal como esta en Odoo. Vacio significa que la ficha "
              "esta sin rellenar.",
    "EN SHOPIFY": "SI = tiene ficha publicada en la tienda. Ojo: una ficha "
                  "publicada no implica que hoy haya existencias.",
    "PROVEEDORES QUE LO TIENEN": "Cuantos proveedores lo declaran con "
                                 "existencias en su ultimo fichero.",
    "PROVEEDORES QUE LE PONEN PRECIO": "Cuantos nos mandan un importe para "
                                       "ese libro, tengan existencias o no.",
    "PRECIO MAS BAJO QUE NOS MANDAN": "El menor de esos importes.",
    "PVP MAS ALTO": "El mayor. De aqui sale el precio de venta.",
    "EN EL ARCHIVO DE PRECIOS": "SI = aparece en el Excel de precios. El "
                                "criterio es simple: que al menos un "
                                "proveedor le ponga precio hoy.",
    "UNIDADES PUBLICABLES": "Las que el catalogo deja ofertar. Un 0 con "
                            "existencias detras quiere decir que algo lo "
                            "esta frenando; la ultima columna dice que.",
    "PRECIO DE VENTA CALCULADO": "Lo que se cobraria en los marketplaces.",
    "PRECIO DEL QUE SIRVE": "El importe del proveedor que aporta las "
                            "existencias, que no siempre es el mas barato "
                            "de la lista.",
    "PROVEEDOR QUE SIRVE": "Quien lo surte.",
    "ULTIMA VEZ CONFIRMADO": "Cuando se vio por ultima vez en un fichero de "
                             "ese proveedor.",
    "SE VENDE": "SI = esta ofertado ahora mismo.",
    "QUE LE FALTA": "La PRIMERA condicion que no cumple. Solo se nombra "
                    "una: si falla la de mas arriba, las de abajo ni se "
                    "miran.",
}

HOJAS = [
    ("Se venden", "si",
     "Cumplen todo: alguien los tiene, ese alguien puede servirlos y el "
     "precio pasa los filtros."),
    ("No se venden", "no",
     "Algun proveedor los tiene o los tarifa, pero falta algo. La ultima "
     "columna dice que. Esta es la hoja donde hay trabajo."),
    ("Sin proveedor", "sin",
     "Ningun proveedor los tiene ni les pone precio hoy. Son la mayoria del "
     "espejo: fondo editorial antiguo, descatalogados y altas que nunca "
     "llegaron a surtirse. Los que ademas salen con SI en EN SHOPIFY son "
     "fichas publicadas en su dia que hoy no tienen quien las sirva."),
]


def _motivo(lineas, con_stock, en_cat, stock, coste, mk, pvp_max):
    """La primera condicion que no cumple, en el mismo orden que el catalogo."""
    if not lineas:
        return "sin", "Ningun proveedor lo tiene en su catalogo"
    if not con_stock:
        return "no", "Los proveedores que lo tienen lo declaran sin existencias"
    if not en_cat:
        return ("no", "Tiene existencias pero no llegan a Odoo: el proveedor "
                      "que lo sostiene no tiene almacen dado de alta o esta "
                      "en pausa")
    if stock > 0:
        return "si", ""
    if coste is None:
        return "no", "Ningun proveedor de los que pueden servirlo le pone precio"
    if coste < UMBRAL:
        return "no", f"El proveedor que sirve cobra {coste:.2f}, menos de 2,90"
    if mk is None:
        return "no", "No se ha podido calcular el precio de venta"
    if mk < UMBRAL:
        return "no", f"El precio de venta calculado sale a {mk:.2f}, menos de 2,90"
    if pvp_max and pvp_max > 0 and (pvp_max - coste) / pvp_max > BRECHA_MAX:
        return ("no", f"El PVP ({pvp_max:.2f}) y lo que cobra el proveedor "
                      f"({coste:.2f}) se separan mas de un 30%: hay que "
                      f"revisarlo a mano")
    return ("no", "Frenado por una venta fallida anterior, por unidades "
                  "reservadas de un pedido sin reponer, o porque otro "
                  "proveedor lo desmiente declarando cero")


def _cabecera(hoja):
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.cell import WriteOnlyCell
    fondo = PatternFill("solid", fgColor="7A1F2B")
    letra = Font(bold=True, color="FFFFFF", size=10)
    raya = Border(bottom=Side(style="thin", color="FFFFFF"))
    hoja.row_dimensions[1].height = 42
    fila = []
    for i, (t, _) in enumerate(COLUMNAS):
        c = WriteOnlyCell(hoja, value=t)
        c.fill, c.font, c.border = fondo, letra, raya
        c.alignment = Alignment(horizontal="left" if i < 2 else "center",
                                vertical="center", wrap_text=i >= 2)
        fila.append(c)
    hoja.append(fila)


class _Reparto:
    """
    Una hoja por situacion, que se continua sola si pasa del tope de Excel.

    En write_only los anchos hay que fijarlos ANTES de meter filas, asi que
    cada hoja nueva se prepara entera de golpe.
    """

    def __init__(self, wb):
        self.wb = wb
        self.hojas: dict[str, list] = {}
        self.cuenta: dict[str, int] = {}

    def _nueva(self, clave, titulo):
        h = self.wb.create_sheet(titulo[:31])
        for i, (_, ancho) in enumerate(COLUMNAS):
            h.column_dimensions[get_letra(i + 1)].width = ancho
        h.freeze_panes = "C2"
        _cabecera(h)
        tramo = [h, 0, titulo]
        self.hojas.setdefault(clave, []).append(tramo)
        return tramo

    def abre(self, clave, titulo):
        self._nueva(clave, titulo)

    def mete(self, clave, fila):
        tramos = self.hojas[clave]
        ultimo = tramos[-1]
        if ultimo[1] >= TOPE_HOJA:
            ultimo = self._nueva(clave, f"{tramos[0][2]} (cont. {len(tramos)})")
        ultimo[0].append(fila)
        ultimo[1] += 1
        self.cuenta[clave] = self.cuenta.get(clave, 0) + 1


CONSULTA = """
WITH prov AS (
    SELECT isbn,
           count(*)                                              AS lineas,
           count(*) FILTER (WHERE stock_disponible > 0)           AS con_stock,
           count(*) FILTER (WHERE precio_con_iva > 0)             AS con_precio,
           min(precio_con_iva) FILTER (WHERE precio_con_iva > 0)  AS min_precio,
           max(precio_con_iva) FILTER (WHERE precio_con_iva > 0)  AS max_precio
    FROM libros_proveedor
    GROUP BY isbn
),
shop AS (
    SELECT variant_barcode AS isbn
    FROM shopify_productos
    WHERE estado = 'publicado'
      AND variant_barcode IS NOT NULL AND variant_barcode <> ''
    GROUP BY variant_barcode
)
SELECT m.barcode,
       COALESCE(NULLIF(m.name, ''), ''),
       (s.isbn IS NOT NULL)                AS en_shopify,
       COALESCE(p.lineas, 0),
       COALESCE(p.con_stock, 0),
       COALESCE(p.con_precio, 0),
       p.min_precio,
       p.max_precio,
       (c.isbn IS NOT NULL)                AS en_catalogo,
       COALESCE(c.stock, 0),
       c.precio_marketplace,
       c.precio_coste,
       c.proveedor,
       c.confirmado_en
FROM odoo_books_mirror m
LEFT JOIN prov p ON p.isbn = m.barcode
LEFT JOIN shop s ON s.isbn = m.barcode
LEFT JOIN catalogo_publicable c ON c.isbn = m.barcode
WHERE m.barcode IS NOT NULL AND m.barcode <> ''
ORDER BY m.barcode
"""


def generar(ruta: str | None = None) -> dict:
    global _job
    from openpyxl import Workbook

    sello = datetime.now()
    ruta = ruta or os.path.join(CARPETA,
                                f"cobertura_kalamo_{sello:%Y%m%d_%H%M}.xlsx")
    _job = {"status": "running", "ruta": ruta, "started_at": sello.isoformat(),
            "espejo": 0, "en_shopify": 0, "en_precios": 0, "en_catalogo": 0,
            "vendibles": 0, "shopify_sin_precio": 0, "motivos": {},
            "errors": [], "elapsed_s": 0}
    job = _job
    t0 = time.monotonic()
    conn = None
    try:
        conn = db.get_connection()
        cur = conn.cursor(name="cobertura") if db.IS_POSTGRES else conn.cursor()
        if db.IS_POSTGRES:
            cur.itersize = 5000
        cur.execute(CONSULTA)

        wb = Workbook(write_only=True)
        guia = wb.create_sheet("Como leerlo")   # se rellena al final
        reparto = _Reparto(wb)
        for titulo, clave, _desc in HOJAS:
            reparto.abre(clave, titulo)

        motivos: dict[str, int] = {}
        while True:
            lote = cur.fetchmany(5000)
            if not lote:
                break
            for (isbn, titulo, en_shop, lineas, con_stock, con_precio,
                 minp, maxp, en_cat, stock, mk, coste, prov, conf) in lote:
                minp = float(minp) if minp is not None else None
                maxp = float(maxp) if maxp is not None else None
                mk = float(mk) if mk is not None else None
                coste = float(coste) if coste is not None else None
                stock = int(stock or 0)
                clave, falta = _motivo(lineas, con_stock, en_cat, stock,
                                       coste, mk, maxp)
                job["espejo"] += 1
                if en_shop:
                    job["en_shopify"] += 1
                    if not con_precio:
                        job["shopify_sin_precio"] += 1
                if con_precio:
                    job["en_precios"] += 1
                if en_cat:
                    job["en_catalogo"] += 1
                if clave == "si":
                    job["vendibles"] += 1
                else:
                    motivos[falta] = motivos.get(falta, 0) + 1
                reparto.mete(clave, [
                    isbn, (titulo or "")[:120],
                    "SI" if en_shop else None,
                    con_stock or None,
                    con_precio or None,
                    minp, maxp,
                    "SI" if con_precio else None,
                    stock or None,
                    mk, coste, prov,
                    conf.strftime("%Y-%m-%d") if conf else None,
                    "SI" if clave == "si" else None,
                    falta or None,
                ])

        job["motivos"] = dict(sorted(motivos.items(), key=lambda kv: -kv[1]))
        _guia(guia, job, reparto)
        wb.save(ruta)
        job["bytes"] = os.path.getsize(ruta)
        job["borrados"] = _podar(ruta)
        job["generado_en"] = sello.isoformat()
        job["status"] = "completed"
        print(f"[Cobertura] {job['espejo']:,} ISBN, "
              f"{job['vendibles']:,} vendibles")
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e}"[:300])
        print(f"[Cobertura] FAIL: {e!r}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        job["elapsed_s"] = round(time.monotonic() - t0, 2)
    return job


def _guia(hoja, job, reparto):
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.cell import WriteOnlyCell
    hoja.column_dimensions["A"].width = 38
    hoja.column_dimensions["B"].width = 92

    def f(a, b=None, neg=False, rel=None):
        fila = []
        for v in (a, b):
            c = WriteOnlyCell(hoja, value=v)
            c.alignment = Alignment(wrap_text=True, vertical="top")
            if neg:
                c.font = Font(bold=True, size=11)
            if rel:
                c.fill = PatternFill("solid", fgColor=rel)
            fila.append(c)
        hoja.append(fila)

    e = job["espejo"] or 1
    f("POR QUE UN LIBRO SE VENDE Y OTRO NO", None, True)
    f("Generado", datetime.now().strftime("%d/%m/%Y %H:%M"))
    f("")
    f("LAS CUATRO LISTAS", None, True, "F6F3F0")
    f("Espejo de Odoo", f"{job['espejo']:,} — todo lo que existe como "
                        f"producto.")
    f("Publicados en Shopify", f"{job['en_shopify']:,} "
      f"({job['en_shopify']*100/e:.1f}%) — tienen ficha en la tienda. Esta "
      f"lista no se vacia nunca: una ficha subida hace meses sigue ahi "
      f"aunque hoy no la surta nadie.")
    f("En el archivo de precios", f"{job['en_precios']:,} "
      f"({job['en_precios']*100/e:.1f}%) — algun proveedor les pone precio "
      f"hoy. Ese es el unico criterio del archivo de precios, y por eso no "
      f"coincide con Shopify.")
    f("Publicados pero sin precio hoy", f"{job['shopify_sin_precio']:,} — "
      f"estan en Shopify y NO en el archivo de precios. Esta es la "
      f"diferencia entre las dos listas, libro a libro: salen en la hoja "
      f"«Sin proveedor» con SI en la columna EN SHOPIFY.")
    f("En el catalogo", f"{job['en_catalogo']:,} — ademas llegaron a Odoo "
      f"con existencias de un proveedor que puede servir.")
    f("Se venden", f"{job['vendibles']:,} "
      f"({job['vendibles']*100/e:.1f}%) — pasan tambien los filtros de "
      f"precio.")
    f("")
    f("QUE LE FALTA A LOS DEMAS", None, True, "F6F3F0")
    for m, n in job["motivos"].items():
        f(f"{n:,}", m)
    f("")
    f("LAS HOJAS", None, True, "F6F3F0")
    for titulo, clave, desc in HOJAS:
        f(titulo, f"{reparto.cuenta.get(clave, 0):,} libros · {desc}")
    f("")
    f("LAS COLUMNAS", None, True, "F6F3F0")
    for i, (t, _ancho) in enumerate(COLUMNAS):
        f(f"{get_letra(i+1)} · {t}", AYUDA.get(t, ""))
    f("")
    f("UNA ACLARACION", None, True, "FFF4E0")
    f("", "Las casillas vacias son vacias a proposito: un 0 o un NO repetido "
         "en medio millon de filas no deja leer nada. Si una casilla no "
         "tiene nada, ese libro no cumple esa condicion.")
