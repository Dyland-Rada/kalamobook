"""
Vigilante: comprueba que el sistema esta vivo, arregla lo que es seguro
arreglar y deja constancia de lo que no puede tocar.

Es a proposito determinista, no una IA: lo que falla aqui es predecible y se
detecta con reglas. Un modelo que decida reiniciar cosas en produccion es un
riesgo que no compensa.

Lo que SI puede arreglar solo:
  - Un cron parado tras un reinicio -> lo arranca
  - Un lock del sync atascado por un deploy -> lo libera

Lo que solo puede reportar:
  - Un proveedor que dejo de mandar ficheros (el problema esta en Server A)
  - Odoo, Shopify o la base de datos caidos
  - Errores repetidos del sync

Cada revision queda en event_log, y las incidencias se ven en la pestana
Salud del panel. No manda nada a ningun sitio: se consulta cuando se
quiere, no interrumpe.
"""
import asyncio
import os
import time
from datetime import datetime

import db

INTERVALO_S = int(os.environ.get("VIGILANTE_INTERVALO_S", "43200"))  # 12 h
CEGALD_DIAS_AVISO = float(os.environ.get("VIGILANTE_CEGALD_DIAS", "3"))
LOCK_MIN_AVISO = int(os.environ.get("VIGILANTE_LOCK_MIN", "45"))
ERRORES_AVISO = int(os.environ.get("VIGILANTE_ERRORES", "50"))

# Quien ejecuta la revision. Va en cada apunte del historial para que una
# prueba desde otra maquina no se confunda con lo que pasa en produccion.
ORIGEN = os.environ.get("WORKER_NAME", "?")
# El proceso del servidor es el unico que puede opinar sobre los crones,
# porque su estado vive en memoria. Lo marca el mismo interruptor que los
# arranca al inicio.
EN_SERVIDOR = os.environ.get("VIGILANTE_ENABLED", "").lower() in ("1", "true", "yes")

OK, AVISO, ERROR = "ok", "aviso", "error"

_ultima: dict | None = None
_cron_task = None
_cron_state: dict = {
    "enabled": False, "interval_s": INTERVALO_S, "last_run_at": None,
    "runs_total": 0, "errors": [],
}
_estado_previo: dict[str, str] = {}


def get_estado() -> dict:
    return dict(_ultima) if _ultima else {"status": "sin_revisar"}


def get_cron_status() -> dict:
    out = dict(_cron_state)
    out["task_running"] = bool(_cron_task and not _cron_task.done())
    return out


def _chequeo(clave, titulo, estado, detalle, arreglado=None, accion=None):
    return {"clave": clave, "titulo": titulo, "estado": estado,
            "detalle": detalle, "arreglado": arreglado, "accion": accion}


# ─── Comprobaciones ──────────────────────────────────────────────────
def _revisar_crones(arreglar: bool) -> list[dict]:
    """
    Los tres crones que deben estar siempre en marcha.

    OJO: el estado de un cron vive en la memoria del proceso que lo ejecuta.
    Si el vigilante corre fuera del servidor —una prueba desde un portatil—
    ve sus propios crones, que no existen, y los daria por caidos. Eso paso
    el 04/08 y metio tres errores falsos en el historial compartido. Por eso
    fuera del servidor este chequeo no opina.
    """
    salida = []
    if not EN_SERVIDOR:
        return [_chequeo("crones", "Crones", OK,
                         "no se comprueban: esta revision no corre en el servidor")]
    definicion = [
        ("cron_azeta", "Cron de stock AZETA", "azeta_push_odoo",
         "get_cron_status", "start_stock_cron"),
        ("cron_sinli", "Cron del sync SINLI", "sync_stock_sinli",
         "get_cron_status", "start_cron"),
        ("cron_nuevos", "Cron de libros nuevos", "auto_scrape",
         "get_cron_status", "start_cron"),
        # Estos dos faltaban, y se noto: el 24/08 el servidor se reinicio y
        # volvieron todos menos el de Shopify, que se quedo parado sin que
        # nadie se enterara. El stock de la tienda llevaba dias sin
        # actualizarse por eso. Si el vigilante no los vigila, no hay quien
        # los levante salvo que alguien mire el panel.
        ("cron_shopify_stock", "Cron de stock a Shopify", "shopify_stock",
         "get_cron_status", "start_cron"),
        ("cron_catalogo", "Cron del catalogo publicable", "catalogo_publicable",
         "get_cron_status", "start_cron"),
        # Y este faltaba tambien. La ficha de los libros de AZETA —titulo,
        # autor, editorial, portada, sinopsis— se quedo congelada del 08/09
        # al 08/10 de 2026 y el vigilante daba 11 de 11 en verde todo ese
        # mes, porque no la miraba. El stock si estaba vigilado; la ficha no.
        ("cron_catalogo_azeta", "Cron del catalogo de AZETA", "azeta_catalog",
         "get_cron_status", "start_cron"),
        ("cron_feed_azeta", "Cron del feed de descuentos AZETA", "azeta_feed",
         "get_cron_status", "start_cron"),
        # Nace apagado (SHOPIFY_PRECIO_CRON_ENABLED). Se vigila igual para
        # que el dia que se encienda no se quede caido en silencio, que es
        # justo lo que le paso al catalogo de AZETA durante un mes.
        ("cron_shopify_precios", "Cron de precios a Shopify", "shopify_precios",
         "get_cron_status", "start_cron"),
        # El que arregla el caso del 15/10: AZETA tarifaba a 32,00 desde el
        # dia 13 y Odoo seguia en 28,00 porque el empuje del precio era
        # manual y no lo lanzaba nadie.
        ("cron_precios", "Cron de precios a Odoo", "pricing_engine",
         "get_cron_status", "start_cron"),
        # Nuevo el 19/10/2026. Hasta ese dia solo corria a mano, y por eso
        # el campo llevaba meses a medias: Grupo Anaya con CERO relaciones
        # y AZETA saliendo como proveedor de sus libros.
        ("cron_proveedores_odoo", "Cron de proveedores en Odoo",
         "odoo_proveedores", "get_cron_status", "start_cron"),
        # El supervisor del enriquecedor. No es un cron de intervalo -mira
        # si el trabajo esta caido habiendo cola-, pero se vigila igual:
        # estuvo parado del 09/06 al 19/10/2026, cuatro meses, sin que
        # nadie se enterara.
        ("cron_enriquecedor", "Supervisor del enriquecedor", "enrichment",
         "get_cron_status", "start_cron"),
    ]
    for clave, titulo, modulo, fn_estado, fn_arrancar in definicion:
        try:
            mod = __import__(modulo)
            est = getattr(mod, fn_estado)()
            vivo = bool(est.get("enabled") and est.get("task_running"))
            if vivo:
                salida.append(_chequeo(clave, titulo, OK, "en marcha"))
                continue
            # Apagado a proposito por su variable: ni es un fallo ni hay que
            # resucitarlo. Arrancarlo aqui seria pasar por encima de quien lo
            # apago, que es justo lo que no debe hacer un vigilante.
            if est.get("apagado_por_config"):
                salida.append(_chequeo(
                    clave, titulo, OK, "apagado a proposito por su variable"))
                continue
            arreglado = False
            if arreglar:
                try:
                    arreglado = bool(getattr(mod, fn_arrancar)())
                except Exception as e:
                    print(f"[Vigilante] no pude arrancar {clave}: {e}")
            salida.append(_chequeo(
                clave, titulo, OK if arreglado else ERROR,
                "estaba parado, lo he arrancado" if arreglado else "PARADO",
                arreglado=arreglado,
                accion=None if arreglado else "Arrancalo desde su tarjeta"))
        except Exception as e:
            salida.append(_chequeo(clave, titulo, ERROR,
                                   f"no se pudo consultar: {type(e).__name__}"))
    return salida


def _revisar_lock(arreglar: bool) -> dict:
    """Un lock cogido mucho rato suele ser un proceso muerto en un deploy."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT entidad, lock_activo,
                   EXTRACT(EPOCH FROM (NOW() - lock_desde))/60
            FROM sync_state WHERE lock_activo = true
        """)
        filas = cur.fetchall()
        atascados = [(r[0], int(r[2] or 0)) for r in filas
                     if (r[2] or 0) > LOCK_MIN_AVISO]
        if not atascados:
            return _chequeo("lock", "Locks de sincronizacion", OK,
                            "ninguno atascado" if not filas
                            else f"{len(filas)} en uso normal")
        nombres = ", ".join(f"{e} ({m} min)" for e, m in atascados)
        if arreglar:
            for entidad, _ in atascados:
                db.execute_query(cur, """
                    UPDATE sync_state SET lock_activo = false WHERE entidad = ?
                """, (entidad,))
            conn.commit()
            return _chequeo("lock", "Locks de sincronizacion", OK,
                            f"liberado: {nombres}", arreglado=True)
        return _chequeo("lock", "Locks de sincronizacion", ERROR,
                        f"atascado: {nombres}",
                        accion="Se libera solo, o desde aqui con Revisar ahora")
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return _chequeo("lock", "Locks de sincronizacion", ERROR,
                        f"{type(e).__name__}: {str(e)[:80]}")
    finally:
        conn.close()


def _revisar_proveedores() -> dict:
    """Proveedores que llevan dias sin mandar nada. Solo se puede avisar."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT m.nombre_proveedor,
                   EXTRACT(EPOCH FROM (NOW() - MAX(a.procesado_en)))/86400 AS dias
            FROM proveedor_almacen_odoo m
            LEFT JOIN sinli_auditoria a ON a.email_canonico = m.proveedor_email
            GROUP BY m.nombre_proveedor
            HAVING MAX(a.procesado_en) IS NOT NULL
               AND EXTRACT(EPOCH FROM (NOW() - MAX(a.procesado_en)))/86400
                   > {CEGALD_DIAS_AVISO}
            ORDER BY 2 DESC
        """)
        mudos = [(r[0], int(r[1])) for r in cur.fetchall()]
        if not mudos:
            return _chequeo("proveedores", "Entrada de ficheros", OK,
                            "todos han mandado algo estos dias")
        txt = ", ".join(f"{n} ({d}d)" for n, d in mudos[:6])
        return _chequeo("proveedores", "Entrada de ficheros", AVISO,
                        f"{len(mudos)} sin mandar nada: {txt}",
                        accion="El problema esta en la entrada (Server A), no aqui")
    except Exception as e:
        return _chequeo("proveedores", "Entrada de ficheros", AVISO,
                        f"no se pudo comprobar: {type(e).__name__}")
    finally:
        conn.close()


def _revisar_sync() -> dict:
    """Errores recientes del sync y pendientes acumulados."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT COUNT(*) FROM sync_errores
            WHERE creado_en > NOW() - INTERVAL '2 hours'
        """)
        errores = int(cur.fetchone()[0])
        if errores > ERRORES_AVISO:
            return _chequeo("sync", "Sync de stock", AVISO,
                            f"{errores:,} errores en las ultimas 2 horas",
                            accion="Mira los errores recientes en su tarjeta")
        return _chequeo("sync", "Sync de stock", OK,
                        "sin errores" if not errores else f"{errores} errores, normal")
    except Exception as e:
        return _chequeo("sync", "Sync de stock", AVISO,
                        f"no se pudo comprobar: {type(e).__name__}")
    finally:
        conn.close()


async def _revisar_odoo() -> dict:
    try:
        from odoo_client import OdooClient
        async with OdooClient() as o:
            n = await o.search_count("product.template", [["active", "=", True]])
        return _chequeo("odoo", "Conexion con Odoo", OK,
                        f"{n:,} productos activos")
    except Exception as e:
        return _chequeo("odoo", "Conexion con Odoo", ERROR,
                        f"{type(e).__name__}: {str(e)[:90]}",
                        accion="Sin Odoo no se puede sincronizar nada")


def _revisar_shopify() -> dict:
    try:
        import shopify_api as sa
        if not (sa.CLIENT_ID and sa.CLIENT_SECRET):
            return _chequeo("shopify", "Conexion con Shopify", AVISO,
                            "sin credenciales configuradas")
        return _chequeo("shopify", "Conexion con Shopify", OK,
                        f"{sa.contar_productos():,} productos en la tienda")
    except Exception as e:
        return _chequeo("shopify", "Conexion con Shopify", ERROR,
                        f"{type(e).__name__}: {str(e)[:90]}")


def _revisar_bd() -> dict:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        t0 = time.monotonic()
        db.execute_query(cur, "SELECT 1")
        cur.fetchone()
        ms = int((time.monotonic() - t0) * 1000)
        db.execute_query(cur, "SELECT pg_size_pretty(pg_database_size(current_database()))")
        tam = cur.fetchone()[0]
        return _chequeo("bd", "Base de datos", OK, f"{tam}, responde en {ms} ms")
    except Exception as e:
        return _chequeo("bd", "Base de datos", ERROR,
                        f"{type(e).__name__}: {str(e)[:90]}")
    finally:
        try: conn.close()
        except Exception: pass



# ─── Coherencia del negocio ──────────────────────────────────────────
# Las siete comprobaciones de arriba miran infraestructura: crones vivos,
# locks sueltos, ficheros que entran, Odoo y Shopify respondiendo. Ninguna
# pregunta si lo que hacemos tiene sentido.
#
# El 16/10/2026 el parte daba 14 de 15 en verde mientras AbeBooks vendia
# por segunda vez un libro sin existencias desde el 9, UDL llevaba meses
# sin fuente de stock y DISMALIBRO mandaba 12.318 libros a diario que no
# llegaban a ningun almacen. Nada de eso es un cron caido ni un servicio
# muerto, asi que nada de eso se veia.
#
# Estas cuatro comprobaciones son las que lo habrian cazado sin que nadie
# lo reportara desde atencion al cliente.

def _revisar_ventas_sin_stock() -> dict:
    """
    Reservas abiertas de libros que el catalogo da a cero.

    Una reserva nace cuando entra un pedido. Si el libro esta a 0, es que
    se vendio algo que no tenemos: o la oferta sigue viva en un marketplace
    que no hemos podido apagar, o el proveedor se quedo sin existencias
    entre la venta y ahora.

    Lo que distingue un caso del otro es `confirmado_en`: si el proveedor
    lo confirmo hace horas, es la carrera normal del dropship; si lleva
    dias sin confirmarse, la oferta esta encallada.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT r.canal, COUNT(*),
                   COUNT(*) FILTER (
                       WHERE c.confirmado_en IS NULL
                          OR c.confirmado_en < NOW() - INTERVAL '2 days')
            FROM reserva_stock r
            LEFT JOIN catalogo_publicable c ON c.isbn = r.isbn
            WHERE r.liberada_en IS NULL
              AND COALESCE(c.stock, 0) = 0
            GROUP BY r.canal
            ORDER BY 2 DESC
        """)
        filas = cur.fetchall()
        if not filas:
            return _chequeo("ventas_sin_stock", "Ventas sin stock", OK,
                            "ninguna reserva abierta sobre un libro a cero")
        total = sum(int(r[1]) for r in filas)
        rancias = sum(int(r[2]) for r in filas)
        txt = ", ".join(f"{r[0]} {int(r[1])}" for r in filas)
        # Las rancias son las graves: el proveedor no lo confirma desde
        # hace dias y aun asi se sigue vendiendo.
        estado = ERROR if rancias else AVISO
        detalle = f"{total} reservas abiertas sobre libros a 0 ({txt})"
        if rancias:
            detalle += f"; {rancias} llevan mas de 2 dias sin confirmarse"
        return _chequeo(
            "ventas_sin_stock", "Ventas sin stock", estado, detalle,
            accion=("Mira /api/v1/reservas. Si el libro lleva dias a cero, "
                    "la oferta esta encallada en el marketplace")
            if rancias else None)
    except Exception as e:
        return _chequeo("ventas_sin_stock", "Ventas sin stock", AVISO,
                        f"no se pudo comprobar: {type(e).__name__}")
    finally:
        conn.close()


def _revisar_proveedores_mudos_con_almacen() -> dict:
    """
    Proveedores con almacen dado de alta y CERO libros con stock.

    Es el caso de UDL: almacen UDL01 listo, mapeo listo, y ni una linea en
    libros_proveedor porque solo manda ESTADO y ENVIO, nunca CEGALD. Manda
    ficheros a diario, asi que la comprobacion de frescura lo da por bueno;
    simplemente no traen stock.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT m.nombre_proveedor, m.proveedor_email
            FROM proveedor_almacen_odoo m
            WHERE NOT EXISTS (
                SELECT 1 FROM libros_proveedor lp
                WHERE lp.proveedor_email = m.proveedor_email
                  AND lp.stock_disponible > 0)
            ORDER BY 1
        """)
        mudos = [(r[0] or r[1]) for r in cur.fetchall()]
        if not mudos:
            return _chequeo("prov_sin_stock", "Proveedores sin stock", OK,
                            "todos los que tienen almacen aportan libros")
        return _chequeo(
            "prov_sin_stock", "Proveedores sin stock", AVISO,
            f"{len(mudos)} con almacen y 0 libros: {', '.join(mudos[:6])}",
            accion="Probablemente no mandan CEGALD. Pedirselo o montar la "
                   "ingesta manual")
    finally:
        conn.close()


def _revisar_stock_sin_almacen() -> dict:
    """
    Lo contrario: proveedores que mandan stock y no tienen donde ponerlo.

    Es el caso de DISMALIBRO, 12.318 libros a diario sin almacen en Odoo.
    El catalogo exige almacen para dejar que alguien gane la puja, asi que
    todo ese stock era invisible. Le paso igual a PENGUIN en septiembre con
    22.193 libros.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT lp.proveedor_email, COUNT(*)
            FROM libros_proveedor lp
            WHERE lp.stock_disponible > 0
              AND NOT EXISTS (
                  SELECT 1 FROM proveedor_almacen_odoo m
                  WHERE m.proveedor_email = lp.proveedor_email)
            GROUP BY lp.proveedor_email
            ORDER BY 2 DESC
        """)
        filas = [(r[0], int(r[1])) for r in cur.fetchall()]
        if not filas:
            return _chequeo("stock_sin_almacen", "Stock sin almacen", OK,
                            "todo el stock que entra tiene donde ir")
        total = sum(n for _e, n in filas)
        txt = ", ".join(f"{e.split('@')[0]} ({n:,})" for e, n in filas[:4])
        return _chequeo(
            "stock_sin_almacen", "Stock sin almacen", ERROR,
            f"{total:,} libros con stock de {len(filas)} proveedores sin "
            f"almacen: {txt}",
            accion="Darles de alta con /api/v1/proveedores/alta y despues "
                   "/proveedores/empujar-ahora")
    finally:
        conn.close()


def _revisar_feeds_topados() -> dict:
    """
    Un feed que lleva muchas vueltas seguidas en su tope no esta al dia:
    esta drenando una cola que no se vacia nunca.

    AbeBooks manda 1.000 por vuelta cada 15 minutos -96.000 al dia- y el
    catalogo genera mas cambios que eso, asi que su oferta va siempre por
    detras. Eso no lo ve ninguna comprobacion de infraestructura: el
    workflow corre, termina bien y da success.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        # Lo que el espejo de cada canal dice tener vivo frente a lo que el
        # catalogo da por vendible. Una diferencia enorme y sostenida es la
        # firma de una cola que no se vacia.
        salida = []
        # AbeBooks falla de otra manera y esta consulta no lo veia. Su
        # espejo SI tiene la fila, con cantidad 0: nosotros creemos haberlo
        # apagado y AbeBooks nunca aplico el delete. Medido por Alexander el
        # 16/10/2026: 35.207 ofertas vivas que respondieron "Book deleted"
        # al reenviarles la orden, prueba de que seguian publicadas.
        #
        # Lo que se mide aqui es el tamano de esa cola pendiente de
        # reconfirmar. El feed la drena a 150 por vuelta; si crece en vez de
        # bajar, el delete esta volviendo a fallar.
        try:
            db.execute_query(cur, """
                SELECT COUNT(*) FROM abe_offer_state
                WHERE last_quantity = 0
                  AND last_pushed_at < NOW() - INTERVAL '2 days'
            """)
            pendientes = int(cur.fetchone()[0])
            if pendientes:
                salida.append(("AbeBooks sin reconfirmar", pendientes))
        except Exception:
            pass

        for canal, tabla in (("AbeBooks", "abe_offer_state"),
                             ("Casa del Libro", "mirakl_offer_state"),
                             ("Fnac", "fnac_offer_state")):
            try:
                db.execute_query(cur, f"""
                    SELECT COUNT(*) FROM {tabla} m
                    WHERE m.last_quantity > 0
                      AND NOT EXISTS (
                          SELECT 1 FROM catalogo_publicable c
                          WHERE c.isbn = m.sku AND c.stock > 0)
                """)
                n = int(cur.fetchone()[0])
                if n:
                    salida.append((canal, n))
            except Exception:
                continue        # la tabla puede no existir todavia
        if not salida:
            return _chequeo("feeds_al_dia", "Ofertas vivas sin stock", OK,
                            "lo publicado coincide con lo vendible")
        total = sum(n for _c, n in salida)
        txt = ", ".join(f"{c} {n:,}" for c, n in salida)
        return _chequeo(
            "feeds_al_dia", "Ofertas vivas sin stock", AVISO,
            f"{total:,} ofertas publicadas sin stock en catalogo: {txt}",
            accion="Es la cola del feed. Si no baja en unas horas, la oferta "
                   "esta encallada")
    finally:
        conn.close()


def _revisar_libros_sin_titulo() -> dict:
    """
    Un libro cuyo titulo es su propio codigo de barras no se puede vender:
    el catalogo lo apaga y el cliente nunca lo ve.

    No lo veia nadie. El worker que los arregla -enrichment.py- estuvo
    parado del 09/06 al 19/10/2026, cuatro meses, sin cron que lo levantara
    ni comprobacion que avisara. Mientras tanto el 88,5% de las fichas
    creadas cada mes nacian asi, y se juntaron 58.140 con stock: 2,98 M EUR
    de catalogo apagado.

    Se miran dos cosas distintas: cuantos hay, y si quien los arregla esta
    vivo. Lo segundo importa mas, porque lo primero se cura solo si lo
    segundo funciona.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT COUNT(*) FROM odoo_books_mirror m
            WHERE (m.name IS NULL OR TRIM(m.name) = ''
                   OR TRIM(m.name) = m.barcode)
              AND EXISTS (SELECT 1 FROM libros_proveedor lp
                          WHERE lp.isbn = m.barcode
                            AND lp.stock_disponible > 0)
        """)
        sin_titulo = int(cur.fetchone()[0])

        # Los que nadie va a arreglar: sin titulo, con stock, ISBN de verdad
        # -un EAN de puzle no lo encuentra ni CdL ni Google Books- y fuera de
        # la cola. Esta es la cifra que avisa de una recaida: si el encolado
        # al crear funciona, se queda en cero sola.
        huerfanos = 0
        pendientes = 0
        try:
            # El mismo criterio EXACTO que usa enrichment.rellenar_cola_
            # vendibles. Si se mide mas ancho que lo que se arregla, el
            # numero no baja a cero nunca y el parte se queda en ambar
            # permanente, que es como se consigue que nadie lo mire. Medido
            # el 19/10/2026: con el criterio ancho salian 30.289, de los que
            # 26.734 ni siquiera estaban en el catalogo -no se pueden
            # vender tengan titulo o no- y solo 3.563 eran accionables.
            db.execute_query(cur, """
                SELECT COUNT(*) FROM catalogo_publicable c
                JOIN odoo_books_mirror m ON m.odoo_id = c.odoo_id
                WHERE (m.name IS NULL OR TRIM(m.name) = ''
                       OR TRIM(m.name) = m.barcode)
                  AND c.proveedor IS NOT NULL
                  AND c.precio_marketplace >= 2.90
                  -- LEFT() y no LIKE: psycopg2 lee el comodin como
                  -- marcador de parametro y revienta con un IndexError que
                  -- el try de abajo se traga entero, dejando el check mudo.
                  AND LEFT(c.isbn, 3) IN ('978', '979')
                  AND NOT EXISTS (SELECT 1 FROM enrichment_queue e
                                  WHERE e.odoo_id = m.odoo_id)
            """)
            huerfanos = int(cur.fetchone()[0])
            db.execute_query(cur, "SELECT COUNT(*) FROM enrichment_queue "
                                  "WHERE status = 'pending'")
            pendientes = int(cur.fetchone()[0])
        except Exception:
            pass
    finally:
        conn.close()

    import enrichment
    corriendo = (enrichment.enrichment_job or {}).get("status") == "running"
    supervisor = enrichment.get_cron_status().get("enabled")

    # El worker caido HABIENDO cola es el fallo de verdad: significa que el
    # agujero se esta llenando y no lo esta vaciando nadie.
    if pendientes > 0 and not corriendo:
        estado = ERROR if not supervisor else AVISO
        return _chequeo(
            "libros_sin_titulo", "Libros sin titulo", estado,
            f"{sin_titulo:,} libros con stock sin titulo utilizable y el "
            f"enriquecedor PARADO con {pendientes:,} en cola"
            + ("" if supervisor else " (y el supervisor tambien apagado)"),
            accion="POST /api/v1/odoo/sync/cron/start")

    if not supervisor:
        return _chequeo(
            "libros_sin_titulo", "Libros sin titulo", AVISO,
            f"el supervisor del enriquecedor esta APAGADO ({sin_titulo:,} "
            f"sin titulo, {pendientes:,} en cola). Sin el, el dia que el "
            f"trabajo se caiga no lo levanta nadie",
            accion="POST /api/v1/odoo/sync/cron/start")

    # Un huerfano es un libro que nacio sin titulo y no fue a la cola: eso
    # solo pasa si el encolado al crear dejo de funcionar.
    # El supervisor rellena cada 10 minutos, asi que un pico pequeno es
    # normal entre dos vueltas. Sostenido y grande significa que el relleno
    # o el encolado al crear han dejado de funcionar.
    if huerfanos > 2000:
        return _chequeo(
            "libros_sin_titulo", "Libros sin titulo", AVISO,
            f"{huerfanos:,} libros VENDIBLES sin titulo y fuera de la cola: "
            f"nadie los va a titular. Revisar el relleno del supervisor "
            f"(enrichment.rellenar_cola_vendibles) y el encolado al crear "
            f"la ficha (enrichment.encolar_sin_titulo)",
            accion="POST /api/v1/odoo/sync/cron/start")

    return _chequeo(
        "libros_sin_titulo", "Libros sin titulo", OK,
        f"{sin_titulo:,} sin titulo ({huerfanos:,} vendibles sin encolar), "
        f"{pendientes:,} en cola, "
        f"enriquecedor {'corriendo' if corriendo else 'en reposo'}")


# ─── Revision completa ───────────────────────────────────────────────
async def revisar(arreglar: bool = True) -> dict:
    """Pasa todas las comprobaciones. Devuelve el parte completo."""
    global _ultima
    t0 = time.monotonic()
    chequeos = []
    chequeos += _revisar_crones(arreglar)
    chequeos.append(_revisar_lock(arreglar))
    chequeos.append(_revisar_bd())
    chequeos.append(await _revisar_odoo())
    chequeos.append(_revisar_shopify())
    chequeos.append(_revisar_proveedores())
    chequeos.append(_revisar_sync())
    # Coherencia del negocio. Sin estas, el parte da verde mientras se
    # vende lo que no hay.
    for fn in (_revisar_ventas_sin_stock,
               _revisar_proveedores_mudos_con_almacen,
               _revisar_stock_sin_almacen,
               _revisar_feeds_topados,
               _revisar_libros_sin_titulo):
        try:
            chequeos.append(fn())
        except Exception as e:
            chequeos.append(_chequeo(fn.__name__, fn.__name__, AVISO,
                                     f"no se pudo comprobar: {type(e).__name__}"))

    errores = [c for c in chequeos if c["estado"] == ERROR]
    avisos = [c for c in chequeos if c["estado"] == AVISO]
    arreglados = [c for c in chequeos if c.get("arreglado")]
    salud = ERROR if errores else (AVISO if avisos else OK)

    _ultima = {
        "status": "completado", "salud": salud, "origen": ORIGEN,
        "en_servidor": EN_SERVIDOR,
        "cuando": datetime.now().isoformat(),
        "chequeos": chequeos,
        "resumen": {"ok": len(chequeos) - len(errores) - len(avisos),
                    "avisos": len(avisos), "errores": len(errores),
                    "arreglados": len(arreglados)},
        "elapsed_s": round(time.monotonic() - t0, 1),
    }

    cambios = _registrar_cambios(chequeos, arreglados)
    _ultima["cambios"] = cambios

    try:
        if not EN_SERVIDOR:
            return _ultima      # una prueba fuera del servidor no ensucia el historial
        import audit_log
        audit_log.log_event(
            "vigilante", f"revision_{salud}",
            f"Vigilante [{ORIGEN}]: {len(errores)} errores, {len(avisos)} avisos, "
            f"{len(arreglados)} arreglados solos",
            detalle={"salud": salud, "cambios": cambios, "origen": ORIGEN,
                     "problemas": [f"{c['titulo']}: {c['detalle']}"
                                   for c in errores + avisos]},
            nivel="error" if errores else "info")
    except Exception:
        pass
    return _ultima


def _registrar_cambios(chequeos: list[dict], arreglados: list[dict]) -> list[str]:
    """
    Guarda los cambios de estado para poder verlos en el panel. Solo se
    apunta lo que CAMBIA: si no, un proveedor mudo llenaria el historial de
    la misma linea cada revision.
    """
    global _estado_previo
    cambios = []
    for c in chequeos:
        previo = _estado_previo.get(c["clave"])
        if previo != c["estado"]:
            if c["estado"] == ERROR:
                cambios.append(f"ERROR — {c['titulo']}: {c['detalle']}")
            elif c["estado"] == AVISO:
                cambios.append(f"AVISO — {c['titulo']}: {c['detalle']}")
            elif previo in (ERROR, AVISO):
                cambios.append(f"RESUELTO — {c['titulo']}")
            _estado_previo[c["clave"]] = c["estado"]
    for c in arreglados:
        cambios.append(f"ARREGLADO — {c['titulo']}: {c['detalle']}")
    return cambios


# ─── Cron ────────────────────────────────────────────────────────────
async def _cron_loop():
    from datetime import timedelta
    print(f"[Vigilante] Arrancado, cada {_cron_state['interval_s']}s")
    while _cron_state["enabled"]:
        try:
            r = await revisar(arreglar=True)
            _cron_state["last_run_at"] = datetime.now().isoformat()
            _cron_state["runs_total"] += 1
            print(f"[Vigilante] revision #{_cron_state['runs_total']}: "
                  f"{r['salud']} ({r['resumen']})")
        except Exception as e:
            _cron_state["errors"].append(f"{type(e).__name__}: {e!r}"[:200])
            print(f"[Vigilante] fallo: {e!r}")
        _cron_state["next_run_at"] = (
            datetime.now() + timedelta(seconds=_cron_state["interval_s"])).isoformat()
        for _ in range(_cron_state["interval_s"]):
            if not _cron_state["enabled"]:
                break
            await asyncio.sleep(1)
    print("[Vigilante] Detenido")


def start_cron() -> bool:
    global _cron_task
    if _cron_task and not _cron_task.done():
        return False
    _cron_state["enabled"] = True
    try:
        _cron_task = asyncio.create_task(_cron_loop())
        return True
    except RuntimeError:
        _cron_state["enabled"] = False
        return False


def stop_cron() -> bool:
    if not _cron_state["enabled"]:
        return False
    _cron_state["enabled"] = False
    return True


if __name__ == "__main__":
    r = asyncio.run(revisar(arreglar=False))
    print(f"SALUD: {r['salud']}  {r['resumen']}\n")
    for c in r["chequeos"]:
        icono = {"ok": "OK ", "aviso": "?? ", "error": "!! "}[c["estado"]]
        print(f"{icono}{c['titulo']:<28}{c['detalle']}")
