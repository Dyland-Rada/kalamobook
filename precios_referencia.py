"""
Un solo PVP de referencia para todo: web y marketplaces.

EL PROBLEMA. El 14/10/2026 Sandra detecta que en los marketplaces se cobra
por debajo del PVP del editor. La causa: once de los quince proveedores
mandan la BASE IMPONIBLE en el campo donde guardamos el precio con IVA, y
como el precio de venta sale de ahi, nace un 4% bajo (un 21% en lo que no
es libro). Medido sobre 589.448 lineas contrastadas contra AZETA:

    AZETA, GRUPO ANAYA, MACHADO      mandan el PVP          98-99,9%
    los otros once                   mandan la base         92-99,6%
    PODIPRINT                        sin contrastar         118.170 lineas

El porcentaje NO es del proveedor: es del libro. Cada proveedor hace
siempre lo mismo -o PVP o base-, y lo que cambia dentro de uno es el tipo
de IVA: 375.842 libros al 4%, 2.581 al 21% (tarot, cuadernos, regalo) y 2
al 10%.

LA SEGUNDA MITAD. El precio de Shopify sale de odoo_books_mirror.list_price,
que lo escribe el scrapeo el dia del alta y no se vuelve a tocar nunca
-auto_scrape solo mira ISBN que no estan en el espejo, y shopify_pub solo
publica los que no estan ya publicados-. Asi que la web no cobra sin IVA,
pero cobra el precio del dia que se dio de alta el libro. No es mejor: es
otro error distinto.

LO QUE HACE ESTE MODULO. Calcular un PVP por ISBN que sea el bueno y que
usen los dos caminos, para que no vuelvan a divergir:

    1. AZETA manda cuando lo tiene. Es la unica fuente con PVP e IVA en
       campos separados: no hay nada que deducir.
    2. Si no, se reconstruye desde el proveedor: precio x (1 + IVA), pero
       solo para los proveedores clasificados como "manda la base".
    3. El IVA sale de AZETA; donde no hay, de la editorial (se aprende de
       AZETA cuales son del 21%); y si tampoco, 4%, que acierta el 99,3%.

NADA SE SOBREESCRIBE. libros_proveedor es de supabase_admin y no admite
ALTER, pero es que ademas no queremos tocarla: lo que manda el proveedor
se guarda tal cual llega. El dia que alguien pregunte por que cambio un
precio hay que poder enseñar el original al lado del corregido.

DOS REGLAS DE SEGURIDAD:

    - Nunca se baja un precio. Si el proveedor manda MAS que la referencia,
      la vieja es la nuestra. Son 1.271 casos en Logista y 1.636 en
      Podiprint, y ahi no se toca nada.
    - Se corrige y se comprueba. Donde el resultado no cae donde deberia,
      se marca como dudoso y se deja fuera en vez de publicarlo.
"""
import os
import time
from datetime import datetime

import db
from informe_stock import get_letra, _podar

CARPETA = os.environ.get("INFORME_STOCK_DIR", "/tmp")

TABLA_MODO = "proveedor_precio_modo"
TABLA_PVP = "pvp_referencia"
TABLA_IVA_ED = "editorial_iva"

# Margen para dar dos importes por iguales. Tres por mil cubre el redondeo
# del CEGALD -que viene en centimos enteros- sin tragarse la diferencia del
# 4%, que es trece veces mas grande.
TOL = 0.003
TOL_MIN = 0.02

# A partir de que proporcion se da por sentado que un proveedor hace
# siempre lo mismo. El mas flojo de los once esta en 92,5%; el mas flojo de
# los tres que mandan PVP, en 98,1%. Con 85% no se cuela ninguno de los dos
# grupos y PODIPRINT -30,7%- se queda fuera, que es lo que se quiere.
UMBRAL_MODO = 0.85
MINIMO_CONTRASTADAS = 200

# Lo mismo para la editorial: con cinco libros conocidos y el 90% en el
# mismo tipo, se da por bueno para los hermanos que no estan en AZETA.
MINIMO_LIBROS_ED = 5
UMBRAL_IVA_ED = 0.90
IVA_POR_DEFECTO = 4.0

_job: dict | None = None


def get_status() -> dict:
    return dict(_job) if _job else {"status": "idle"}


def ensure_schema():
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            CREATE TABLE IF NOT EXISTS {TABLA_MODO} (
                proveedor_email  TEXT PRIMARY KEY,
                modo             TEXT NOT NULL,
                contrastadas     INTEGER NOT NULL DEFAULT 0,
                como_pvp         INTEGER NOT NULL DEFAULT 0,
                como_base        INTEGER NOT NULL DEFAULT 0,
                -- Puesto a mano gana siempre sobre lo medido. Existe para
                -- PODIPRINT y para cualquiera que un dia nos confirme por
                -- escrito lo que manda: una respuesta suya vale mas que
                -- nuestra estadistica.
                fijado_a_mano    BOOLEAN NOT NULL DEFAULT FALSE,
                medido_en        TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        db.execute_query(cur, f"""
            CREATE TABLE IF NOT EXISTS {TABLA_IVA_ED} (
                editorial    TEXT PRIMARY KEY,
                iva          NUMERIC(5,2) NOT NULL,
                libros       INTEGER NOT NULL DEFAULT 0,
                medido_en    TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        db.execute_query(cur, f"""
            CREATE TABLE IF NOT EXISTS {TABLA_PVP} (
                isbn             TEXT PRIMARY KEY,
                pvp              NUMERIC(10,2),
                iva              NUMERIC(5,2),
                origen           TEXT,
                proveedor_email  TEXT,
                -- Lo que manda el proveedor sin tocar, para poder enseñar
                -- siempre de donde sale el corregido.
                precio_recibido  NUMERIC(10,2),
                dudoso           BOOLEAN NOT NULL DEFAULT FALSE,
                motivo           TEXT,
                calculado_en     TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        db.execute_query(cur, f"CREATE INDEX IF NOT EXISTS {TABLA_PVP}_origen_idx "
                              f"ON {TABLA_PVP} (origen)")
        conn.commit()
    finally:
        conn.close()


def _cerca(a: float, b: float) -> bool:
    return abs(a - b) <= max(TOL_MIN, abs(b) * TOL)


# ── 1. Que manda cada proveedor ─────────────────────────────────────────
def clasificar_proveedores(guardar: bool = True) -> dict:
    """
    Contrasta cada proveedor contra AZETA y decide si manda PVP o base.

    No se toca a quien este fijado a mano: una confirmacion por escrito del
    proveedor vale mas que nuestra medicion.
    """
    ensure_schema()
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT lp.proveedor_email,
                   COUNT(*),
                   COUNT(*) FILTER (
                       WHERE ABS(lp.precio_con_iva - f.pvp)
                             <= GREATEST({TOL_MIN}, f.pvp * {TOL})),
                   COUNT(*) FILTER (
                       WHERE ABS(lp.precio_con_iva - f.pvp / (1 + f.iva / 100))
                             <= GREATEST({TOL_MIN},
                                         f.pvp / (1 + f.iva / 100) * {TOL}))
            FROM libros_proveedor lp
            JOIN azeta_feed_descuentos f ON f.isbn = lp.isbn
            WHERE lp.precio_con_iva > 0 AND f.pvp > 0 AND f.iva IS NOT NULL
            GROUP BY lp.proveedor_email
        """)
        filas = cur.fetchall()

        db.execute_query(cur, f"SELECT proveedor_email FROM {TABLA_MODO} "
                              f"WHERE fijado_a_mano")
        a_mano = {r[0] for r in cur.fetchall()}

        out = []
        for email, n, pvp, base in filas:
            n, pvp, base = int(n), int(pvp), int(base)
            if n < MINIMO_CONTRASTADAS:
                modo = "desconocido"
            elif base / n >= UMBRAL_MODO:
                modo = "base"
            elif pvp / n >= UMBRAL_MODO:
                modo = "pvp"
            else:
                modo = "desconocido"
            out.append({"proveedor_email": email, "modo": modo,
                        "contrastadas": n, "como_pvp": pvp, "como_base": base,
                        "pct_pvp": round(pvp * 100 / n, 1) if n else 0.0,
                        "pct_base": round(base * 100 / n, 1) if n else 0.0,
                        "fijado_a_mano": email in a_mano})
            if guardar and email not in a_mano:
                db.execute_query(cur, f"""
                    INSERT INTO {TABLA_MODO}
                        (proveedor_email, modo, contrastadas, como_pvp,
                         como_base, medido_en)
                    VALUES (?, ?, ?, ?, ?, NOW())
                    ON CONFLICT (proveedor_email) DO UPDATE SET
                        modo = EXCLUDED.modo,
                        contrastadas = EXCLUDED.contrastadas,
                        como_pvp = EXCLUDED.como_pvp,
                        como_base = EXCLUDED.como_base,
                        medido_en = NOW()
                """, (email, modo, n, pvp, base))
        if guardar:
            conn.commit()
        out.sort(key=lambda d: -d["contrastadas"])
        return {"status": "ok", "guardado": guardar, "proveedores": out}
    finally:
        conn.close()


def fijar_modo(proveedor_email: str, modo: str) -> dict:
    """Clavar a mano lo que manda un proveedor. Gana sobre lo medido."""
    if modo not in ("pvp", "base", "desconocido"):
        raise ValueError(f"modo invalido: {modo}")
    ensure_schema()
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            INSERT INTO {TABLA_MODO} (proveedor_email, modo, fijado_a_mano,
                                      medido_en)
            VALUES (?, ?, TRUE, NOW())
            ON CONFLICT (proveedor_email) DO UPDATE SET
                modo = EXCLUDED.modo, fijado_a_mano = TRUE, medido_en = NOW()
        """, (proveedor_email, modo))
        conn.commit()
        return {"status": "ok", "proveedor_email": proveedor_email,
                "modo": modo, "fijado_a_mano": True}
    finally:
        conn.close()


# ── 2. Que IVA le toca a cada editorial ─────────────────────────────────
def aprender_iva_editorial(guardar: bool = True) -> dict:
    """
    Saca de AZETA que editoriales van al 21% y cuales al 4%.

    Hace falta para los libros que no estan en AZETA: ahi el unico indicio
    del tipo es de quien es. Las del 21% no son editoriales de libro -LO
    SCARABEO es tarot, MOLESKINE son cuadernos, PLANETA GIFTS es regalo- y
    por eso la señal es tan limpia.
    """
    ensure_schema()
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT UPPER(TRIM(editorial)), iva, COUNT(*)
            FROM azeta_feed_descuentos
            WHERE NULLIF(TRIM(editorial), '') IS NOT NULL
              AND iva IS NOT NULL AND pvp > 0
            GROUP BY UPPER(TRIM(editorial)), iva
        """)
        por_ed: dict[str, dict[float, int]] = {}
        for ed, iva, n in cur.fetchall():
            por_ed.setdefault(ed, {})[float(iva)] = int(n)

        resueltas, repartidas = [], 0
        for ed, tipos in por_ed.items():
            total = sum(tipos.values())
            if total < MINIMO_LIBROS_ED:
                continue
            iva, n = max(tipos.items(), key=lambda kv: kv[1])
            if n / total < UMBRAL_IVA_ED:
                repartidas += 1
                continue
            resueltas.append((ed, iva, total))
            if guardar:
                db.execute_query(cur, f"""
                    INSERT INTO {TABLA_IVA_ED} (editorial, iva, libros, medido_en)
                    VALUES (?, ?, ?, NOW())
                    ON CONFLICT (editorial) DO UPDATE SET
                        iva = EXCLUDED.iva, libros = EXCLUDED.libros,
                        medido_en = NOW()
                """, (ed, iva, total))
        if guardar:
            conn.commit()
        reparto: dict[str, int] = {}
        for _ed, iva, _n in resueltas:
            k = f"{iva:.0f}%"
            reparto[k] = reparto.get(k, 0) + 1
        return {"status": "ok", "guardado": guardar,
                "editoriales": len(por_ed), "resueltas": len(resueltas),
                "sin_mayoria_clara": repartidas, "reparto": reparto}
    finally:
        conn.close()


# ── 3. El PVP de referencia, ISBN a ISBN ────────────────────────────────
CONSULTA_PVP = f"""
    SELECT lp.isbn,
           f.pvp, f.iva,
           ed.iva  AS iva_editorial,
           m.cdl_editorial,
           lp.proveedor_email, lp.precio_con_iva,
           mo.modo
    FROM libros_proveedor lp
    LEFT JOIN azeta_feed_descuentos f  ON f.isbn = lp.isbn
    LEFT JOIN odoo_books_mirror m      ON m.barcode = lp.isbn
    LEFT JOIN {TABLA_IVA_ED} ed        ON ed.editorial = UPPER(TRIM(
                                              COALESCE(NULLIF(f.editorial,''),
                                                       m.cdl_editorial)))
    LEFT JOIN {TABLA_MODO} mo          ON mo.proveedor_email = lp.proveedor_email
    WHERE lp.precio_con_iva > 0
    ORDER BY lp.isbn
"""


def _iva_de(iva_azeta, iva_ed, isbn: str) -> tuple[float, str]:
    if iva_azeta is not None:
        return float(iva_azeta), "azeta"
    if iva_ed is not None:
        return float(iva_ed), "editorial"
    # Un codigo que no empieza por 978/979 no es un libro: de los 2.581 al
    # 21%, el 27,6% son EAN de juego o papeleria, frente al 0,27% entre los
    # del 4%. Aun asi es una pista, no una certeza, y por eso queda marcado.
    if not isbn.startswith(("978", "979")):
        return 21.0, "no_es_isbn"
    return IVA_POR_DEFECTO, "defecto"


def construir(dry_run: bool = True) -> dict:
    """
    Rellena pvp_referencia: un PVP por ISBN, con su IVA y de donde sale.

    En dry_run no escribe nada y devuelve el mismo recuento, que es como
    hay que mirarlo la primera vez.
    """
    global _job
    sello = datetime.now()
    _job = {"status": "running", "dry_run": dry_run, "fase": "preparando",
            "started_at": sello.isoformat(), "isbns": 0, "escritos": 0,
            "origen": {}, "fuente_iva": {}, "dudosos": 0,
            "subidas": 0, "euros_que_suben": 0.0, "errors": [],
            "elapsed_s": 0}
    job = _job
    t0 = time.monotonic()
    conn = None
    try:
        ensure_schema()
        # Las dos mediciones se guardan SIEMPRE, tambien en seco, y no es
        # una licencia: son tablas de consulta que no publica nadie -quien
        # mueve precios es KALAMO_PVP_REFERENCIA-, y si no se guardan, la
        # consulta de abajo no encuentra el mapa de IVA por editorial y
        # todo cae al 4% por defecto. Es decir: la corrida en seco daria un
        # resultado distinto del real, que es justo lo que no puede pasar
        # en lo unico que se mira antes de tocar precios.
        job["fase"] = "clasificando proveedores"
        job["proveedores"] = clasificar_proveedores(guardar=True)["proveedores"]
        job["fase"] = "aprendiendo IVA por editorial"
        job["iva_editorial"] = aprender_iva_editorial(guardar=True)

        # Dos conexiones a proposito: un cursor del lado del servidor se
        # cierra en cuanto se hace commit en SU conexion, asi que leer y
        # escribir por la misma deja la lectura muerta a mitad de vuelta.
        conn = db.get_connection()
        cur = conn.cursor(name="pvpref") if db.IS_POSTGRES else conn.cursor()
        if db.IS_POSTGRES:
            cur.itersize = 5000
        cur.execute(CONSULTA_PVP)
        conn_esc = db.get_connection() if not dry_run else None
        escritor = conn_esc.cursor() if conn_esc else None

        job["fase"] = "calculando"
        actual = None
        acc: list = []
        lote: list = []

        def cierra():
            if actual is None or not acc:
                return
            pvp_az = iva_az = None
            mejor = None          # (pvp_normalizado, email, recibido, dudoso, motivo)
            crudo_max = 0.0
            for (p_az, i_az, i_ed, _ed, email, precio, modo) in acc:
                precio = float(precio)
                crudo_max = max(crudo_max, precio)
                if p_az is not None and float(p_az) > 0 and i_az is not None:
                    pvp_az, iva_az = float(p_az), float(i_az)
                iva, fuente = _iva_de(i_az, i_ed, actual)
                if modo == "base":
                    norm, dud, mot = round(precio * (1 + iva / 100), 2), False, None
                elif modo == "pvp":
                    norm, dud, mot = precio, False, None
                else:
                    # Sin clasificar no se inventa nada: se deja lo que
                    # manda y se marca, que es honesto y ademas conservador
                    # -nunca sube un precio por una suposicion-.
                    norm, dud, mot = precio, True, "proveedor sin clasificar"
                if mejor is None or norm > mejor[0]:
                    mejor = (norm, email, precio, dud, mot, fuente)

            if pvp_az is not None:
                # Regla de no bajar nunca: si alguien manda mas que AZETA,
                # nuestra referencia es la vieja y gana el importe alto.
                if crudo_max > pvp_az + TOL_MIN:
                    fila = (actual, round(crudo_max, 2), iva_az, "proveedor",
                            mejor[1], mejor[2], True,
                            "el proveedor manda mas que AZETA")
                    org, fiva = "proveedor", "azeta"
                else:
                    fila = (actual, round(pvp_az, 2), iva_az, "azeta", None,
                            round(crudo_max, 2), False, None)
                    org, fiva = "azeta", "azeta"
            else:
                norm, email, recibido, dud, mot, fuente = mejor
                fila = (actual, round(norm, 2), None, "proveedor", email,
                        recibido, dud, mot)
                org, fiva = "proveedor", fuente

            job["origen"][org] = job["origen"].get(org, 0) + 1
            job["fuente_iva"][fiva] = job["fuente_iva"].get(fiva, 0) + 1
            if fila[6]:
                job["dudosos"] += 1
            if fila[1] and fila[1] > crudo_max + TOL_MIN:
                job["subidas"] += 1
                job["euros_que_suben"] += float(fila[1]) - crudo_max
            job["isbns"] += 1
            lote.append(fila)

        def vuelca():
            if dry_run or not lote:
                lote.clear()
                return
            from psycopg2.extras import execute_values
            execute_values(escritor, f"""
                INSERT INTO {TABLA_PVP}
                    (isbn, pvp, iva, origen, proveedor_email, precio_recibido,
                     dudoso, motivo, calculado_en)
                VALUES %s
                ON CONFLICT (isbn) DO UPDATE SET
                    pvp = EXCLUDED.pvp, iva = EXCLUDED.iva,
                    origen = EXCLUDED.origen,
                    proveedor_email = EXCLUDED.proveedor_email,
                    precio_recibido = EXCLUDED.precio_recibido,
                    dudoso = EXCLUDED.dudoso, motivo = EXCLUDED.motivo,
                    calculado_en = NOW()
            """, lote, template="(%s,%s,%s,%s,%s,%s,%s,%s,NOW())",
                page_size=2000)
            job["escritos"] += len(lote)
            conn_esc.commit()
            lote.clear()

        while True:
            bloque = cur.fetchmany(5000)
            if not bloque:
                break
            for fila in bloque:
                isbn = fila[0]
                if isbn != actual:
                    cierra()
                    actual, acc = isbn, []
                acc.append(fila[1:])
            if len(lote) >= 20000:
                vuelca()
        cierra()
        vuelca()

        job["euros_que_suben"] = round(job["euros_que_suben"], 2)
        job["fase"] = "done"
        job["status"] = "completed"
        print(f"[PvpRef] {job['isbns']:,} ISBN, {job['escritos']:,} escritos, "
              f"{job['dudosos']:,} dudosos")
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e}"[:300])
        print(f"[PvpRef] FAIL: {e!r}")
    finally:
        for c in (conn, locals().get("conn_esc")):
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
        job["elapsed_s"] = round(time.monotonic() - t0, 2)
    return job


# ── 4. Que cambiaria de precio si se aplica ─────────────────────────────
def impacto(limite: int = 40) -> dict:
    """
    Compara el precio que se publica hoy con el que saldria de la
    referencia. Es lo que hay que mirar ANTES de enchufar nada.
    """
    import pricing_engine
    ensure_schema()
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT c.isbn, c.titulo, c.precio_marketplace, r.pvp, r.origen,
                   r.precio_recibido, r.dudoso
            FROM catalogo_publicable c
            JOIN {TABLA_PVP} r ON r.isbn = c.isbn
            WHERE c.stock > 0 AND r.pvp > 0 AND NOT r.dudoso
        """)
        suben = igual = bajan = 0
        euros = 0.0
        muestra = []
        for isbn, tit, hoy, pvp, origen, recibido, _dud in cur.fetchall():
            nuevo = pricing_engine.precio_marketplace(
                pricing_engine.web_price(float(pvp)))
            if nuevo is None or hoy is None:
                continue
            hoy = float(hoy)
            d = round(nuevo - hoy, 2)
            if d > 0.01:
                suben += 1
                euros += d
                if len(muestra) < limite:
                    muestra.append({
                        "isbn": isbn, "titulo": (tit or "")[:60],
                        "cobramos_hoy": hoy, "pasaria_a": nuevo,
                        "diferencia": d, "pvp_referencia": float(pvp),
                        "origen": origen,
                        "precio_del_proveedor": (float(recibido)
                                                 if recibido is not None else None)})
            elif d < -0.01:
                bajan += 1
            else:
                igual += 1
        return {"status": "ok", "suben": suben, "sin_cambio": igual,
                "bajan": bajan, "euros_que_suben": round(euros, 2),
                "muestra": muestra}
    finally:
        conn.close()


# ── 5. El Excel de lo que cambiaria en Odoo ─────────────────────────────
COLS_ODOO = [
    ("ISBN", 15, "El codigo del libro."),
    ("TITULO", 48, "Como esta en Odoo."),
    ("PRECIO EN ODOO HOY", 14, "El list_price que tiene ahora mismo."),
    ("PASARIA A", 12, "El precio que saldria del PVP de referencia."),
    ("DIFERENCIA", 12, "La columna anterior menos la de hoy."),
    ("DIF %", 9, "Esa diferencia en porcentaje."),
    ("PVP DE REFERENCIA", 14,
     "El PVP corregido. De aqui sale el precio: se le suma el suplemento de "
     "los libros baratos (2,00 por debajo de 4,99; 1,50 hasta 6,00; 1,00 "
     "hasta 7,50) y nada por encima de 7,50."),
    ("DE DONDE SALE", 13,
     "«azeta» = su catalogo nos da el PVP y el IVA en campos separados. "
     "«proveedor» = no esta en AZETA y el PVP se reconstruye multiplicando "
     "lo que manda el proveedor por su IVA."),
    ("PROVEEDOR", 22,
     "Quien manda ese precio, cuando la referencia se reconstruyo."),
    ("PRECIO QUE MANDA", 13,
     "El importe tal cual llega en su fichero, sin tocar. Es la prueba de "
     "que venia sin IVA."),
    ("IVA %", 8, "El tipo que se le aplico para reconstruir el PVP."),
    ("QUE PASA", 30,
     "«SUBE» se aplica. «BAJA - no se toca» queda fuera: una bajada "
     "significa que la referencia esta por debajo de lo publicado y eso se "
     "mira a mano. «SE ARCHIVARIA - no se toca» tambien queda fuera: el PVP "
     "no llega a 2,90 y archivar saca el libro de la tienda."),
]

SQL_CAMBIOS = f"""
    SELECT m.barcode, COALESCE(NULLIF(m.name, ''), ''), m.list_price,
           r.pvp, r.origen, r.proveedor_email, r.precio_recibido, r.iva
    FROM odoo_books_mirror m
    JOIN {TABLA_PVP} r ON r.isbn = m.barcode
    WHERE m.odoo_id IS NOT NULL AND r.pvp > 0 AND NOT r.dudoso
"""


def cambios_odoo(ruta: str | None = None) -> dict:
    """
    Una fila por producto que cambiaria de precio en Odoo, con los que se
    quedan fuera incluidos y dicho por que.

    Los protegidos van dentro a proposito: son los unicos que hay que mirar
    a mano, y un informe que solo enseñara lo que sale bien no serviria
    para decidir nada.
    """
    global _job
    import pricing_engine
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.cell import WriteOnlyCell

    sello = datetime.now()
    ruta = ruta or os.path.join(CARPETA,
                                f"precios_odoo_{sello:%Y%m%d_%H%M}.xlsx")
    _job = {"status": "running", "accion": "cambios_odoo", "ruta": ruta,
            "started_at": sello.isoformat(), "filas": 0, "suben": 0,
            "bajan": 0, "archivarian": 0, "euros_que_suben": 0.0,
            "errors": [], "elapsed_s": 0}
    job = _job
    t0 = time.monotonic()
    conn = None
    try:
        conn = db.get_connection()
        cur = conn.cursor(name="camodoo") if db.IS_POSTGRES else conn.cursor()
        if db.IS_POSTGRES:
            cur.itersize = 5000
        cur.execute(SQL_CAMBIOS)

        wb = Workbook(write_only=True)
        guia = wb.create_sheet("Como leerlo")
        hoja = wb.create_sheet("Cambios")
        for i, (_n, ancho, _a) in enumerate(COLS_ODOO):
            hoja.column_dimensions[get_letra(i + 1)].width = ancho
        hoja.freeze_panes = "C2"
        fondo = PatternFill("solid", fgColor="7A1F2B")
        cab = []
        for i, (t, _a, _d) in enumerate(COLS_ODOO):
            c = WriteOnlyCell(hoja, value=t)
            c.fill = fondo
            c.font = Font(bold=True, color="FFFFFF", size=10)
            c.border = Border(bottom=Side(style="thin", color="FFFFFF"))
            c.alignment = Alignment(horizontal="left" if i < 2 else "center",
                                    vertical="center", wrap_text=i >= 2)
            cab.append(c)
        hoja.row_dimensions[1].height = 40
        hoja.append(cab)

        while True:
            lote = cur.fetchmany(5000)
            if not lote:
                break
            for isbn, tit, lp, pvp, origen, prov, recib, iva in lote:
                nuevo = pricing_engine.web_price(float(pvp))
                actual = round(float(lp), 2) if lp is not None else None
                if nuevo is None:
                    que, dif = "SE ARCHIVARIA - no se toca", None
                    job["archivarian"] += 1
                elif actual is None or nuevo > actual:
                    que = "SUBE"
                    dif = round(nuevo - (actual or 0), 2)
                    job["suben"] += 1
                    job["euros_que_suben"] += dif
                elif nuevo < actual:
                    que, dif = "BAJA - no se toca", round(nuevo - actual, 2)
                    job["bajan"] += 1
                else:
                    continue
                job["filas"] += 1
                hoja.append([
                    isbn, (tit or "")[:110], actual, nuevo, dif,
                    round(dif * 100 / actual, 1) if (dif and actual) else None,
                    float(pvp), origen, prov,
                    float(recib) if recib is not None else None,
                    float(iva) if iva is not None else None,
                    que,
                ])

        _guia_odoo(guia, job)
        wb.save(ruta)
        job["bytes"] = os.path.getsize(ruta)
        job["euros_que_suben"] = round(job["euros_que_suben"], 2)
        job["borrados"] = _podar(ruta)
        job["status"] = "completed"
        print(f"[PvpRef] cambios Odoo: {job['filas']:,} filas")
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e}"[:300])
        print(f"[PvpRef] cambios_odoo FAIL: {e!r}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        job["elapsed_s"] = round(time.monotonic() - t0, 2)
    return job


def _guia_odoo(hoja, job):
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.cell import WriteOnlyCell
    hoja.column_dimensions["A"].width = 30
    hoja.column_dimensions["B"].width = 96

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

    f("PRECIOS QUE CAMBIARIAN EN ODOO", None, True)
    f("Generado", datetime.now().strftime("%d/%m/%Y %H:%M"))
    f("")
    f("QUE PASO", None, True, "FFF4E0")
    f("", "Once de los quince proveedores mandan la BASE IMPONIBLE en el "
         "campo donde guardamos el precio con IVA. Un libro de 23,50 llega "
         "como 22,60. Cruzando con el catalogo de AZETA -la unica fuente que "
         "da el PVP y el tipo de IVA en campos separados- se sabe el tipo de "
         "cada libro y se reconstruye el PVP de verdad.")
    f("")
    f("QUE SE HACE CON ESTE FICHERO", None, True, "F6F3F0")
    f("", "Solo se aplican las filas que ponen SUBE. Las otras dos estan "
         "aqui para mirarlas a mano; no se tocan.")
    f("")
    f("RECUENTO", None, True, "F6F3F0")
    f("Suben", f"{job['suben']:,} productos · {job['euros_que_suben']:,.2f} "
               f"EUR de subida acumulada")
    f("Bajan (no se tocan)", f"{job['bajan']:,}")
    f("Se archivarian (no se tocan)", f"{job['archivarian']:,}")
    f("")
    f("LAS COLUMNAS", None, True, "F6F3F0")
    for i, (t, _a, desc) in enumerate(COLS_ODOO):
        f(f"{get_letra(i+1)} · {t}", desc)
    f("")
    f("UN AVISO", None, True, "FFF4E0")
    f("", "La suma en euros NO es dinero perdido ni dinero que se vaya a "
         "ganar: es lo que cada libro esta mal valorado. Lo que se recupera "
         "depende de cuantos se vendan.")
