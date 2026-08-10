"""
Buscador unificado por ISBN: la vida entera de un libro en una consulta.

Mira los cuatro sitios a la vez y, sobre todo, EXPLICA lo que encuentra:
por que un libro con stock no aparece en la web, quien lo tiene, cuando
cambio por ultima vez y que le falta para poder venderse.

  1. libros_proveedor   que proveedores lo tienen, con cuanto stock y desde cuando
  2. odoo_books_mirror  que sabemos de su ficha
  3. Odoo (en vivo)     si existe, si admite stock, y cuanto tiene en cada almacen
  4. Shopify (en vivo)  si esta publicado y con cuanto stock

Nace de una revision del 05/08: 40 EAN de AZETA aparecian sin stock en la
web teniendolo en Odoo. Hacer esa comprobacion a mano son cuatro consultas
a cuatro sistemas distintos.
"""
import asyncio
import re
from datetime import datetime

import db

# Fechas de stock que se miran, de mas a menos fiable
CAMPOS_FECHA = ["actualizado_en", "stock_actualizado_en"]


def limpiar_isbn(texto: str) -> str:
    """Quita guiones y espacios: la gente los copia de cualquier sitio."""
    return re.sub(r"[^0-9Xx]", "", str(texto or "")).upper()


def _proveedores(isbn: str) -> list[dict]:
    """Quien lo tiene, con cuanto y desde cuando."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT COALESCE(p.nombre, lp.proveedor_email) AS proveedor,
                   lp.proveedor_email, lp.stock_disponible, lp.precio_con_iva,
                   lp.actualizado_en, lp.stock_actualizado_en,
                   m.warehouse_code,
                   COALESCE(pa.activo, true) AS proveedor_activo
            FROM libros_proveedor lp
            LEFT JOIN proveedores p ON p.id = lp.proveedor_id
            LEFT JOIN proveedor_almacen_odoo m
                   ON m.proveedor_email = lp.proveedor_email
            LEFT JOIN proveedor_pausa pa
                   ON pa.proveedor_email = lp.proveedor_email
            WHERE lp.isbn = ?
            ORDER BY lp.stock_disponible DESC
        """, (isbn,))
        return [{
            "proveedor": r[0], "email": r[1],
            "stock": int(r[2] or 0),
            "precio": float(r[3]) if r[3] is not None else None,
            # OJO con estos dos: las dos vias de entrada usan los campos con
            # significados CONTRARIOS, asi que ninguno vale como "cambio de
            # stock" a secas.
            #
            #   AZETA (fetcher CSV)  stock_actualizado_en = NOW() siempre que
            #                        el libro viene en el CSV; actualizado_en
            #                        solo si el numero cambio
            #   SINLI (n8n)          al reves
            #
            # Lo que si es cierto en ambos casos es que si stock_actualizado_en
            # se movio, el proveedor mando ese libro: sirve como "confirmado
            # por el proveedor", que es lo que importa para saber si el dato
            # esta fresco o rancio. Por eso se muestra ese.
            #
            # actualizado_en ademas lo pisan nuestras propias operaciones:
            # 31.051 filas de AZETA comparten el instante del 30/07 porque ese
            # dia se forzo un resync, no porque cambiaran todas a la vez.
            "cambio_stock": str(r[5]) if r[5] else None,
            "fila_actualizada": str(r[4]) if r[4] else None,
            "almacen": r[6],
            "proveedor_activo": bool(r[7]),
        } for r in cur.fetchall()]
    finally:
        conn.close()


def _ficha(isbn: str) -> dict | None:
    """Lo que sabemos de su ficha, con el origen de cada dato."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT m.odoo_id, m.name, m.list_price, m.pvp_base,
                   m.cdl_author, m.cdl_editorial, m.cdl_weight,
                   m.cdl_height, m.cdl_width,
                   m.cdl_image_url, m.gbooks_thumbnail,
                   LENGTH(COALESCE(m.description,'')) AS desc_len,
                   m.inferred_categories, m.nuevo_creado_en, m.synced_at,
                   b.title, db2.title
            FROM odoo_books_mirror m
            LEFT JOIN books b ON b.isbn = m.barcode
            LEFT JOIN distributor_books db2 ON db2.isbn = m.barcode
            WHERE m.barcode = ?
        """, (isbn,))
        r = cur.fetchone()
        if not r:
            return None
        return {
            "odoo_id": r[0], "nombre": r[1],
            "precio_web": float(r[2]) if r[2] is not None else None,
            "pvp_base": float(r[3]) if r[3] is not None else None,
            "autor": r[4], "editorial": r[5], "peso": r[6],
            "alto": r[7], "ancho": r[8],
            "imagen": r[9] or r[10],
            "descripcion_caracteres": int(r[11] or 0),
            "categoria_distribuidor": r[12],
            "creado_por_nosotros": str(r[13]) if r[13] else None,
            "espejo_actualizado": str(r[14]) if r[14] else None,
            "titulo_en_books": r[15], "titulo_en_catalogo": r[16],
        }
    finally:
        conn.close()


def _etiqueta_que_le_toca(ficha: dict | None) -> str | None:
    """
    Que etiqueta le corresponde con los datos que tenemos AHORA. Si no
    coincide con la que lleva en Odoo, es que se enriquecio despues de
    clasificarlo y hay que volver a pasarle los tags.
    """
    if not ficha:
        return None
    try:
        from odoo_tags import _classify
        return _classify(ficha.get("imagen"),
                         "x" if ficha.get("descripcion_caracteres") else None,
                         ficha.get("peso"), ficha.get("alto"), ficha.get("ancho"))
    except Exception:
        return None


def _publicacion(isbn: str) -> dict | None:
    """Que tenemos fichado de la tienda para este ISBN."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT estado, title, tags, variant_price, variant_grams,
                   fichero_origen, generado_en, cargado_en,
                   LENGTH(COALESCE(body_html,'')) AS body_len
            FROM shopify_productos WHERE handle = ?
        """, (isbn,))
        r = cur.fetchone()
        if not r:
            return None
        return {"estado": r[0], "titulo": r[1], "tags": r[2],
                "precio": r[3], "gramos": r[4], "origen": r[5],
                "generado_en": str(r[6]) if r[6] else None,
                "anotado_en": str(r[7]) if r[7] else None,
                "ficha_caracteres": int(r[8] or 0)}
    finally:
        conn.close()


async def _odoo(isbn: str) -> dict:
    """
    Estado real en Odoo: si existe, si admite stock y cuanto tiene en cada
    almacen. El write_date del quant dice cuando se toco por ultima vez.
    """
    from odoo_client import OdooClient
    async with OdooClient() as o:
        tmpl = await o.search_read(
            "product.template",
            [["barcode", "=", isbn], ["active", "in", [True, False]]],
            ["id", "name", "active", "is_storable", "list_price",
             "qty_available", "write_date", "product_tag_ids"], limit=1)
        if not tmpl:
            return {"existe": False}
        t = tmpl[0]
        salida = {
            "existe": True, "odoo_id": t["id"], "nombre": t["name"],
            "activo": t["active"], "admite_stock": t.get("is_storable"),
            "precio": t.get("list_price"),
            "stock_total": t.get("qty_available"),
            "modificado": t.get("write_date"),
            "almacenes": [], "variante_activa": None, "etiquetas": [],
        }
        if t.get("product_tag_ids"):
            tags = await o.read("product.tag", t["product_tag_ids"], ["name"])
            salida["etiquetas"] = [x["name"] for x in tags]
        var = await o.search_read(
            "product.product",
            [["product_tmpl_id", "=", t["id"]], ["active", "in", [True, False]]],
            ["id", "active"], limit=1)
        if not var:
            salida["variante_activa"] = False
            return salida
        salida["variante_activa"] = var[0]["active"]
        quants = await o.search_read(
            "stock.quant", [["product_id", "=", var[0]["id"]]],
            ["location_id", "quantity", "write_date"])
        for q in quants:
            loc = q.get("location_id")
            salida["almacenes"].append({
                "almacen": loc[1] if isinstance(loc, list) else loc,
                "cantidad": q.get("quantity"),
                "modificado": q.get("write_date"),
            })
        salida["almacenes"].sort(key=lambda x: -(x["cantidad"] or 0))
        return salida


def _shopify(isbn: str) -> dict:
    """Estado real en la tienda."""
    try:
        import shopify_api as sa
        if not (sa.CLIENT_ID and sa.CLIENT_SECRET):
            return {"existe": None, "error": "sin credenciales"}
        d = sa.graphql("""
            query($q: String!) {
              products(first: 3, query: $q) {
                edges { node { id handle title status totalInventory updatedAt
                  variants(first:1){edges{node{ price inventoryQuantity
                    inventoryItem { tracked } }}}
                  resourcePublicationsCount { count } } }
              }
            }""", {"q": f"handle:{isbn}"})
        edges = d["products"]["edges"]
        if not edges:
            return {"existe": False}
        n = edges[0]["node"]
        v = (n["variants"]["edges"] or [{}])[0].get("node", {})
        return {
            "existe": True, "titulo": n.get("title"),
            "estado": n.get("status"), "stock": n.get("totalInventory"),
            "precio": v.get("price"),
            "seguimiento_inventario": (v.get("inventoryItem") or {}).get("tracked"),
            "canales": (n.get("resourcePublicationsCount") or {}).get("count"),
            "modificado": n.get("updatedAt"),
        }
    except Exception as e:
        return {"existe": None, "error": f"{type(e).__name__}: {str(e)[:110]}"}


def _diagnosticar(prov, ficha, odoo, tienda, publicacion) -> list[dict]:
    """
    La parte util: explicar por que un libro no se puede comprar. Va de la
    causa mas de fondo a la mas superficial, porque arreglar la primera
    suele resolver las siguientes.
    """
    d = []
    def di(nivel, txt, accion=None):
        d.append({"nivel": nivel, "texto": txt, "accion": accion})

    stock_prov = max([p["stock"] for p in prov], default=0)
    if not prov:
        di("error", "Ningun proveedor lo tiene en la base de datos.",
           "Si deberia estar, el problema esta en la entrada de ficheros (Server A)")
        return d
    if stock_prov <= 0:
        di("aviso", "Ningun proveedor lo tiene con stock ahora mismo.",
           "Es correcto que no se venda")

    pausados = [p["proveedor"] for p in prov if not p["proveedor_activo"]]
    if pausados and all(not p["proveedor_activo"] for p in prov):
        di("aviso", f"Su unico proveedor esta pausado: {', '.join(pausados)}.",
           "Reactivalo en la tarjeta de Proveedores")
    sin_almacen = [p["proveedor"] for p in prov if not p["almacen"]]
    if sin_almacen:
        di("error", f"Sin almacen mapeado: {', '.join(sin_almacen)}.",
           "Dale de alta en la tarjeta de Proveedores")

    if not odoo.get("existe"):
        di("error", "No existe como producto en Odoo.",
           "Lo crea el ciclo diario de libros nuevos si cumple los requisitos")
        return d
    if not odoo.get("activo"):
        di("error", "El producto esta archivado en Odoo.",
           "Suele ser la regla de precios: por debajo de 2,90 no se publica")
    if odoo.get("admite_stock") is False:
        di("error", "El producto no admite stock en Odoo (Track Inventory apagado).",
           "Usa Reparar catalogo en la tarjeta de Proveedores")
    if odoo.get("variante_activa") is False:
        di("error", "Su variante esta archivada: no se le puede escribir stock.",
           "Usa Reparar catalogo en la tarjeta de Proveedores")

    stock_odoo = sum(a["cantidad"] or 0 for a in odoo.get("almacenes", [])
                     if (a["cantidad"] or 0) > 0)
    if stock_prov > 0 and stock_odoo <= 0 and odoo.get("admite_stock"):
        di("error", f"El proveedor tiene {stock_prov} pero en Odoo esta a 0.",
           "Usa Conciliar stock en la tarjeta de Proveedores")

    if tienda.get("existe") is False:
        if publicacion:
            di("aviso", f"Ficha generada ({publicacion['estado']}) pero aun no subida a la tienda.",
               "Pendiente de publicar desde la pestana Shopify")
        else:
            di("aviso", "No esta publicado en la tienda.",
               "Mira en la pestana Shopify si cumple los requisitos para publicarse")
    elif tienda.get("existe"):
        if (tienda.get("stock") or 0) <= 0 and stock_odoo > 0:
            di("error",
               f"Publicado pero con stock 0 en la tienda, teniendo {stock_odoo:g} en Odoo.",
               "El inventario de Odoo no se esta sincronizando a Shopify")
        if tienda.get("estado") != "ACTIVE":
            di("aviso", f"En la tienda esta como {tienda.get('estado')}.")
    else:
        # existe es None: no se pudo preguntar a Shopify. Hay que decirlo,
        # porque si no el diagnostico final acababa afirmando que el libro
        # estaba "publicado con inventario" sin haberlo mirado.
        di("aviso", f"No se pudo consultar la tienda ({tienda.get('error') or 'sin respuesta'}).",
           "Lo de Shopify que sale aqui no es fiable hasta que responda")

    etiquetas = odoo.get("etiquetas") or []
    toca = _etiqueta_que_le_toca(ficha)
    estado_tags = [e for e in etiquetas if e in ("Completo", "Web", "Foto", "Stock")]
    if toca and estado_tags and toca not in estado_tags:
        di("aviso",
           f"Etiquetado como {estado_tags[0]} pero con los datos de hoy le tocaria {toca}.",
           "Se arregla volviendo a clasificar en Tags Odoo")
    if "Bloqueado" in etiquetas:
        di("aviso", "Lleva la etiqueta Bloqueado: alguien lo aparto a proposito.")

    if not d:
        di("ok", "Todo correcto: tiene stock, esta en Odoo y publicado con inventario.")
    return d


async def buscar(isbn_bruto: str) -> dict:
    """La vida entera del libro, con diagnostico."""
    isbn = limpiar_isbn(isbn_bruto)
    if not isbn:
        return {"error": "ISBN vacio"}
    prov = _proveedores(isbn)
    ficha = _ficha(isbn)
    publicacion = _publicacion(isbn)
    try:
        odoo = await _odoo(isbn)
    except Exception as e:
        odoo = {"existe": None, "error": f"{type(e).__name__}: {str(e)[:110]}"}
    tienda = _shopify(isbn)

    stock_prov = max([p["stock"] for p in prov], default=0)
    stock_odoo = sum(a["cantidad"] or 0 for a in odoo.get("almacenes", [])
                     if (a["cantidad"] or 0) > 0)
    # La fecha mas reciente en que se movio el stock, venga de donde venga
    fechas = [p["cambio_stock"] for p in prov if p["cambio_stock"]]
    fechas += [a["modificado"] for a in odoo.get("almacenes", []) if a.get("modificado")]

    return {
        "isbn": isbn,
        "consultado_en": datetime.now().isoformat(),
        "resumen": {
            "proveedores": len(prov),
            "stock_proveedor": stock_prov,
            "stock_odoo": stock_odoo,
            "stock_shopify": tienda.get("stock"),
            "en_odoo": bool(odoo.get("existe")),
            "en_shopify": tienda.get("existe"),
            "ultimo_cambio_stock": max(fechas) if fechas else None,
        },
        "etiquetas": {
            "en_odoo": odoo.get("etiquetas", []),
            "le_corresponde": _etiqueta_que_le_toca(ficha),
            "en_shopify": publicacion.get("tags") if publicacion else None,
        },
        "diagnostico": _diagnosticar(prov, ficha, odoo, tienda, publicacion),
        "proveedores": prov,
        "ficha": ficha,
        "odoo": odoo,
        "shopify": tienda,
        "publicacion": publicacion,
    }


if __name__ == "__main__":
    import sys, json
    print(json.dumps(asyncio.run(buscar(sys.argv[1])), indent=2, ensure_ascii=False))
