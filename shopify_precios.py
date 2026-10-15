"""
Mantener al dia el precio de la tienda.

POR QUE NO EXISTIA. El precio que un libro recibe el dia que se publica es
el que se queda para siempre, y no por un cron apagado: no habia codigo que
lo cambiase. Son cuatro candados en fila, cada uno suficiente por si solo:

    auto_scrape.py:119     WHERE ... AND m.barcode IS NULL
                           solo scrapea ISBN que NO estan en el espejo, asi
                           que a un libro ya dado de alta no se le vuelve a
                           preguntar el PVP nunca mas.

    shopify_pub.py:312     AND NOT EXISTS (SELECT 1 FROM shopify_productos
                                           WHERE handle = m.barcode)
                           solo publica los que no estan publicados.

    shopify_stock.py       cero menciones a precio: sincroniza cantidades.

    shopify_api.py         no tenia ninguna funcion para cambiar un precio.

Resultado: si el editor sube el PVP, seguimos vendiendo al precio viejo.

DE DONDE SALE EL PRECIO AHORA. De pvp_referencia, la misma tabla que usan
los marketplaces. Esa es la mitad importante del arreglo: hasta hoy la web
tiraba del PVP scrapeado y los marketplaces del fichero del proveedor, y
esa divergencia es la que produjo el problema del IVA. Una sola fuente y
dejan de poder separarse.

    precio de la web = web_price(pvp_referencia)   -- Capa 1 y nada mas

Sin Capa 3 ni el centimo: esos son de marketplace.

COMO SE ESCRIBE. productVariantsBulkUpdate pide el producto y cada libro
tiene una sola variante, asi que una llamada por libro serian 700.000.
Se sube un JSONL con una linea por cambio y Shopify lo aplica en su lado,
igual que la lectura masiva pero al reves.

SEGURIDAD. dry_run por defecto, y solo_subidas por defecto tambien: una
bajada de precio es la señal de que la referencia esta peor que lo que hay
publicado, y eso se mira a mano antes de aplicarlo. La corrida en seco
dice cuantas bajarian y cuanto, que es el numero que hay que leer primero.
"""
import os
import time
from datetime import datetime

import db
import pricing_engine
import shopify_api as sa

TABLA = "shopify_precio"
TABLA_REF = "pvp_referencia"

# Por debajo de esto no se toca: no merece una escritura ni mover el precio
# de un escaparate por dos centimos.
MINIMO_CAMBIO = float(os.environ.get("SHOPIFY_PRECIO_MIN_CAMBIO", "0.05"))
LOTE_JSONL = int(os.environ.get("SHOPIFY_PRECIO_LOTE", "50000"))

_job: dict | None = None
_parar = False

_MUTACION = (
    "mutation call($productId: ID!, "
    "$variants: [ProductVariantsBulkInput!]!) { "
    "productVariantsBulkUpdate(productId: $productId, variants: $variants) "
    "{ productVariants { id price } userErrors { field message } } }"
)

_BULK_PRECIOS = """
mutation {
  bulkOperationRunQuery(
    query: \"\"\"
    { productVariants {
        edges { node {
          id
          barcode
          price
          product { id handle }
        } }
    } }
    \"\"\"
  ) { bulkOperation { id status } userErrors { field message } }
}
"""


def get_status() -> dict:
    return dict(_job) if _job else {"status": "idle"}


def stop() -> bool:
    global _parar
    _parar = True
    return True


def ensure_schema():
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            CREATE TABLE IF NOT EXISTS {TABLA} (
                handle            TEXT PRIMARY KEY,
                variant_gid       TEXT,
                product_gid       TEXT,
                precio_tienda     NUMERIC(10,2),
                precio_objetivo   NUMERIC(10,2),
                escrito_en        TIMESTAMP,
                leido_en          TIMESTAMP
            )
        """)
        db.execute_query(cur, f"CREATE INDEX IF NOT EXISTS {TABLA}_obj_idx "
                              f"ON {TABLA} (precio_objetivo)")
        conn.commit()
    finally:
        conn.close()


# ── 1. Que precio tiene hoy cada libro en la tienda ─────────────────────
def exportar_precios(espera_s: int = 10, maximo_min: int = 60) -> dict:
    """
    Baja de Shopify, con una sola operacion masiva, el precio y los
    identificadores de cada variante. Sin esto no se sabe que cambiar.
    """
    ensure_schema()
    t0 = time.monotonic()
    d = sa.graphql(_BULK_PRECIOS)
    errores = d["bulkOperationRunQuery"]["userErrors"]
    if errores:
        raise sa.ShopifyError(f"no arranco la operacion masiva: {errores}")
    limite = time.time() + maximo_min * 60
    url = None
    while time.time() < limite:
        time.sleep(espera_s)
        est = sa.graphql("{ currentBulkOperation { status objectCount url "
                         "errorCode } }")
        op = est.get("currentBulkOperation") or {}
        if op.get("status") == "COMPLETED":
            url = op.get("url")
            break
        if op.get("status") in ("FAILED", "CANCELED"):
            raise sa.ShopifyError(f"operacion masiva {op.get('status')}: "
                                  f"{op.get('errorCode')}")
        print(f"[ShopPrecio] leyendo... {op.get('objectCount') or 0} objetos",
              flush=True)
    if not url:
        raise sa.ShopifyError("la operacion masiva no termino a tiempo")

    import json as _json
    import urllib.request
    from psycopg2.extras import execute_values
    conn = db.get_connection()
    cur = conn.cursor()
    leidos = 0
    lote: list = []
    try:
        with urllib.request.urlopen(url, timeout=900) as r:
            for linea in r:
                linea = linea.strip()
                if not linea:
                    continue
                n = _json.loads(linea)
                # El JSONL trae nodos de varios tipos; solo interesan las
                # variantes, que son las que llevan barcode.
                barcode = (n.get("barcode") or "").strip()
                if not barcode or not n.get("id"):
                    continue
                prod = n.get("product") or {}
                try:
                    precio = round(float(n.get("price") or 0), 2)
                except (TypeError, ValueError):
                    continue
                lote.append((barcode, n["id"], prod.get("id"), precio))
                leidos += 1
                if len(lote) >= 5000:
                    _guardar_leidos(cur, execute_values, lote)
                    conn.commit()
        if lote:
            _guardar_leidos(cur, execute_values, lote)
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "variantes": leidos,
            "elapsed_s": round(time.monotonic() - t0, 1)}


def _guardar_leidos(cur, execute_values, lote):
    execute_values(cur, f"""
        INSERT INTO {TABLA} (handle, variant_gid, product_gid,
                             precio_tienda, leido_en)
        VALUES %s
        ON CONFLICT (handle) DO UPDATE SET
            variant_gid = EXCLUDED.variant_gid,
            product_gid = EXCLUDED.product_gid,
            precio_tienda = EXCLUDED.precio_tienda,
            leido_en = NOW()
    """, lote, template="(%s,%s,%s,%s,NOW())", page_size=2000)
    lote.clear()


# ── 2. Que precio deberia tener ─────────────────────────────────────────
def _objetivos(solo_subidas: bool, limite: int | None):
    """
    Devuelve (handle, product_gid, variant_gid, precio_tienda, objetivo).

    Solo se mira lo que tiene referencia NO dudosa: un PVP que no hemos
    podido confirmar no entra en la tienda.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT s.handle, s.product_gid, s.variant_gid, s.precio_tienda,
                   r.pvp
            FROM {TABLA} s
            JOIN {TABLA_REF} r ON r.isbn = s.handle
            WHERE s.product_gid IS NOT NULL
              AND s.variant_gid IS NOT NULL
              AND r.pvp > 0
              AND NOT r.dudoso
        """)
        salida = []
        for handle, pgid, vgid, tienda, pvp in cur.fetchall():
            obj = pricing_engine.web_price(float(pvp))
            if obj is None:
                continue
            actual = float(tienda) if tienda is not None else None
            if actual is not None and abs(obj - actual) < MINIMO_CAMBIO:
                continue
            if solo_subidas and actual is not None and obj < actual:
                continue
            salida.append((handle, pgid, vgid, actual, obj))
            if limite and len(salida) >= limite:
                break
        return salida
    finally:
        conn.close()


def comparar(limite_muestra: int = 40) -> dict:
    """Que pasaria, sin escribir nada ni en Shopify ni en la base."""
    ensure_schema()
    todos = _objetivos(solo_subidas=False, limite=None)
    suben = [x for x in todos if x[3] is None or x[4] > x[3]]
    bajan = [x for x in todos if x[3] is not None and x[4] < x[3]]
    eur_s = round(sum(x[4] - (x[3] or 0) for x in suben), 2)
    eur_b = round(sum((x[3] or 0) - x[4] for x in bajan), 2)
    peor = sorted(bajan, key=lambda x: x[3] - x[4], reverse=True)[:limite_muestra]
    return {
        "status": "ok",
        "a_cambiar": len(todos), "suben": len(suben), "bajan": len(bajan),
        "euros_que_suben": eur_s, "euros_que_bajan": eur_b,
        "muestra_de_las_que_bajan": [
            {"isbn": h, "tienda": t, "pasaria_a": o,
             "diferencia": round(o - (t or 0), 2)}
            for h, _p, _v, t, o in peor],
        "nota": ("Las que bajan son la señal de alarma: significan que la "
                 "referencia esta por debajo de lo publicado. Con "
                 "solo_subidas=true no se tocan."),
    }


# ── 3. Escribirlo ───────────────────────────────────────────────────────
def sincronizar(dry_run: bool = True, solo_subidas: bool = True,
                limite: int | None = None) -> dict:
    global _job, _parar
    _parar = False
    sello = datetime.now()
    _job = {"status": "running", "dry_run": dry_run,
            "solo_subidas": solo_subidas, "fase": "comparando",
            "started_at": sello.isoformat(), "a_cambiar": 0, "enviados": 0,
            "suben": 0, "bajan": 0, "euros_que_suben": 0.0,
            "lotes": 0, "errors": [], "elapsed_s": 0}
    job = _job
    t0 = time.monotonic()
    try:
        ensure_schema()
        cambios = _objetivos(solo_subidas, limite)
        job["a_cambiar"] = len(cambios)
        for _h, _p, _v, actual, obj in cambios:
            if actual is None or obj > actual:
                job["suben"] += 1
                job["euros_que_suben"] += obj - (actual or 0)
            else:
                job["bajan"] += 1
        job["euros_que_suben"] = round(job["euros_que_suben"], 2)

        if dry_run:
            job["fase"] = "dry_run_done"
            job["status"] = "completed"
            job["muestra"] = [
                {"isbn": h, "tienda": t, "pasaria_a": o}
                for h, _p, _v, t, o in cambios[:40]]
            return job

        import json as _json
        job["fase"] = "escribiendo"
        for i in range(0, len(cambios), LOTE_JSONL):
            if _parar:
                job["errors"].append("parado a mano")
                break
            trozo = cambios[i:i + LOTE_JSONL]
            lineas = [_json.dumps({
                "productId": pgid,
                "variants": [{"id": vgid, "price": f"{obj:.2f}"}],
            }) for _h, pgid, vgid, _t, obj in trozo]
            ruta = sa.subir_jsonl(lineas, f"precios_{i//LOTE_JSONL:03d}.jsonl")
            res = sa.mutacion_masiva(_MUTACION, ruta)
            job["lotes"] += 1
            job["enviados"] += len(trozo)
            if res.get("url_errores"):
                job["errors"].append(f"lote {job['lotes']}: hay errores "
                                     f"parciales en {res['url_errores']}")
            _marcar_escritos(trozo)

        job["fase"] = "done"
        job["status"] = "completed"
        print(f"[ShopPrecio] {job['enviados']:,} precios enviados en "
              f"{job['lotes']} lotes")
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e}"[:300])
        print(f"[ShopPrecio] FAIL: {e!r}")
    finally:
        job["elapsed_s"] = round(time.monotonic() - t0, 2)
    return job


def _marcar_escritos(trozo):
    from psycopg2.extras import execute_values
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        execute_values(cur, f"""
            UPDATE {TABLA} s SET precio_objetivo = v.obj,
                                 precio_tienda = v.obj,
                                 escrito_en = NOW()
            FROM (VALUES %s) AS v(handle, obj)
            WHERE s.handle = v.handle
        """, [(h, o) for h, _p, _v, _t, o in trozo],
            template="(%s,%s::numeric)", page_size=2000)
        conn.commit()
    finally:
        conn.close()


# ── 4. Cron ─────────────────────────────────────────────────────────────
CRON_HORAS = float(os.environ.get("SHOPIFY_PRECIO_CRON_HORAS", "24"))
CRON_ACTIVO = os.environ.get("SHOPIFY_PRECIO_CRON_ENABLED", "0") == "1"

_cron: dict = {"running": False, "ultima": None, "ultimo_resultado": None,
               "runs_total": 0, "apagado_por_config": not CRON_ACTIVO}
_cron_task = None


def get_cron_status() -> dict:
    return dict(_cron)


def _ciclo_en_su_hilo() -> dict:
    """
    Lee la tienda y escribe lo que haya cambiado.

    Va en su propio hilo con su propio bucle: es trabajo sincrono de varios
    minutos y hacerlo en el del servidor deja el panel colgado. Ya paso el
    13/10 con el catalogo de AZETA.
    """
    import asyncio
    bucle = asyncio.new_event_loop()
    asyncio.set_event_loop(bucle)
    try:
        leido = exportar_precios()
        escrito = sincronizar(dry_run=False, solo_subidas=True)
        return {"leido": leido, "escrito": {
            k: escrito.get(k) for k in
            ("status", "a_cambiar", "enviados", "suben", "bajan",
             "euros_que_suben", "errors")}}
    finally:
        bucle.close()


async def _cron_loop():
    import asyncio
    while True:
        try:
            _cron["running"] = True
            r = await asyncio.to_thread(_ciclo_en_su_hilo)
            _cron["ultimo_resultado"] = r
            _cron["runs_total"] += 1
            _cron["ultima"] = datetime.now().isoformat()
            print(f"[ShopPrecio] cron: "
                  f"{r['escrito'].get('enviados', 0):,} precios actualizados")
        except Exception as e:
            _cron["ultimo_resultado"] = {"status": "error", "error": str(e)[:200]}
            print(f"[ShopPrecio] cron FAIL: {e!r}")
        finally:
            _cron["running"] = False
        await asyncio.sleep(CRON_HORAS * 3600)


def start_cron() -> bool:
    """
    Arranca el cron. La variable de entorno manda sobre el vigilante: si
    esta apagado a proposito, nadie lo vuelve a encender por su cuenta.
    """
    global _cron_task
    import asyncio
    if not CRON_ACTIVO:
        _cron["apagado_por_config"] = True
        return False
    if _cron_task and not _cron_task.done():
        return False
    _cron_task = asyncio.create_task(_cron_loop())
    _cron["apagado_por_config"] = False
    return True


def stop_cron() -> bool:
    global _cron_task
    if _cron_task and not _cron_task.done():
        _cron_task.cancel()
        _cron_task = None
        return True
    return False
