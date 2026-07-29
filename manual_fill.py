"""
Relleno manual de libros nuevos incompletos.

Dos vistas:
- "no_scrapeados": libros que llegaron y NO se pudieron scrapear (sin
  titulo). Organizados por fecha de intento de scraping (nuevo_creado_en).
- "todos": el resto de libros nuevos (scrapeados), editables. Por fecha de
  llegada.

Al guardar: actualiza books + odoo_books_mirror + el producto en Odoo
(nombre/precio) y re-etiqueta (Completo/Web/Foto/Stock) a la vez.
"""
import db
import pricing_engine
from odoo_client import OdooClient
from odoo_tags import _classify, _resolve_tags, TAG_NAMES

_BOOK_COLS = ["title", "author", "editorial", "image_url", "description",
              "weight", "height", "width"]


def _where(tipo: str) -> str:
    base = "m.nuevo_creado_en IS NOT NULL"
    if tipo == "no_scrapeados":
        return base + " AND (b.title IS NULL OR b.title = '')"
    return base + " AND b.title IS NOT NULL AND b.title <> ''"


def get_dates(tipo: str) -> list[dict]:
    """Fechas con conteo de pendientes para la sub-pestaña."""
    conn = db.get_connection(); cur = conn.cursor()
    db.execute_query(cur, f"""
        SELECT m.nuevo_creado_en::date AS d, COUNT(*)
        FROM odoo_books_mirror m
        LEFT JOIN books b ON b.isbn = m.barcode
        WHERE {_where(tipo)}
        GROUP BY 1 ORDER BY 1 DESC
    """)
    out = [{"fecha": str(r[0]), "count": r[1]} for r in cur.fetchall()]
    conn.close()
    return out


def get_pending(tipo: str, fecha: str, page: int = 1,
                page_size: int = 50) -> dict:
    conn = db.get_connection(); cur = conn.cursor()
    where = _where(tipo)
    db.execute_query(cur, f"""
        SELECT COUNT(*) FROM odoo_books_mirror m
        LEFT JOIN books b ON b.isbn = m.barcode
        WHERE {where} AND m.nuevo_creado_en::date = ?
    """, (fecha,))
    total = cur.fetchone()[0]
    off = (max(page, 1) - 1) * page_size
    db.execute_query(cur, f"""
        SELECT m.barcode, m.odoo_id, b.title, b.author, b.editorial,
               m.list_price, m.cdl_image_url, m.description,
               m.cdl_weight, m.cdl_height, m.cdl_width,
               (SELECT string_agg(DISTINCT p.nombre, ', ')
                  FROM libros_proveedor lp JOIN proveedores p ON p.id = lp.proveedor_id
                  WHERE lp.isbn = m.barcode) AS proveedores
        FROM odoo_books_mirror m
        LEFT JOIN books b ON b.isbn = m.barcode
        WHERE {where} AND m.nuevo_creado_en::date = ?
        ORDER BY m.barcode
        LIMIT ? OFFSET ?
    """, (fecha, page_size, off))
    items = []
    for r in cur.fetchall():
        items.append({
            "isbn": r[0], "odoo_id": r[1], "title": r[2] or "",
            "author": r[3] or "", "editorial": r[4] or "",
            "precio": float(r[5]) if r[5] is not None else "",
            "image_url": r[6] or "", "description": r[7] or "",
            "weight": r[8] or "", "height": r[9] or "", "width": r[10] or "",
            "proveedores": r[11] or "",
            "tag": _classify(r[6], r[7], r[8], r[9], r[10]),
        })
    conn.close()
    return {"total": total, "page": page, "page_size": page_size, "items": items}


def _save_db(data: dict):
    """Upsert a books + update odoo_books_mirror. Devuelve odoo_id."""
    isbn = data["isbn"]
    conn = db.get_connection(); cur = conn.cursor()
    vals = [data.get(c) or None for c in _BOOK_COLS]
    db.execute_query(cur, f"""
        INSERT INTO books (isbn, {", ".join(_BOOK_COLS)}, fuente, timestamp)
        VALUES (?, {", ".join(["?"] * len(_BOOK_COLS))}, 'manual', NOW())
        ON CONFLICT (isbn) WHERE isbn IS NOT NULL DO UPDATE SET
            {", ".join(f"{c}=EXCLUDED.{c}" for c in _BOOK_COLS)},
            fuente='manual', timestamp=NOW()
    """, (isbn, *vals))
    name = (data.get("title") or "").strip() or isbn
    precio = float(data["precio"]) if str(data.get("precio") or "").strip() else None
    wp = pricing_engine.web_price(precio)  # precio web con suplemento
    db.execute_query(cur, """
        UPDATE odoo_books_mirror SET
            name = ?, list_price = COALESCE(?, list_price),
            pvp_base = COALESCE(?, pvp_base),
            cdl_image_url = ?, description = ?,
            cdl_weight = ?, cdl_height = ?, cdl_width = ?
        WHERE barcode = ?
    """, (name, wp if wp is not None else precio, precio,
          data.get("image_url") or None, data.get("description") or None,
          data.get("weight") or None, data.get("height") or None,
          data.get("width") or None, isbn))
    db.execute_query(cur, "SELECT odoo_id FROM odoo_books_mirror WHERE barcode = ?", (isbn,))
    row = cur.fetchone()
    conn.commit(); conn.close()
    return row[0] if row else None


async def save_book(data: dict) -> dict:
    """Guarda en BD y Odoo a la vez, y re-etiqueta. Devuelve {tag, odoo_id}."""
    odoo_id = _save_db(data)
    tag = _classify(data.get("image_url"), data.get("description"),
                    data.get("weight"), data.get("height"), data.get("width"))
    if not odoo_id:
        return {"ok": False, "error": "no encontrado en Odoo", "tag": tag}
    name = (data.get("title") or "").strip() or data["isbn"]
    async with OdooClient() as odoo:
        tag_ids = await _resolve_tags(odoo)
        write_vals = {"name": name[:250]}
        if str(data.get("precio") or "").strip():
            pvp = float(data["precio"])
            wp = pricing_engine.web_price(pvp)  # aplica suplemento API-15
            if wp is None:
                write_vals["active"] = False    # < 2,90 -> apagar
            else:
                write_vals["list_price"] = wp
                write_vals["active"] = True
        # Reemplazar los 4 tags de estado por el nuevo (preserva Bloqueado)
        ops = [(3, tag_ids[t]) for t in TAG_NAMES if t != "Bloqueado" and t in tag_ids]
        if tag_ids.get(tag):
            ops.append((4, tag_ids[tag]))
        write_vals["product_tag_ids"] = ops
        await odoo.write("product.template", [odoo_id], write_vals)
    return {"ok": True, "tag": tag, "odoo_id": odoo_id}
