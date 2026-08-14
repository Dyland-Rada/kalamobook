"""
Stock de Odoo a Shopify, por nuestra cuenta.

El conector que habia sincroniza unos 3.144 productos al dia y Odoo genera
16.257 cambios de stock diarios: no llega ni de lejos, y el desfase se
acumula durante semanas. Se nota en las dos direcciones y las dos cuestan
dinero: libros con existencias que salen AGOTADOS, y libros a la venta que
ya no tiene nadie.

Que hace este modulo:

  1. exportar_inventario()  trae de Shopify el identificador de inventario
                            de cada libro y lo que la tienda cree tener
  2. sincronizar()          compara con Odoo y escribe solo lo que cambia

Por que hace falta el paso 1: para escribir stock Shopify no vale el handle
ni el ISBN, hace falta el inventoryItemId de la variante. No lo teniamos.
Se saca con una operacion masiva -una peticion, no 3.000- y se guarda.

Decisiones que conviene conocer:

  - La cantidad que se publica es el TOTAL de Odoo sumando los catorce
    almacenes, porque es lo que se puede servir. Cada almacen es la
    disponibilidad de un proveedor, no inventario propio.
  - El sync es incremental: pregunta a Odoo solo por los quants tocados
    desde la ultima corrida. La primera vez lee todo.
  - Un libro que esta en Shopify y no en Odoo NO se toca. Que falte en Odoo
    puede ser un fallo nuestro, y poner a cero por si acaso es la clase de
    decision que deja una tienda vacia.

Escribir stock en la tienda es una operacion de cara al publico: dry_run
viene activado por defecto y hay tope de seguridad por corrida.
"""
import os
import time
from datetime import datetime, timezone

import db
import shopify_api as sa
from odoo_client import OdooClient

TABLA = "shopify_inventario"
ENTIDAD = "shopify_stock"

# Shopify admite 250 cantidades por llamada.
LOTE = int(os.environ.get("SHOPIFY_STOCK_LOTE", "250"))
# Tope por corrida. Si una corrida quiere cambiar mas que esto, para y avisa:
# suele significar que la lectura de Odoo vino mal, no que cambiara medio
# catalogo de golpe.
TOPE_CORRIDA = int(os.environ.get("SHOPIFY_STOCK_TOPE", "60000"))
# "available" es lo que ve el comprador. "on_hand" es el fisico.
CAMPO = os.environ.get("SHOPIFY_STOCK_CAMPO", "available")
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
                handle              TEXT PRIMARY KEY,
                variant_gid         TEXT,
                inventory_item_gid  TEXT,
                qty_shopify         INTEGER,
                qty_odoo            INTEGER,
                escrito_en          TIMESTAMP,
                leido_en            TIMESTAMP
            )
        """)
        db.execute_query(cur, f"CREATE INDEX IF NOT EXISTS {TABLA}_item_idx "
                              f"ON {TABLA} (inventory_item_gid)")
        conn.commit()
    finally:
        conn.close()


# ── Marcapaginas ────────────────────────────────────────────────────────

def _marcador() -> str | None:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, "SELECT ultimo_timestamp FROM sync_state "
                              "WHERE entidad = ?", (ENTIDAD,))
        r = cur.fetchone()
        return str(r[0])[:19] if r and r[0] else None
    except Exception:
        conn.rollback()
        return None
    finally:
        conn.close()


def _guardar_marcador(ts: str, procesados: int):
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            INSERT INTO sync_state (entidad, ultimo_timestamp, ultima_ejecucion,
                                    ultima_ejecucion_ok, items_procesados)
            VALUES (?, ?, NOW(), true, ?)
            ON CONFLICT (entidad) DO UPDATE
            SET ultimo_timestamp = EXCLUDED.ultimo_timestamp,
                ultima_ejecucion = NOW(), ultima_ejecucion_ok = true,
                items_procesados = EXCLUDED.items_procesados
        """, (ENTIDAD, ts, procesados))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


# ── 1. Traer de Shopify los identificadores de inventario ───────────────

_BULK_INVENTARIO = """
mutation {
  bulkOperationRunQuery(
    query: \"\"\"
    { productVariants {
        edges { node {
          id
          barcode
          inventoryQuantity
          inventoryItem { id }
          product { handle }
        } }
    } }
    \"\"\"
  ) { bulkOperation { id status } userErrors { field message } }
}
"""


def exportar_inventario(espera_s: int = 10, maximo_min: int = 60) -> dict:
    """
    Descarga de Shopify, con una sola operacion masiva, el identificador de
    inventario de cada variante y la cantidad que la tienda cree tener.
    Hay que correrlo una vez antes de sincronizar, y de vez en cuando para
    recoger los libros nuevos.
    """
    global _job
    ensure_schema()
    _job = {"status": "running", "accion": "exportar_inventario",
            "started_at": datetime.now().isoformat(), "stage": "pidiendo a Shopify",
            "leidos": 0, "guardados": 0, "sin_handle": 0,
            "errors": [], "elapsed_s": 0}
    job = _job
    t0 = time.monotonic()
    try:
        import json as _json
        import urllib.request
        d = sa.graphql(_BULK_INVENTARIO)
        errores = d["bulkOperationRunQuery"]["userErrors"]
        if errores:
            raise sa.ShopifyError(f"no arranco la operacion masiva: {errores}")
        limite = time.time() + maximo_min * 60
        url = None
        while time.time() < limite:
            time.sleep(espera_s)
            est = sa.graphql("{ currentBulkOperation "
                             "{ status objectCount url errorCode } }")
            op = est["currentBulkOperation"] or {}
            if op.get("status") == "COMPLETED":
                url = op.get("url")
                break
            if op.get("status") in ("FAILED", "CANCELED"):
                raise sa.ShopifyError(
                    f"operacion masiva {op.get('status')}: {op.get('errorCode')}")
            job["stage"] = f"exportando ({op.get('objectCount') or 0} objetos)"
        if not url:
            raise sa.ShopifyError("la operacion masiva no termino a tiempo")

        job["stage"] = "guardando"
        filas = []
        with urllib.request.urlopen(url, timeout=900) as r:
            for linea in r:
                linea = linea.strip()
                if not linea:
                    continue
                n = _json.loads(linea)
                job["leidos"] += 1
                handle = (n.get("product") or {}).get("handle")
                item = (n.get("inventoryItem") or {}).get("id")
                if not handle or not item:
                    job["sin_handle"] += 1
                    continue
                filas.append((handle, n.get("id"), item,
                              n.get("inventoryQuantity")))
        job["guardados"] = _guardar_inventario(filas)
        job["status"] = "completed"
        job["stage"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e}"[:300])
        print(f"[ShopifyStock] exportar FAIL: {e!r}")
    finally:
        job["elapsed_s"] = round(time.monotonic() - t0, 1)
        _audit("exportar_inventario",
               f"Inventario de Shopify: {job['leidos']:,} variantes leidas, "
               f"{job['guardados']:,} guardadas ({job['elapsed_s']}s)", job)
    return job


def _guardar_inventario(filas: list[tuple]) -> int:
    if not filas:
        return 0
    from psycopg2.extras import execute_values
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        total = 0
        for i in range(0, len(filas), 5000):
            execute_values(cur, f"""
                INSERT INTO {TABLA}
                    (handle, variant_gid, inventory_item_gid, qty_shopify, leido_en)
                VALUES %s
                ON CONFLICT (handle) DO UPDATE
                SET variant_gid = EXCLUDED.variant_gid,
                    inventory_item_gid = EXCLUDED.inventory_item_gid,
                    qty_shopify = EXCLUDED.qty_shopify,
                    leido_en = NOW()
            """, filas[i:i + 5000], template="(%s,%s,%s,%s,NOW())",
                page_size=5000)
            total += len(filas[i:i + 5000])
            conn.commit()
        return total
    finally:
        conn.close()


# ── 2. Stock de Odoo ────────────────────────────────────────────────────

async def _stock_odoo(odoo: OdooClient, desde: str | None,
                      job: dict) -> tuple[dict[int, float], str | None]:
    """
    {template_id: unidades} sumando los almacenes internos.

    Incremental: con `desde` solo pregunta por los quants tocados despues.
    OJO, y por eso se devuelven los template_id afectados y no solo los
    quants: si un libro tiene stock en tres almacenes y solo cambia uno,
    hay que releer los tres para saber su total. Eso lo resuelve la segunda
    lectura, dirigida a esos templates.
    """
    dominio = [["location_id.usage", "=", "internal"]]
    if desde:
        dominio.append(["write_date", ">", desde])

    tocados: set[int] = set()
    max_ts = desde
    offset = 0
    while True:
        if job["status"] != "running":
            break
        pagina = await odoo.search_read(
            "stock.quant", dominio,
            ["product_tmpl_id", "write_date"],
            offset=offset, limit=PAGINA_QUANT, order="id")
        for q in pagina:
            t = q.get("product_tmpl_id")
            t = t[0] if isinstance(t, list) else t
            if t:
                tocados.add(t)
            w = q.get("write_date")
            if w and (max_ts is None or w > max_ts):
                max_ts = w
        if len(pagina) < PAGINA_QUANT:
            break
        offset += PAGINA_QUANT
        job["stage"] = f"leyendo Odoo ({len(tocados):,} libros tocados)"
        print(f"[ShopifyStock] tocados: {len(tocados):,}", flush=True)

    if not tocados:
        return {}, max_ts

    # Total real de cada libro tocado, ahora si sumando TODOS sus almacenes.
    job["stage"] = f"sumando el total de {len(tocados):,} libros"
    totales: dict[int, float] = {t: 0.0 for t in tocados}
    ids = sorted(tocados)
    for i in range(0, len(ids), 2000):
        if job["status"] != "running":
            break
        trozo = ids[i:i + 2000]
        quants = await odoo.search_read(
            "stock.quant",
            [["product_tmpl_id", "in", trozo],
             ["location_id.usage", "=", "internal"]],
            ["product_tmpl_id", "quantity"])
        for q in quants:
            t = q.get("product_tmpl_id")
            t = t[0] if isinstance(t, list) else t
            if t in totales:
                totales[t] += (q.get("quantity") or 0.0)
    return totales, max_ts


def _handles_de(totales: dict[int, float]) -> tuple[list[tuple], int, int]:
    """
    Devuelve (cambios, ya_coinciden, sin_identificador), donde cambios es
    [(handle, inventory_item_gid, qty_odoo, qty_shopify)].

    Los tres se cuentan por separado a proposito. Restar "los que cambian"
    del total mezcla dos cosas muy distintas: un libro que ya esta bien y un
    libro al que no podemos escribir porque no tenemos su identificador. El
    primero no hay que hacer nada con el; el segundo es trabajo pendiente.
    """
    if not totales:
        return [], 0, 0
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        ids = list(totales)
        cambios, iguales, encontrados = [], 0, 0
        for i in range(0, len(ids), 5000):
            trozo = ids[i:i + 5000]
            db.execute_query(cur, f"""
                SELECT m.odoo_id, m.barcode, s.inventory_item_gid, s.qty_shopify
                FROM odoo_books_mirror m
                JOIN {TABLA} s ON s.handle = m.barcode
                WHERE m.odoo_id = ANY(?)
                  AND s.inventory_item_gid IS NOT NULL
            """, (trozo,))
            for odoo_id, handle, item, qty_shop in cur.fetchall():
                encontrados += 1
                nuevo = int(round(totales.get(int(odoo_id), 0.0)))
                if qty_shop is not None and nuevo == qty_shop:
                    iguales += 1
                else:
                    cambios.append((handle, item, nuevo, qty_shop))
        return cambios, iguales, len(totales) - encontrados
    finally:
        conn.close()


# ── 3. Escribir en Shopify ──────────────────────────────────────────────

_MUT_SET = """
mutation ($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup { createdAt reason }
    userErrors { field message }
  }
}
"""


def _location_gid() -> str:
    """La ubicacion de la tienda. Se pregunta, no se da por sabida."""
    fijo = os.environ.get("SHOPIFY_LOCATION_GID")
    if fijo:
        return fijo
    d = sa.graphql("{ locations(first: 10) { edges { node { id name } } } }")
    nodos = [e["node"] for e in d["locations"]["edges"]]
    if not nodos:
        raise sa.ShopifyError("la tienda no tiene ninguna ubicacion")
    return nodos[0]["id"]


def _escribir(loc: str, lote: list[tuple], job: dict) -> int:
    entrada = {
        "name": CAMPO,
        "reason": "correction",
        "ignoreCompareQuantity": True,
        "quantities": [{"inventoryItemId": item, "locationId": loc,
                        "quantity": qty} for _, item, qty, _ in lote],
    }
    d = sa.graphql(_MUT_SET, {"input": entrada})
    errores = (d.get("inventorySetQuantities") or {}).get("userErrors") or []
    if errores:
        job["errors"].append(f"lote: {errores[:3]}")
        return 0
    return len(lote)


def _marcar_escritos(lote: list[tuple]):
    from psycopg2.extras import execute_values
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        execute_values(cur, f"""
            UPDATE {TABLA} s SET qty_shopify = v.qty, qty_odoo = v.qty,
                                 escrito_en = NOW()
            FROM (VALUES %s) AS v(handle, qty)
            WHERE s.handle = v.handle
        """, [(h, q) for h, _, q, _ in lote], template="(%s,%s)",
            page_size=1000)
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


async def sincronizar(dry_run: bool = True, completo: bool = False,
                      limite: int | None = None) -> dict:
    """
    Lleva el stock de Odoo a Shopify. Solo escribe lo que ha cambiado.

    dry_run=True dice cuantos cambiaria y una muestra, sin tocar la tienda.
    completo=True ignora el marcapaginas y repasa el catalogo entero.
    """
    global _job
    ensure_schema()
    _job = {"status": "running", "accion": "sincronizar", "dry_run": dry_run,
            "started_at": datetime.now().isoformat(), "stage": "empezando",
            "desde": None, "libros_tocados": 0, "a_cambiar": 0,
            "escritos": 0, "ya_coinciden": 0, "sin_identificador": 0,
            "subidas": 0, "bajadas": 0,
            "a_cero": 0, "muestra": [], "errors": [], "elapsed_s": 0}
    job = _job
    t0 = time.monotonic()
    try:
        desde = None if completo else _marcador()
        job["desde"] = desde
        job["stage"] = "leyendo Odoo"
        async with OdooClient() as odoo:
            totales, max_ts = await _stock_odoo(odoo, desde, job)
        job["libros_tocados"] = len(totales)

        job["stage"] = "comparando con la tienda"
        cambios, iguales, sin_id = _handles_de(totales)
        job["a_cambiar"] = len(cambios)
        job["ya_coinciden"] = iguales
        job["sin_identificador"] = sin_id
        job["subidas"] = sum(1 for _, _, n, v in cambios if v is not None and n > v)
        job["bajadas"] = sum(1 for _, _, n, v in cambios if v is not None and n < v)
        job["a_cero"] = sum(1 for _, _, n, _ in cambios if n == 0)
        job["muestra"] = [{"handle": h, "shopify": v, "odoo": n}
                          for h, _, n, v in cambios[:25]]

        if len(cambios) > TOPE_CORRIDA and not completo:
            job["status"] = "error"
            job["errors"].append(
                f"{len(cambios):,} cambios supera el tope de {TOPE_CORRIDA:,}. "
                f"No se escribe nada: revisar antes que la lectura de Odoo "
                f"sea correcta, o lanzar con completo=true a proposito.")
            return job

        if dry_run:
            job["stage"] = "dry_run_done"
            job["status"] = "completed"
            return job

        if limite:
            cambios = cambios[:limite]
        job["stage"] = "escribiendo en Shopify"
        loc = _location_gid()
        job["location"] = loc
        for i in range(0, len(cambios), LOTE):
            if job["status"] != "running":
                break
            lote = cambios[i:i + LOTE]
            try:
                n = _escribir(loc, lote, job)
                if n:
                    _marcar_escritos(lote)
                    job["escritos"] += n
            except Exception as e:
                job["errors"].append(f"lote@{i}: {type(e).__name__}: {str(e)[:140]}")
            if job["escritos"] % 5000 < LOTE:
                print(f"[ShopifyStock] {job['escritos']:,}/{len(cambios):,} "
                      f"({time.monotonic() - t0:.0f}s)", flush=True)

        # El marcapaginas solo avanza si no hubo fallos: si algo se quedo sin
        # escribir, la proxima corrida tiene que volver a verlo.
        if max_ts and not job["errors"] and job["status"] == "running":
            _guardar_marcador(max_ts, job["escritos"])
        job["stage"] = "done"
        if job["status"] == "running":
            job["status"] = "completed"
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e}"[:300])
        print(f"[ShopifyStock] FAIL: {e!r}")
    finally:
        job["elapsed_s"] = round(time.monotonic() - t0, 1)
        _audit("sincronizar" if not dry_run else "dry_run",
               f"Stock a Shopify{' [DRY RUN]' if dry_run else ''}: "
               f"{job['libros_tocados']:,} libros tocados en Odoo, "
               f"{job['a_cambiar']:,} distintos de la tienda, "
               f"{job['escritos']:,} escritos ({job['elapsed_s']}s)", job)
    return job


def _audit(evento: str, resumen: str, job: dict):
    try:
        import audit_log
        audit_log.log_event(
            "shopify_stock", evento, resumen,
            detalle={k: job.get(k) for k in
                     ("desde", "libros_tocados", "a_cambiar", "escritos",
                      "ya_coinciden", "sin_identificador", "subidas",
                      "bajadas", "a_cero",
                      "leidos", "guardados", "dry_run", "elapsed_s")},
            nivel="error" if job.get("status") == "error" else "info")
    except Exception:
        pass


if __name__ == "__main__":
    import asyncio
    import sys
    if "--exportar" in sys.argv:
        r = exportar_inventario()
        print(f"\n{r['status']}: {r['leidos']:,} variantes leidas, "
              f"{r['guardados']:,} guardadas ({r['elapsed_s']}s)")
    else:
        seco = "--apply" not in sys.argv
        r = asyncio.run(sincronizar(dry_run=seco, completo="--completo" in sys.argv))
        print(f"\n{'DRY RUN' if seco else 'SINCRONIZACION'}  {r['status']}  "
              f"({r['elapsed_s']}s)")
        print(f"   desde              {r['desde'] or '(todo)'}")
        print(f"   libros tocados     {r['libros_tocados']:>9,}")
        print(f"   distintos          {r['a_cambiar']:>9,}  "
              f"(suben {r['subidas']:,} · bajan {r['bajadas']:,} · "
              f"a cero {r['a_cero']:,})")
        print(f"   ya coinciden       {r['ya_coinciden']:>9,}")
        print(f"   sin identificador  {r['sin_identificador']:>9,}  (falta traerlos de Shopify)")
        print(f"   escritos           {r['escritos']:>9,}")
        for m in r["muestra"][:10]:
            print(f"      {m['handle']}  tienda={m['shopify']} -> odoo={m['odoo']}")
        for e in r["errors"][:5]:
            print(f"   ERROR: {e}")
