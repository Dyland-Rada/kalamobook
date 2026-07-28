"""
Crea en Odoo los libros con stock que faltan, con la data que tengamos, y
los etiqueta (Completo/Web/Foto/Stock) reutilizando la logica de odoo_tags.

Pipeline por lote:
  1. Selecciona faltantes (stock>0, no en Odoo)
  2. Crea product.template (name/barcode/list_price/type=consu)
  3. Upsert al mirror con la metadata de `books` (img/desc/peso/dims)
  4. Clasifica y escribe product_tag_ids

Uso:
    ... python scripts/create_faltantes.py --limit 20          # prueba
    ... python scripts/create_faltantes.py                     # completo
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
from odoo_client import OdooClient
from odoo_tags import _classify, _resolve_tags


def fetch_targets(limit=None):
    """Faltantes: ISBN con stock, no en Odoo. Junta precio + metadata books."""
    conn = db.get_connection(); cur = conn.cursor()
    q = """
        SELECT lp.isbn,
               MAX(lp.precio_con_iva) AS precio,
               MAX(b.title) AS title, MAX(b.author) AS author,
               MAX(b.image_url) AS image_url, MAX(b.description) AS description,
               MAX(b.weight) AS weight, MAX(b.height) AS height, MAX(b.width) AS width
        FROM libros_proveedor lp
        LEFT JOIN odoo_books_mirror m ON m.barcode = lp.isbn
        LEFT JOIN books b ON b.isbn = lp.isbn
        WHERE lp.stock_disponible > 0 AND m.barcode IS NULL
        GROUP BY lp.isbn
    """
    if limit:
        q += f" LIMIT {int(limit)}"
    cur.execute(q)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows


def mirror_upsert(items):
    """items: [{odoo_id, isbn, precio, title, image_url, description, weight, height, width}]"""
    from psycopg2.extras import execute_values
    conn = db.get_connection(); cur = conn.cursor()
    vals = [(it["odoo_id"], it["isbn"], it.get("title"), it.get("precio"),
             it.get("image_url"), it.get("description"),
             it.get("weight"), it.get("height"), it.get("width"))
            for it in items]
    execute_values(cur, """
        INSERT INTO odoo_books_mirror
            (odoo_id, barcode, name, list_price, cdl_image_url, description,
             cdl_weight, cdl_height, cdl_width, synced_at)
        VALUES %s
        ON CONFLICT (odoo_id) DO UPDATE SET
            barcode=EXCLUDED.barcode, name=EXCLUDED.name,
            list_price=EXCLUDED.list_price, cdl_image_url=EXCLUDED.cdl_image_url,
            description=EXCLUDED.description, cdl_weight=EXCLUDED.cdl_weight,
            cdl_height=EXCLUDED.cdl_height, cdl_width=EXCLUDED.cdl_width,
            synced_at=NOW()
    """, vals, template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())", page_size=len(vals))
    conn.commit(); conn.close()


async def run(limit, chunk=200):
    targets = fetch_targets(limit)
    print(f"Faltantes a crear: {len(targets):,}", flush=True)
    if not targets:
        return
    created = 0
    tag_counts = {"Completo": 0, "Web": 0, "Foto": 0, "Stock": 0}
    async with OdooClient() as odoo:
        tag_ids = await _resolve_tags(odoo)
        for i in range(0, len(targets), chunk):
            batch = targets[i:i + chunk]
            # 1) crear product.template
            vals = []
            for t in batch:
                name = (t.get("title") or "").strip() or t["isbn"]
                price = float(t["precio"]) if t.get("precio") else 0.0
                vals.append({
                    "name": name[:250], "barcode": t["isbn"],
                    "default_code": t["isbn"], "list_price": price,
                    "type": "consu", "sale_ok": True, "purchase_ok": True,
                })
            new_ids = await odoo.execute_kw("product.template", "create", [vals])
            for t, oid in zip(batch, new_ids):
                t["odoo_id"] = oid
            created += len(new_ids)
            # 2) upsert al mirror
            mirror_upsert(batch)
            # 3) clasificar y agrupar por tag
            by_tag = {}
            for t in batch:
                tag = _classify(t.get("image_url"), t.get("description"),
                                t.get("weight"), t.get("height"), t.get("width"))
                tag_counts[tag] += 1
                by_tag.setdefault(tag, []).append(t["odoo_id"])
            # 4) escribir product_tag_ids por grupo
            for tag, ids in by_tag.items():
                if tag_ids.get(tag):
                    await odoo.write("product.template", ids,
                                     {"product_tag_ids": [(4, tag_ids[tag])]})
            print(f"  {created:,}/{len(targets):,} creados | tags {tag_counts}", flush=True)
    print(f"LISTO: {created:,} creados. Tags: {tag_counts}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    asyncio.run(run(a.limit))
