"""
El catalogo que se puede vender: una fila por ISBN, ya filtrada.

Nace de una venta en Fnac de un libro que no existia. El feed de marketplace
leia `libros_proveedor`, que es una tabla de TRABAJO: ahi esta lo que manda
cada proveedor en crudo, sin pasar por ninguna regla. Las pausas, los
apagados por ausencia y la deteccion de proveedores mudos estaban todas
aplicadas contra Odoo, asi que quien leyera la tabla intermedia se las
saltaba enteras. Medido el 19/08: de 674.642 libros con stock ahi, 309.517
eran de proveedores pausados, mudos o con el dato rancio.

Esta tabla es un CONTRATO, no una tabla de trabajo. Lo que esta aqui se
puede vender. Quien la lea no necesita saber nada de proveedores, ficheros,
pausas ni capas de precio.

De donde sale cada cosa:

  stock              de Odoo, que es donde estan aplicadas todas las reglas
  precio_marketplace Capa 1 (ya en Odoo) + Capa 3 + centimo
  precio_web         pendiente: falta definir la Capa 2 de Shopify
  confirmado_en      cuando lo confirmo por ultima vez algun proveedor

Las filas que se quedan sin stock NO se borran: se dejan a 0. Quien consume
necesita enterarse de que un libro dejo de estar disponible, y si la fila
desaparece no hay forma de distinguir "ya no se vende" de "aun no ha
llegado".
"""
import os
import time
from datetime import datetime

import db
import pricing_engine
from odoo_client import OdooClient

TABLA = "catalogo_publicable"
PAGINA_QUANT = 40000

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


def ensure_schema():
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            CREATE TABLE IF NOT EXISTS {TABLA} (
                isbn                TEXT PRIMARY KEY,
                titulo              TEXT,
                stock               INTEGER NOT NULL DEFAULT 0,
                precio_marketplace  NUMERIC(10,2),
                precio_web          NUMERIC(10,2),
                precio_odoo         NUMERIC(10,2),
                proveedor           TEXT,
                precio_coste        NUMERIC(10,2),
                confirmado_en       TIMESTAMP,
                odoo_id             INTEGER,
                actualizado_en      TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        db.execute_query(cur, f"CREATE INDEX IF NOT EXISTS {TABLA}_stock_idx "
                              f"ON {TABLA} (stock) WHERE stock > 0")
        db.execute_query(cur, f"CREATE INDEX IF NOT EXISTS {TABLA}_act_idx "
                              f"ON {TABLA} (actualizado_en)")
        conn.commit()
    finally:
        conn.close()


async def _totales_odoo(odoo: OdooClient, job: dict) -> dict[int, float]:
    """{template_id: unidades} sumando los almacenes internos."""
    totales: dict[int, float] = {}
    offset = 0
    while True:
        if job["status"] != "running":
            break
        pagina = await odoo.search_read(
            "stock.quant", [["location_id.usage", "=", "internal"]],
            ["product_tmpl_id", "quantity"],
            offset=offset, limit=PAGINA_QUANT, order="id")
        for q in pagina:
            t = q.get("product_tmpl_id")
            t = t[0] if isinstance(t, list) else t
            if t:
                totales[t] = totales.get(t, 0.0) + (q.get("quantity") or 0.0)
        if len(pagina) < PAGINA_QUANT:
            break
        offset += PAGINA_QUANT
        job["stage"] = f"leyendo Odoo ({len(totales):,})"
        print(f"[Catalogo] leidos {len(totales):,}", flush=True)
    return totales


def _filas(totales: dict[int, float]) -> list[tuple]:
    """
    Junta el stock de Odoo con la ficha y el proveedor mas barato que lo
    tiene. El precio de marketplace se calcula aqui, que antes solo existia
    dentro del workflow de n8n y no se podia consultar desde ningun sitio.
    """
    con_stock = [k for k, v in totales.items() if v > 0]
    if not con_stock:
        return []
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        filas = []
        for i in range(0, len(con_stock), 5000):
            trozo = con_stock[i:i + 5000]
            db.execute_query(cur, """
                SELECT m.odoo_id, m.barcode, m.name, m.list_price,
                       lp.proveedor_email, lp.precio_con_iva,
                       lp.stock_actualizado_en
                FROM odoo_books_mirror m
                LEFT JOIN LATERAL (
                    SELECT proveedor_email, precio_con_iva, stock_actualizado_en
                    FROM libros_proveedor
                    WHERE isbn = m.barcode AND stock_disponible > 0
                    ORDER BY precio_con_iva NULLS LAST
                    LIMIT 1
                ) lp ON true
                WHERE m.odoo_id = ANY(?)
                  AND m.barcode IS NOT NULL
            """, (trozo,))
            for oid, isbn, nombre, precio, prov, coste, conf in cur.fetchall():
                pw = float(precio) if precio else None
                filas.append((
                    isbn, nombre, int(round(totales.get(int(oid), 0.0))),
                    pricing_engine.precio_marketplace(pw),
                    None,          # precio_web: falta la Capa 2
                    pw, prov,
                    float(coste) if coste is not None else None,
                    conf, int(oid),
                ))
        return filas
    finally:
        conn.close()


def _guardar(filas: list[tuple], job: dict, inicio: datetime):
    from psycopg2.extras import execute_values
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        for i in range(0, len(filas), 5000):
            execute_values(cur, f"""
                INSERT INTO {TABLA}
                    (isbn, titulo, stock, precio_marketplace, precio_web,
                     precio_odoo, proveedor, precio_coste, confirmado_en,
                     odoo_id, actualizado_en)
                VALUES %s
                ON CONFLICT (isbn) DO UPDATE SET
                    titulo = EXCLUDED.titulo,
                    stock = EXCLUDED.stock,
                    precio_marketplace = EXCLUDED.precio_marketplace,
                    precio_web = EXCLUDED.precio_web,
                    precio_odoo = EXCLUDED.precio_odoo,
                    proveedor = EXCLUDED.proveedor,
                    precio_coste = EXCLUDED.precio_coste,
                    confirmado_en = EXCLUDED.confirmado_en,
                    odoo_id = EXCLUDED.odoo_id,
                    actualizado_en = NOW()
            """, filas[i:i + 5000],
                template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())",
                page_size=5000)
            conn.commit()
            job["guardadas"] += len(filas[i:i + 5000])

        # Lo que ya no tiene stock se pone a 0 en vez de borrarse: quien
        # consume tiene que poder distinguir "dejo de venderse" de "nunca
        # estuvo". Si la fila desaparece, el feed no retira el articulo.
        #
        # Se compara contra el instante en que ARRANCO esta corrida, no
        # contra "hace un minuto". Guardar 386.554 filas lleva mas de un
        # minuto, asi que la version anterior daba por viejas las que ella
        # misma acababa de escribir: en la primera corrida retiro 160.000
        # libros que si tenian stock.
        job["stage"] = "retirando lo que ya no hay"
        db.execute_query(cur, f"""
            UPDATE {TABLA} SET stock = 0, actualizado_en = NOW()
            WHERE stock > 0 AND actualizado_en < ?
        """, (inicio,))
        job["retiradas"] = cur.rowcount or 0
        conn.commit()
    finally:
        conn.close()


async def refrescar(dry_run: bool = False) -> dict:
    """Vuelve a construir el catalogo publicable desde Odoo."""
    global _job
    ensure_schema()
    _job = {"status": "running", "dry_run": dry_run,
            "started_at": datetime.now().isoformat(), "stage": "empezando",
            "con_stock_odoo": 0, "filas": 0, "guardadas": 0, "retiradas": 0,
            "sin_precio": 0, "errors": [], "elapsed_s": 0}
    job = _job
    t0 = time.monotonic()
    inicio = datetime.now()
    try:
        job["stage"] = "leyendo Odoo"
        async with OdooClient() as odoo:
            totales = await _totales_odoo(odoo, job)
        job["con_stock_odoo"] = sum(1 for v in totales.values() if v > 0)

        job["stage"] = "montando las filas"
        filas = _filas(totales)
        job["filas"] = len(filas)
        job["sin_precio"] = sum(1 for f in filas if f[3] is None)

        if dry_run:
            job["muestra"] = [
                {"isbn": f[0], "titulo": (f[1] or "")[:40], "stock": f[2],
                 "marketplace": f[3], "odoo": f[5], "proveedor": f[6]}
                for f in filas[:20]]
            job["stage"] = "dry_run_done"
            job["status"] = "completed"
            return job

        job["stage"] = "guardando"
        _guardar(filas, job, inicio)
        job["stage"] = "done"
        if job["status"] == "running":
            job["status"] = "completed"
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e}"[:300])
        print(f"[Catalogo] FAIL: {e!r}")
    finally:
        job["elapsed_s"] = round(time.monotonic() - t0, 1)
        try:
            import audit_log
            audit_log.log_event(
                "catalogo_publicable",
                "dry_run" if dry_run else "refresco",
                f"Catalogo publicable{' [DRY RUN]' if dry_run else ''}: "
                f"{job['filas']:,} libros vendibles, {job['guardadas']:,} "
                f"guardados, {job['retiradas']:,} retirados "
                f"({job['elapsed_s']}s)",
                detalle={k: job.get(k) for k in
                         ("con_stock_odoo", "filas", "guardadas", "retiradas",
                          "sin_precio", "dry_run", "elapsed_s")},
                nivel="error" if job["status"] == "error" else "info")
        except Exception:
            pass
    return job


if __name__ == "__main__":
    import asyncio
    import sys
    seco = "--apply" not in sys.argv
    r = asyncio.run(refrescar(dry_run=seco))
    print(f"\n{'DRY RUN' if seco else 'REFRESCO'}  {r['status']}  ({r['elapsed_s']}s)")
    print(f"   con stock en Odoo   {r['con_stock_odoo']:>9,}")
    print(f"   filas del catalogo  {r['filas']:>9,}")
    print(f"   sin precio          {r['sin_precio']:>9,}")
    print(f"   guardadas           {r['guardadas']:>9,}")
    print(f"   retiradas (a 0)     {r['retiradas']:>9,}")
    for m in (r.get("muestra") or [])[:10]:
        print(f"      {m['isbn']}  stock={m['stock']:<4} "
              f"odoo={m['odoo']}  marketplace={m['marketplace']}  "
              f"{(m['titulo'] or '')[:30]}")
    for e in r["errors"][:5]:
        print(f"   ERROR: {e}")
