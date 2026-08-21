"""
Generador de fichas de producto para Shopify.

Dado un ISBN devuelve las 23 columnas Matrixify. El reparto es lo que abarata
el proceso: la mitad de la ficha sale de datos que ya tenemos y solo la parte
redactada se le pide a DeepSeek.

Sin gastar tokens:
  - Ficha tecnica (autor, editorial, idioma, tema, coleccion, encuadernacion,
    fecha, paginas, peso)
  - cat:novedades          -> por ano de edicion (2025 o posterior)
  - cat:literatura-en-otros-idiomas -> por idioma
  - madre:                 -> se deduce de la categoria tematica elegida
  - precio, imagen, autor, ano, SKU, codigo de barras

Con DeepSeek (deepseek-chat):
  - Los 6 bloques narrativos del Body HTML
  - SEO Title y SEO Description
  - Las categorias tematicas, de una lista cerrada

Formato copiado de las 734.957 fichas ya publicadas, no inventado.
Spec: docs/superpowers/specs/2026-07-31-shopify-publicacion-design.md
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

import db
import pricing_engine

# Dos claves con relevo: si una falla se reintenta con la siguiente.
CLAVES = [k.strip() for k in os.environ.get("DEEPSEEK_API_KEYS", "").split(",")
          if k.strip()]
MODELO = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
URL_API = "https://api.deepseek.com/chat/completions"
TIMEOUT_S = int(os.environ.get("DEEPSEEK_TIMEOUT_S", "180"))

PESO_POR_DEFECTO = int(os.environ.get("SHOPIFY_PESO_DEFECTO", "350"))
ANIO_NOVEDAD = int(os.environ.get("SHOPIFY_ANIO_NOVEDAD", "2025"))
# Medido sobre las 734.957 fichas ya publicadas: la SEO Description va de 98
# a 160 caracteres (media 155) y ningun SEO Title pasa de 60. El tope de 160
# es el de Google, asi que es el que manda.
SEO_DESC_MIN, SEO_DESC_MAX = 120, 160
SEO_TITLE_MAX = 60

# Idiomas que NO llevan cat:literatura-en-otros-idiomas
IDIOMAS_ES = {"spa", "es", "castellano", "espanol", "español"}


# ─── Datos del libro ─────────────────────────────────────────────────
# Cada dato en cascada por las tres fuentes que tenemos: lo scrapeado de Casa
# del Libro (mirror.cdl_*), lo scrapeado a la tabla books, y el catalogo que
# manda el propio distribuidor. Solo con cdl_* la mitad de las fichas saldrian
# sin autor y sin editorial (19.107 de 38.171); con la cascada, el 100%.
_SELECT_LIBRO = """
    SELECT m.barcode,
           m.name,
           COALESCE(NULLIF(m.cdl_author,''),   NULLIF(b.author,''),
                    NULLIF(db.author,''))                      AS autor,
           COALESCE(NULLIF(m.cdl_editorial,''), NULLIF(m.gbooks_publisher,''),
                    NULLIF(b.editorial,''),     NULLIF(db.editorial,''))
                                                               AS editorial,
           COALESCE(NULLIF(m.cdl_language,''),  NULLIF(m.gbooks_language,''),
                    NULLIF(db.language,''))                    AS idioma,
           COALESCE(NULLIF(m.inferred_categories,''), NULLIF(db.category,'')) AS tema,
           COALESCE(NULLIF(m.cdl_collection,''), NULLIF(db.collection,''))    AS coleccion,
           COALESCE(NULLIF(m.cdl_binding,''),    NULLIF(db.binding,''))       AS encuadernacion,
           COALESCE(NULLIF(m.cdl_release_date,''), NULLIF(db.release_date,''),
                    NULLIF(db.edition_year,''))                AS fecha,
           COALESCE(NULLIF(m.cdl_pages,''), NULLIF(db.pages,''),
                    NULLIF(m.gbooks_pages::text,''))           AS paginas,
           COALESCE(NULLIF(m.cdl_weight,''), NULLIF(b.weight,''),
                    NULLIF(db.weight,''))                      AS peso,
           COALESCE(NULLIF(m.cdl_image_url,''), NULLIF(m.gbooks_thumbnail,''),
                    NULLIF(b.image_url,''), NULLIF(db.image_url,'')) AS imagen,
           COALESCE(NULLIF(m.description,''), NULLIF(b.description,''),
                    NULLIF(db.description,''))                 AS descripcion,
           m.list_price, m.pvp_base,
           (SELECT MAX(lp.precio_con_iva) FROM libros_proveedor lp
            WHERE lp.isbn = m.barcode AND lp.stock_disponible > 0) AS precio_cegald
    FROM odoo_books_mirror m
    LEFT JOIN books b             ON b.isbn  = m.barcode
    LEFT JOIN distributor_books db ON db.isbn = m.barcode
"""
_CAMPOS = ["barcode", "name", "autor", "editorial", "idioma", "tema",
           "coleccion", "encuadernacion", "fecha", "paginas", "peso",
           "imagen", "descripcion", "list_price", "pvp_base", "precio_cegald"]


def datos_libro(isbn: str) -> dict | None:
    """Todo lo que sabemos del libro, con cada campo resuelto en cascada."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, _SELECT_LIBRO + " WHERE m.barcode = ?", (isbn,))
        r = cur.fetchone()
        return dict(zip(_CAMPOS, r)) if r else None
    finally:
        conn.close()


def datos_libros(isbns: list[str]) -> dict[str, dict]:
    """Igual que datos_libro pero para un lote: una consulta en vez de N."""
    if not isbns:
        return {}
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        cur.execute(_SELECT_LIBRO + " WHERE m.barcode = ANY(%s)", (list(isbns),))
        return {r[0]: dict(zip(_CAMPOS, r)) for r in cur.fetchall()}
    finally:
        conn.close()


# ─── Conversiones ────────────────────────────────────────────────────
def peso_gramos(texto) -> int:
    """'210.0 gr' -> 210. Sin peso -> 350 g (media pactada con el cliente)."""
    if texto:
        m = re.search(r"([\d.,]+)", str(texto))
        if m:
            try:
                g = int(round(float(m.group(1).replace(",", "."))))
                if 1 <= g <= 20000:
                    return g
            except ValueError:
                pass
    return PESO_POR_DEFECTO


def anio_edicion(fecha) -> int | None:
    """'01/11/2019' o '2019-11-01' -> 2019."""
    if not fecha:
        return None
    m = re.search(r"(19|20)\d{2}", str(fecha))
    return int(m.group(0)) if m else None


def precio_web(d: dict) -> float | None:
    """
    Precio de venta. `list_price` ya lleva el suplemento API-15; si falta, se
    calcula desde el PVP crudo (pvp_base o el que mando el proveedor en su
    CEGALD, que es lo que actualiza Server A).
    """
    if d.get("list_price") and float(d["list_price"]) > 0:
        return round(float(d["list_price"]), 2)
    crudo = d.get("pvp_base") or d.get("precio_cegald")
    return pricing_engine.web_price(float(crudo)) if crudo else None


def imagen(d: dict) -> str:
    return (d.get("imagen") or "").strip()


def limpiar_xml(s) -> str:
    """
    Quita los caracteres de control que invalidan el XML 1.0. Un .xlsx es XML
    por dentro, asi que uno suelto en una sinopsis corrompe el fichero entero.
    """
    if s is None:
        return ""
    return "".join(
        c for c in str(s)
        if c in "\t\n\r" or "\x20" <= c <= "퟿"
        or "" <= c <= "�" or c >= "\U00010000"
    ).strip()


# ─── Taxonomia ───────────────────────────────────────────────────────
_tax_cache: dict | None = None


def taxonomia() -> dict:
    """
    {"cats": [...], "madre_de": {cat: madre}} aprendido de lo ya publicado:
    cada categoria tiene una madre dominante (89 de 97 por encima del 70%).
    """
    global _tax_cache
    if _tax_cache:
        return _tax_cache
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        # cur.execute sin parametros: con `LIKE 'cat:%'` psycopg2 tomaria el
        # % por un marcador si se le pasa la tupla de parametros.
        cur.execute("""
            WITH pares AS (
                SELECT trim(c) AS cat, trim(mm) AS madre
                FROM shopify_productos,
                     unnest(string_to_array(tags, ',')) AS c,
                     unnest(string_to_array(tags, ',')) AS mm
                WHERE trim(c) LIKE 'cat:%' AND trim(mm) LIKE 'madre:%')
            SELECT DISTINCT ON (cat) cat, madre, COUNT(*) AS n
            FROM pares GROUP BY cat, madre ORDER BY cat, n DESC
        """)
        filas = cur.fetchall()
        _tax_cache = {
            "cats": [r[0] for r in filas],
            "madre_de": {r[0]: r[1] for r in filas},
        }
        return _tax_cache
    finally:
        conn.close()


def cats_deterministas(d: dict) -> list[str]:
    """Las dos categorias que no hacen falta pensar: novedad e idioma."""
    fuera = []
    anio = anio_edicion(d.get("fecha"))
    if anio and anio >= ANIO_NOVEDAD:
        fuera.append("cat:novedades")
    idioma = (d.get("idioma") or "").strip().lower()
    if idioma and idioma not in IDIOMAS_ES:
        fuera.append("cat:literatura-en-otros-idiomas")
    return fuera


def montar_tags(cats_ia: list[str], d: dict) -> str:
    """madre + categorias, en el mismo orden que las fichas ya publicadas."""
    tax = taxonomia()
    validas = [c for c in cats_ia if c in tax["madre_de"]]
    madre = tax["madre_de"].get(validas[0]) if validas else None
    partes = ([madre] if madre else []) + validas
    for c in cats_deterministas(d):
        if c not in partes:
            partes.append(c)
    return ", ".join(partes)


# ─── Ficha tecnica (sin IA) ──────────────────────────────────────────
_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def _fecha_larga(fecha) -> str | None:
    """'01/11/2019' -> 'noviembre de 2019'."""
    if not fecha:
        return None
    s = str(fecha)
    m = re.match(r"(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})", s)
    if m:
        return f"{_MESES[int(m.group(2)) - 1]} de {m.group(3)}"
    anio = anio_edicion(s)
    return str(anio) if anio else None


def ficha_tecnica_html(d: dict) -> str:
    """Los 9 campos que salen de nuestros datos. Lo que falta, no se menciona."""
    idioma = (d.get("idioma") or "").strip()
    if idioma.lower() in IDIOMAS_ES:
        idioma = "Español"
    filas = [
        ("Autor", (d.get("autor") or "").replace("#", "; ")),
        ("Editorial", d.get("editorial")),
        ("Idioma", idioma),
        ("Tema", d.get("tema")),
        ("Colección", d.get("coleccion")),
        ("Encuadernación", d.get("encuadernacion")),
        ("Fecha de edición", _fecha_larga(d.get("fecha"))),
        ("Número de páginas", d.get("paginas")),
        ("Peso", f"{peso_gramos(d.get('peso'))} g"),
    ]
    lis = [f"<li><strong>{k}:</strong> {limpiar_xml(v)}</li>"
           for k, v in filas if v and str(v).strip()]
    if not lis:
        return ""
    return "<h3>Ficha técnica</h3>\n<ul>\n" + "\n".join(lis) + "\n</ul>"


# ─── DeepSeek ────────────────────────────────────────────────────────
SISTEMA = """Eres redactor de fichas de producto para una libreria online espanola.
Escribes SIEMPRE en espanol de Espana, en tercera persona, con tono editorial y comercial.
No inventas datos: si algo no aparece en la informacion dada, no lo mencionas.
Nada de superlativos vacios ni de formulas como "en este libro el lector encontrara".
Devuelves UNICAMENTE un JSON valido, sin markdown ni texto alrededor."""


def _prompt(d: dict, cats: list[str]) -> str:
    return f"""Datos del libro:
- Titulo: {d.get('name')}
- Autor: {(d.get('autor') or '').replace('#', '; ') or 'no consta'}
- Editorial: {d.get('editorial') or 'no consta'}
- Tema segun el distribuidor: {d.get('tema') or 'no consta'}
- Idioma: {d.get('idioma') or 'no consta'}
- Paginas: {d.get('paginas') or 'no consta'}
- Sinopsis: {(d.get('descripcion') or '')[:1800]}

Categorias tematicas permitidas (elige de 1 a 3, las mas precisas):
{", ".join(cats)}

Devuelve este JSON exacto:
{{
  "resumen": "parrafo de 60-90 palabras",
  "de_que_trata": "parrafo de 110-150 palabras",
  "temas_principales": ["5 vinetas de una linea"],
  "para_quien": "parrafo de 50-70 palabras",
  "que_aporta": ["4 o 5 vinetas de una linea"],
  "valoracion_editorial": "parrafo de 60-90 palabras",
  "seo_title": "maximo {SEO_TITLE_MAX} caracteres, con titulo y autor",
  "seo_description": "entre {SEO_DESC_MIN} y {SEO_DESC_MAX} caracteres, comercial, con llamada a la accion al final",
  "cats": ["cat:..."]
}}"""


class SinClavesError(RuntimeError):
    pass


def _llamar(mensajes: list[dict], intentos_por_clave: int = 2) -> dict:
    """
    Llama a DeepSeek probando las claves por orden. La lista de categorias va
    en el mensaje de sistema, que es identico en todas las llamadas, para que
    la cachee y no se pague la entrada cada vez.
    """
    if not CLAVES:
        raise SinClavesError("Falta DEEPSEEK_API_KEYS")
    payload = json.dumps({
        "model": MODELO, "messages": mensajes, "temperature": 0.7,
        "max_tokens": 2000, "response_format": {"type": "json_object"},
    }).encode()
    ultimo = None
    for clave in CLAVES:
        for intento in range(intentos_por_clave):
            req = urllib.request.Request(
                URL_API, data=payload, method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {clave}"})
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                ultimo = f"HTTP {e.code}"
                if e.code in (401, 402, 403):
                    break          # clave invalida o sin saldo: pasa a la otra
                time.sleep(2 ** intento)
            except Exception as e:
                ultimo = f"{type(e).__name__}"
                time.sleep(2 ** intento)
    raise RuntimeError(f"DeepSeek no respondio ({ultimo})")


def _recortar(texto: str, tope: int) -> str:
    """Recorta por la ultima palabra entera y deja la puntuacion presentable."""
    texto = str(texto or "").strip()
    if len(texto) <= tope:
        return texto
    corte = texto[:tope]
    espacio = corte.rfind(" ")
    if espacio > tope * 0.6:
        corte = corte[:espacio]
    return corte.rstrip(" ,;:-–—").rstrip() + ("." if corte[-1:].isalnum() else "")


def _arreglar(ia: dict) -> dict:
    """
    Lo que se puede corregir aqui sin volver a preguntar. Pasarse cinco
    caracteres del SEO no justifica otra llamada de 2.000 tokens.
    """
    ia["seo_title"] = _recortar(ia.get("seo_title"), SEO_TITLE_MAX)
    ia["seo_description"] = _recortar(ia.get("seo_description"), SEO_DESC_MAX)
    return ia


def _valida(ia: dict, cats_ok: set[str]) -> list[str]:
    """
    Problemas que SI obligan a repetir la llamada: falta contenido o las
    categorias no valen. Las longitudes ya se han recortado en _arreglar.
    """
    fallos = []
    for campo in ("resumen", "de_que_trata", "para_quien",
                  "valoracion_editorial", "seo_title", "seo_description"):
        if not str(ia.get(campo) or "").strip():
            fallos.append(f"{campo} vacio")
    for campo in ("temas_principales", "que_aporta"):
        if not isinstance(ia.get(campo), list) or len(ia[campo]) < 3:
            fallos.append(f"{campo} con menos de 3 vinetas")
    sd = str(ia.get("seo_description") or "")
    if sd and len(sd) < SEO_DESC_MIN:
        fallos.append(f"seo_description demasiado corta ({len(sd)})")
    if not [c for c in (ia.get("cats") or []) if c in cats_ok]:
        fallos.append("ninguna categoria valida")
    return fallos


def redactar(d: dict, reintentos: int = 2) -> tuple[dict, dict]:
    """
    Pide a DeepSeek los bloques narrativos. Si la respuesta no pasa las
    validaciones (tipicamente el SEO se pasa de largo), se le dice que
    corrija y se reintenta. Devuelve (ficha, uso de tokens).
    """
    tax = taxonomia()
    cats_ok = set(tax["cats"])
    mensajes = [{"role": "system", "content": SISTEMA + "\n\nCategorias validas:\n"
                 + ", ".join(tax["cats"])},
                {"role": "user", "content": _prompt(d, tax["cats"])}]
    uso = {"prompt_tokens": 0, "completion_tokens": 0, "llamadas": 0}
    ultimo_fallo = None
    for intento in range(reintentos + 1):
        res = _llamar(mensajes)
        u = res.get("usage", {})
        uso["prompt_tokens"] += u.get("prompt_tokens", 0)
        uso["completion_tokens"] += u.get("completion_tokens", 0)
        uso["llamadas"] += 1
        try:
            ia = json.loads(res["choices"][0]["message"]["content"])
        except Exception:
            ultimo_fallo = ["respuesta no es JSON"]
            continue
        ia = _arreglar(ia)
        fallos = _valida(ia, cats_ok)
        if not fallos:
            return ia, uso
        ultimo_fallo = fallos
        if intento < reintentos:
            mensajes += [
                {"role": "assistant", "content": json.dumps(ia, ensure_ascii=False)},
                {"role": "user", "content":
                 "Corrige SOLO esto y devuelve el JSON completo otra vez: "
                 + "; ".join(fallos)},
            ]
    raise RuntimeError(f"ficha no valida tras {reintentos + 1} intentos: {ultimo_fallo}")


# ─── Montaje del Body HTML ───────────────────────────────────────────
def body_html(d: dict, ia: dict) -> str:
    """Las 10 secciones, en el mismo orden que las fichas ya publicadas."""
    def parrafo(titulo, texto):
        return f"<h3>{titulo}</h3>\n<p>{limpiar_xml(texto)}</p>"

    def lista(titulo, items):
        lis = "\n".join(f"<li>{limpiar_xml(x)}</li>" for x in items if x)
        return f"<h3>{titulo}</h3>\n<ul>\n{lis}\n</ul>"

    partes = [
        f"<h2>{limpiar_xml(d.get('name'))}</h2>",
        parrafo("Resumen del libro", ia["resumen"]),
        parrafo("¿De qué trata?", ia["de_que_trata"]),
        lista("Temas principales", ia["temas_principales"]),
        parrafo("¿Para quién está recomendado?", ia["para_quien"]),
        lista("Qué aporta este libro", ia["que_aporta"]),
    ]
    ficha = ficha_tecnica_html(d)
    if ficha:
        partes.append(ficha)
    partes.append(parrafo("Valoración editorial", ia["valoracion_editorial"]))
    return "\n".join(partes)


def _editorial(valor) -> str:
    """
    Nombre de la editorial para el campo Vendor de Shopify.

    Shopify corta en 255 caracteres y rechaza el producto entero si se pasa.
    Pero el problema de fondo es otro: cuando el catalogo del distribuidor
    trae mal ese campo, lo que llega no es una editorial sino prosa. El
    9788415894711 traia 285 caracteres que empezaban por "PALABRAS DE
    FAMILIA (1995) Y HASTA EL FIN DE LOS CUENTOS (1998)...": es la solapa
    del libro, no el sello.

    Ninguna editorial real pasa de 120 caracteres, asi que por encima de eso
    se descarta en vez de recortarse: mejor sin editorial que con un parrafo
    partido a machete en la ficha publica. Hay 7 libros asi en el espejo.
    """
    v = (valor or "").strip()
    if not v or len(v) > 120:
        return ""
    return v.upper()[:255]


def fila_matrixify(d: dict, ia: dict) -> dict:
    """Las 23 columnas listas para el XLSX o para la API."""
    precio = precio_web(d)
    autor = (d.get("autor") or "").replace("#", "; ").strip()
    titulo = limpiar_xml(d.get("name"))
    img = imagen(d)
    alt = (f"{titulo}, de {autor} | portada del libro" if autor
           else f"{titulo} | portada del libro")[:125]
    return {
        "Command": "MERGE",
        "Handle": d["barcode"],
        "Title": titulo,
        "Vendor": _editorial(d.get("editorial")),
        "Type": "Libro",
        "Tags": montar_tags(ia.get("cats") or [], d),
        "Published": "TRUE",
        "Status": "active",
        "Body HTML": body_html(d, ia),
        "SEO Title": limpiar_xml(ia["seo_title"])[:SEO_TITLE_MAX],
        "SEO Description": limpiar_xml(ia["seo_description"]),
        "Variant SKU": d["barcode"],
        "Variant Barcode": d["barcode"],
        "Variant Price": f"{precio:.2f}" if precio else "",
        "Variant Compare At Price": f"{precio:.2f}" if precio else "",
        "Variant Inventory Qty": 0,
        "Variant Inventory Tracker": "shopify",
        "Variant Grams": peso_gramos(d.get("peso")),
        "Variant Requires Shipping": "TRUE",
        "Image Src": img,
        "Image Alt Text": alt if img else "",
        "Metafield: custom.autor [single_line_text_field]": limpiar_xml(autor),
        "Metafield: custom.anio_publicacion [number_integer]":
            str(anio_edicion(d.get("fecha")) or ""),
    }


def generar(isbn: str) -> tuple[dict, dict]:
    """ISBN -> (23 columnas, uso de tokens). Lanza si el libro no sirve."""
    d = datos_libro(isbn)
    if not d:
        raise RuntimeError(f"{isbn} no esta en el mirror")
    if not (d.get("name") or "").strip():
        raise RuntimeError(f"{isbn} sin titulo")
    ia, uso = redactar(d)
    return fila_matrixify(d, ia), uso


if __name__ == "__main__":
    import sys
    fila, uso = generar(sys.argv[1])
    for k, v in fila.items():
        print(f"{k}: {str(v)[:200]}")
    print("\ntokens:", uso)
