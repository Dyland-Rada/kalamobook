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
  - Cada corrida repasa el catalogo ENTERO. Existe un modo incremental que
    solo pregunta por lo que se movio, pero no se usa por defecto y conviene
    saber por que: en Odoo, cuando un libro se queda sin existencias su
    registro de stock DESAPARECE en vez de quedarse a cero. Un libro asi no
    sale en ninguna lectura de lo que ha cambiado, asi que el incremental
    nunca lo bajaria y la tienda lo seguiria vendiendo. Solo la completa
    recorre todo lo mapeado y da por cero lo que no aparece.
    Leer Odoo entero son 3,5 minutos de los 60 que hay entre corridas.
  - Un libro que esta en Shopify y no lo tenemos mapeado NO se toca.

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
# Shopify guarda dos cifras: on_hand es lo que hay, y available es lo
# vendible, que es on_hand menos lo comprometido por pedidos sin servir.
# Se escribe on_hand y se deja que Shopify calcule available. Escribir
# available directamente pisa esa resta: con pedidos pendientes -habia 6 el
# 19/08- el inventario se descuadra en cuanto se sirve uno. Cuando la verdad
# viene de fuera, como aqui, lo que se dicta es el fisico.
CAMPO = os.environ.get("SHOPIFY_STOCK_CAMPO", "on_hand")
PAGINA_QUANT = 40000
# Segundos entre lotes de escritura, para no chocar con el limite de coste.
PAUSA_LOTE = float(os.environ.get("SHOPIFY_STOCK_PAUSA", "0.6"))

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


def _handles_de(totales: dict[int, float],
                incluir_ausentes: bool = False) -> tuple[list[tuple], int, int]:
    """
    Devuelve (cambios, ya_coinciden, sin_identificador), donde cambios es
    [(handle, inventory_item_gid, qty_odoo, qty_shopify)].

    Los tres se cuentan por separado a proposito. Restar "los que cambian"
    del total mezcla dos cosas distintas: un libro que ya esta bien y un
    libro al que no podemos escribir porque no tenemos su identificador. El
    primero no hay que hacer nada con el; el segundo es trabajo pendiente.

    incluir_ausentes recorre TODO lo que tenemos mapeado en vez de solo lo
    que Odoo devolvio, y da por cero lo que no aparece. Hace falta y no es
    un detalle: cuando en Odoo un libro se queda sin existencias su quant
    desaparece, no se queda en cero. Un libro asi no sale en ninguna lectura
    de quants, asi que sin esto la tienda se quedaria vendiendolo para
    siempre. Es justo el fallo caro -se compra algo que nadie puede servir-
    y lo destapo "El Principito" el 19/08: cero en Odoo desde las pausas del
    dia 10 y a la venta en la tienda.
    """
    if not totales and not incluir_ausentes:
        return [], 0, 0
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        cambios, iguales, encontrados = [], 0, 0
        if incluir_ausentes:
            db.execute_query(cur, f"""
                SELECT m.odoo_id, m.barcode, s.inventory_item_gid, s.qty_shopify
                FROM {TABLA} s
                JOIN odoo_books_mirror m ON m.barcode = s.handle
                WHERE s.inventory_item_gid IS NOT NULL
                  AND m.odoo_id IS NOT NULL
            """)
            filas = cur.fetchall()
            for odoo_id, handle, item, qty_shop in filas:
                encontrados += 1
                nuevo = int(round(totales.get(int(odoo_id), 0.0)))
                if qty_shop is not None and nuevo == qty_shop:
                    iguales += 1
                else:
                    cambios.append((handle, item, nuevo, qty_shop))
            return cambios, iguales, 0

        ids = list(totales)
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


_loc_cache: str | None = None


def _location_gid() -> str:
    """
    La ubicacion de la tienda.

    Se pedia con `locations { id name }` y fallaba entero: `name` exige el
    permiso read_locations, que esta app no tiene, y Shopify rechaza toda la
    consulta por un campo. Solo hace falta el id.

    Tres vias por orden, porque los permisos de la app pueden cambiar y esto
    no puede tumbar la sincronizacion:
      1. SHOPIFY_LOCATION_GID, si esta puesta
      2. locations pidiendo unicamente el id
      3. deducirla del inventario de un libro cualquiera, que solo necesita
         el permiso de inventario, el mismo con el que despues escribimos
    """
    global _loc_cache
    if _loc_cache:
        return _loc_cache
    fijo = os.environ.get("SHOPIFY_LOCATION_GID")
    if fijo:
        _loc_cache = fijo
        return fijo

    try:
        d = sa.graphql("{ locations(first: 5) { edges { node { id } } } }")
        nodos = [e["node"] for e in d["locations"]["edges"]]
        if nodos:
            _loc_cache = nodos[0]["id"]
            return _loc_cache
    except Exception as e:
        print(f"[ShopifyStock] no se pudo listar ubicaciones ({str(e)[:90]}); "
              f"se deduce del inventario")

    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT inventory_item_gid FROM {TABLA}
            WHERE inventory_item_gid IS NOT NULL LIMIT 1
        """)
        r = cur.fetchone()
    finally:
        conn.close()
    if r:
        d = sa.graphql(
            "query($id: ID!) { inventoryItem(id: $id) { inventoryLevels(first: 5)"
            " { edges { node { location { id } } } } } }", {"id": r[0]})
        bordes = ((d.get("inventoryItem") or {}).get("inventoryLevels")
                  or {}).get("edges") or []
        if bordes:
            _loc_cache = bordes[0]["node"]["location"]["id"]
            return _loc_cache

    raise sa.ShopifyError(
        "no se pudo averiguar la ubicacion de la tienda. Ponla a mano en "
        "SHOPIFY_LOCATION_GID (se ve en Shopify: Configuracion > Ubicaciones)")


# Si la API acepta ignoreCompareQuantity. None = aun no lo sabemos.
#
# Este campo ha dado dos errores CONTRARIOS y por eso se decide en caliente:
#
#   sin el   "El argumento compareQuantity debe proporcionarse para cada
#            cantidad o ignorarse mediante ignoreCompareQuantity"
#   con el   "was provided invalid value for ignoreCompareQuantity
#            (Field is not defined on InventorySetQuantitiesInput)"
#
# No es contradictorio: son versiones distintas de la API. En la que usa el
# conector existe y es obligatorio; en la 2026-07, que es la del servidor,
# no existe y la comprobacion se salta omitiendo compareQuantity en cada
# cantidad. Fijarlo a mano rompe en cuanto alguien toca SHOPIFY_API_VERSION,
# asi que se prueba una vez y se recuerda para el resto de la corrida.
_usa_ignore: bool | None = None


def _entrada(loc: str, lote: list[tuple], con_ignore: bool) -> dict:
    e = {
        "name": CAMPO,
        "reason": "correction",
        "referenceDocumentUri": "gid://kalamobook/SyncJob/stock",
        "quantities": [{"inventoryItemId": item, "locationId": loc,
                        "quantity": qty} for _, item, qty, _ in lote],
    }
    if con_ignore:
        e["ignoreCompareQuantity"] = True
    return e


def _escribir(loc: str, lote: list[tuple], job: dict) -> int:
    global _usa_ignore
    intentos = [_usa_ignore] if _usa_ignore is not None else [False, True]
    ultimo = None
    for con_ignore in intentos:
        try:
            d = sa.graphql(_MUT_SET, {"input": _entrada(loc, lote, con_ignore)})
        except Exception as e:
            texto = str(e)
            # Los dos sintomas del desajuste de version: se reintenta con la
            # otra forma en vez de dar el lote por perdido.
            if "ignoreCompareQuantity" in texto or "compareQuantity" in texto:
                ultimo = texto
                continue
            raise
        errores = (d.get("inventorySetQuantities") or {}).get("userErrors") or []
        if errores:
            texto = str(errores)
            if "ompareQuantity" in texto and _usa_ignore is None:
                ultimo = texto
                continue
            job["errors"].append(f"lote: {errores[:3]}")
            return 0
        if _usa_ignore is None:
            _usa_ignore = con_ignore
            job["ignore_compare_quantity"] = con_ignore
            print(f"[ShopifyStock] la API {'exige' if con_ignore else 'no admite'} "
                  f"ignoreCompareQuantity; se usa asi el resto de la corrida")
        return len(lote)
    job["errors"].append(f"lote: no se pudo escribir de ninguna de las dos "
                         f"formas: {str(ultimo)[:160]}")
    return 0


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
        # Sin marcapaginas no hay nada de lo que ser incremental: se lee Odoo
        # entero igual. Si ademas se deja completo=False, el tope de seguridad
        # aborta la corrida, el marcapaginas no avanza, y la siguiente vuelve
        # a empezar de cero: bucle. Paso los dias 19 y 20, con cuatro corridas
        # seguidas informando "218.487 distintos, 0 escritos" sin que nadie
        # viera el motivo. Si no hay marcador, la corrida ES completa.
        if desde is None and not completo:
            completo = True
            job["completo_forzado"] = True
            print("[ShopifyStock] sin marcapaginas: se trata como completa")
        job["desde"] = desde
        job["stage"] = "leyendo Odoo"
        async with OdooClient() as odoo:
            totales, max_ts = await _stock_odoo(odoo, desde, job)
        job["libros_tocados"] = len(totales)

        job["stage"] = "comparando con la tienda"
        cambios, iguales, sin_id = _handles_de(totales, incluir_ausentes=completo)
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
            print(f"[ShopifyStock] ABORTADO por el tope: {job['errors'][-1]}")
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
            # Respiro entre lotes. Shopify cobra por coste, no por peticiones,
            # y una escritura de 250 cantidades sale cara: sin pausa, novecientas
            # llamadas seguidas acaban limitadas aunque haya reintento.
            time.sleep(PAUSA_LOTE)
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
                      "bajadas", "a_cero", "errors", "completo_forzado",
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


# ── Cron ────────────────────────────────────────────────────────────────
# Dos ritmos, y no son intercambiables:
#
#   cada hora   incremental, para el goteo del dia
#   una vez al  completa, porque es la UNICA que detecta los libros que se
#   dia         quedaron sin existencias: en Odoo su registro desaparece y
#               la incremental no tiene nada que leer. Sin esto, la tienda
#               acabaria vendiendo libros que ya no sirve nadie.
#
# La completa va de madrugada: son 15-20 minutos y a esa hora ya han
# entrado los ficheros de la noche.

CRON_INTERVAL_S = int(os.environ.get("SHOPIFY_STOCK_CRON_INTERVAL_S", "3600"))
HORA_COMPLETA = int(os.environ.get("SHOPIFY_STOCK_HORA_COMPLETA", "4"))
# Todas las corridas completas, no solo la de las 4:00.
#
# La rapida no puede ver los libros que se quedan SIN existencias, porque en
# Odoo su registro de stock desaparece y no hay nada que leer. Con una sola
# completa al dia, un libro que se agota a las 5 de la manana sigue a la
# venta 23 horas. Eso es exactamente el stock fantasma que costo la venta de
# Fnac, solo que con menos ventana.
#
# Leer Odoo entero son unos 3,5 minutos de los 60 que hay entre corridas, y
# una vez saldada la deuda las diferencias por hora son de cientos, no de
# miles. El coste es asumible y cierra el agujero del todo.
SIEMPRE_COMPLETO = os.environ.get(
    "SHOPIFY_STOCK_SIEMPRE_COMPLETO", "1").lower() in ("1", "true", "yes")

_cron_task = None
_cron_state: dict = {
    "enabled": False, "interval_s": CRON_INTERVAL_S,
    "hora_completa": HORA_COMPLETA, "last_run_at": None, "last_mode": None,
    "last_summary": None, "next_run_at": None, "runs_total": 0,
    "completas": 0, "errors": [],
}


def get_cron_status() -> dict:
    out = dict(_cron_state)
    out["errors"] = out.get("errors", [])[-10:]
    out["task_running"] = bool(_cron_task and not _cron_task.done())
    return out


async def _cron_loop():
    import asyncio as _a
    from datetime import timedelta
    print(f"[ShopifyStockCron] Arrancado, cada {CRON_INTERVAL_S}s, "
          f"completa a las {HORA_COMPLETA}:00")
    ultima_completa = None
    while _cron_state["enabled"]:
        try:
            hoy = datetime.now().date()
            hora = datetime.now().hour
            # La completa, una vez al dia. Se apunta el dia en que se hizo
            # para que un reinicio a esa misma hora no la repita.
            # Todas las corridas son completas salvo que se diga lo
            # contrario. Ver SIEMPRE_COMPLETO.
            completo = SIEMPRE_COMPLETO or (hora == HORA_COMPLETA
                                            and ultima_completa != hoy)
            r = await sincronizar(dry_run=False, completo=completo)
            if completo and r.get("status") == "completed":
                ultima_completa = hoy
                _cron_state["completas"] += 1
            _cron_state["last_run_at"] = datetime.now().isoformat()
            _cron_state["last_mode"] = "completa" if completo else "rapida"
            _cron_state["last_summary"] = (
                f"{r.get('a_cambiar', 0):,} distintos, "
                f"{r.get('escritos', 0):,} escritos")
            _cron_state["runs_total"] += 1
            print(f"[ShopifyStockCron] #{_cron_state['runs_total']} "
                  f"({_cron_state['last_mode']}): {_cron_state['last_summary']}")
        except Exception as e:
            _cron_state["errors"].append(f"{type(e).__name__}: {e!r}"[:200])
            print(f"[ShopifyStockCron] fallo: {e!r}")
        _cron_state["next_run_at"] = (
            datetime.now() + timedelta(seconds=CRON_INTERVAL_S)).isoformat()
        for _ in range(CRON_INTERVAL_S):
            if not _cron_state["enabled"]:
                break
            await _a.sleep(1)
    print("[ShopifyStockCron] Detenido")
    _cron_state["next_run_at"] = None


def start_cron() -> bool:
    global _cron_task
    import asyncio as _a
    if _cron_task and not _cron_task.done():
        return False
    _cron_state["enabled"] = True
    _cron_state["errors"] = []
    try:
        _cron_task = _a.create_task(_cron_loop())
        return True
    except RuntimeError:
        _cron_state["enabled"] = False
        return False


def stop_cron() -> bool:
    if not _cron_state["enabled"]:
        return False
    _cron_state["enabled"] = False
    return True
