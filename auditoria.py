"""
Auditoria de datos: comprueba que el stock que decimos tener es el que hay.

El vigilante mira si el sistema esta VIVO (crones, ficheros, servicios). Esto
mira si el sistema esta en lo CIERTO, que es otra pregunta: los crones pueden
ir perfectos y el stock estar mal desde hace meses. Fue el caso del proveedor
en Odoo, que llevaba equivocado desde mayo sin que nada fallara.

Compara tres fuentes que deberian decir lo mismo:

    fichero del proveedor  ->  libros_proveedor  ->  Odoo (stock por almacen)

Cada proveedor tiene su almacen en Odoo (AZETA -> AZE01, Anaya -> GRU01...),
asi que la comparacion es exacta: unidad a unidad, libro a libro.

Los dos fallos que importan, y no son simetricos:

  - SOBRA EN ODOO: hay stock que ningun proveedor tiene. Se vende un libro
    que nadie puede servir. Este es el caro: el cliente paga y no hay libro.
  - FALTA EN ODOO: el proveedor lo tiene y Odoo esta a cero. Se deja de
    vender algo que si hay. Molesta, pero no rompe nada.

Se mide en dos planos, y confundirlos lleva a conclusiones falsas:

  POR ALMACEN  mide la fidelidad de cada proveedor. Que a AZETA le falte un
               libro en AZE01 no significa que no se pueda vender: puede
               estar en Icaro. Sirve para saber que espejo va mal.
  POR LIBRO    suma todos los almacenes. ESTE es el dato comercial: si un
               libro esta a cero en los catorce sitios y algun proveedor lo
               tiene, ahi si se esta perdiendo una venta.

No arregla nada por su cuenta. Mide, ordena por gravedad y lo deja escrito.
Reparar stock es una decision con consecuencias comerciales.
"""
import asyncio
import os
import time
from datetime import datetime

import db
from odoo_client import OdooClient

PAGINA_QUANT = int(os.environ.get("AUDIT_PAGINA_QUANT", "40000"))
# Por debajo de esto una diferencia de unidades no se cuenta como fallo: los
# ficheros llegan a horas distintas y un libro se mueve entre medias.
TOLERANCIA_UNIDADES = int(os.environ.get("AUDIT_TOLERANCIA", "0"))
MUESTRA = 25

_job: dict | None = None


def get_status() -> dict:
    return dict(_job) if _job else {"status": "idle"}


def stop() -> bool:
    if _job and _job.get("status") == "running":
        _job["status"] = "stopped"
        return True
    return False


# --------------------------------------------------------------------------
# Lectura de Odoo
# --------------------------------------------------------------------------

async def _almacen_por_ubicacion(odoo: OdooClient) -> dict[int, str]:
    """{location_id: codigo de almacen}. Incluye las hijas de cada almacen."""
    whs = await odoo.search_read("stock.warehouse", [], ["id", "code"])
    codigo = {w["id"]: w["code"] for w in whs}
    locs = await odoo.search_read(
        "stock.location", [["usage", "=", "internal"]], ["id", "warehouse_id"])
    salida = {}
    for l in locs:
        w = l.get("warehouse_id")
        wid = w[0] if isinstance(w, list) else w
        if wid and wid in codigo:
            salida[l["id"]] = codigo[wid]
    return salida


async def _stock_odoo(odoo: OdooClient, ubic: dict[int, str], job: dict) -> dict:
    """{(template_id, codigo_almacen): unidades} leido por paginas."""
    salida: dict[tuple[int, str], float] = {}
    offset = 0
    while True:
        if job["status"] != "running":
            break
        pagina = await odoo.search_read(
            "stock.quant", [["location_id.usage", "=", "internal"]],
            ["product_id", "location_id", "quantity", "product_tmpl_id"],
            offset=offset, limit=PAGINA_QUANT, order="id")
        for q in pagina:
            loc = q.get("location_id")
            loc = loc[0] if isinstance(loc, list) else loc
            code = ubic.get(loc)
            if not code:
                continue
            t = q.get("product_tmpl_id")
            t = t[0] if isinstance(t, list) else t
            if not t:
                continue
            clave = (t, code)
            salida[clave] = salida.get(clave, 0.0) + (q.get("quantity") or 0.0)
        if len(pagina) < PAGINA_QUANT:
            break
        offset += PAGINA_QUANT
        job["stage"] = f"leyendo stock de Odoo ({len(salida):,})"
        print(f"[Auditoria] quants leidos: {len(salida):,}", flush=True)
    return salida


# --------------------------------------------------------------------------
# Lectura de nuestra base de datos
# --------------------------------------------------------------------------

def _stock_bd() -> tuple[dict, dict[str, str], dict[int, str], set[str]]:
    """
    Devuelve:
      {(odoo_id, codigo_almacen): unidades}  lo que dicen los proveedores
      {email: codigo_almacen}
      {odoo_id: barcode}
      proveedores pausados
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, "SELECT proveedor_email, warehouse_code "
                              "FROM proveedor_almacen_odoo")
        almacen = {e: w for e, w in cur.fetchall()}

        pausados: set[str] = set()
        try:
            db.execute_query(cur, "SELECT proveedor_email FROM proveedor_pausa "
                                  "WHERE activo = false")
            pausados = {r[0] for r in cur.fetchall()}
        except Exception:
            conn.rollback()

        db.execute_query(cur, """
            SELECT m.odoo_id, m.barcode, lp.proveedor_email,
                   COALESCE(lp.stock_disponible, 0)
            FROM libros_proveedor lp
            JOIN odoo_books_mirror m ON m.barcode = lp.isbn
            WHERE m.odoo_id IS NOT NULL
        """)
        esperado: dict[tuple[int, str], float] = {}
        barcode: dict[int, str] = {}
        for odoo_id, bc, email, stock in cur.fetchall():
            code = almacen.get(email)
            if not code:
                continue
            oid = int(odoo_id)
            barcode[oid] = bc
            clave = (oid, code)
            esperado[clave] = esperado.get(clave, 0.0) + float(stock or 0)
        return esperado, almacen, barcode, pausados
    finally:
        conn.close()


def _integridad_catalogo() -> dict:
    """Fallos de catalogo que se ven en el espejo sin llamar a Odoo."""
    conn = db.get_connection()
    cur = conn.cursor()
    out = {}
    try:
        db.execute_query(cur, "SELECT count(*) FROM odoo_books_mirror "
                              "WHERE barcode IS NULL OR btrim(barcode) = ''")
        out["sin_barcode"] = cur.fetchone()[0]

        db.execute_query(cur, """
            SELECT count(*), COALESCE(sum(n) - count(*), 0) FROM (
                SELECT barcode, count(*) n FROM odoo_books_mirror
                WHERE barcode IS NOT NULL AND btrim(barcode) <> ''
                GROUP BY barcode HAVING count(*) > 1) t
        """)
        dup, sobrantes = cur.fetchone()
        out["barcodes_duplicados"] = dup
        out["productos_duplicados_de_mas"] = int(sobrantes)

        db.execute_query(cur, """
            SELECT barcode, count(*) n FROM odoo_books_mirror
            WHERE barcode IS NOT NULL AND btrim(barcode) <> ''
            GROUP BY barcode HAVING count(*) > 1
            ORDER BY n DESC LIMIT %s
        """, (MUESTRA,))
        out["muestra_duplicados"] = [{"barcode": b, "veces": n}
                                     for b, n in cur.fetchall()]

        db.execute_query(cur, """
            SELECT count(*) FROM odoo_books_mirror
            WHERE barcode IS NOT NULL AND barcode !~ '^[0-9]{13}$'
        """)
        out["barcode_no_ean13"] = cur.fetchone()[0]

        db.execute_query(cur, "SELECT count(*), max(synced_at) FROM odoo_books_mirror")
        n, ult = cur.fetchone()
        out["productos_en_espejo"] = n
        out["espejo_actualizado"] = ult.isoformat() if ult else None
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------------

async def auditar() -> dict:
    global _job
    _job = {
        "status": "running", "started_at": datetime.now().isoformat(),
        "stage": "empezando", "elapsed_s": 0, "errors": [],
        "espejo": {}, "catalogo": {}, "proveedores": {}, "totales": {},
        "por_libro": {}, "huerfanos": {}, "hallazgos": [],
    }
    job = _job
    t0 = time.monotonic()
    try:
        job["stage"] = "comprobando el espejo"
        catalogo = _integridad_catalogo()
        job["catalogo"] = catalogo

        async with OdooClient() as odoo:
            en_odoo = await odoo.search_count("product.template", [])
            # El espejo solo guarda libros. Los productos sin ISBN de Odoo no
            # estan ahi y no son un fallo: hay que descontarlos antes de
            # decir que el espejo va atrasado.
            sin_isbn = await odoo.search_count(
                "product.template", [["barcode", "in", [False, ""]]])
            job["espejo"] = {
                "productos_en_odoo": en_odoo,
                "productos_sin_isbn_en_odoo": sin_isbn,
                "libros_en_odoo": en_odoo - sin_isbn,
                "productos_en_espejo": catalogo.get("productos_en_espejo"),
                "desfase": (en_odoo - sin_isbn) - (catalogo.get("productos_en_espejo") or 0),
                "actualizado": catalogo.get("espejo_actualizado"),
            }
            job["catalogo"]["no_almacenables"] = await odoo.search_count(
                "product.template", [["is_storable", "=", False]])
            job["catalogo"]["variantes_archivadas"] = await odoo.search_count(
                "product.product", [["active", "=", False]])

            job["stage"] = "leyendo stock de Odoo"
            ubic = await _almacen_por_ubicacion(odoo)
            real = await _stock_odoo(odoo, ubic, job)

        job["stage"] = "leyendo lo que dicen los proveedores"
        esperado, almacen, barcode, pausados = _stock_bd()
        code_a_email = {v: k for k, v in almacen.items()}

        job["stage"] = "comparando"
        # Almacenes que no son de ningun proveedor (el WH generico de la
        # empresa). Su stock no lo alimenta ningun fichero, asi que medirlo
        # contra los proveedores da un 0% que no significa nada.
        propios = set(almacen.values())
        ajenos: dict[str, dict] = {}
        for (oid, code), r in real.items():
            if code not in propios and r > 0:
                a = ajenos.setdefault(code, {"libros": 0, "unidades": 0.0})
                a["libros"] += 1
                a["unidades"] += r
        for a in ajenos.values():
            a["unidades"] = round(a["unidades"])
        job["almacenes_sin_proveedor"] = ajenos

        claves = {k for k in set(real) | set(esperado) if k[1] in propios}
        por_prov: dict[str, dict] = {}
        muestras: dict[str, dict[str, list]] = {}
        for clave in claves:
            oid, code = clave
            r = real.get(clave, 0.0)
            e = esperado.get(clave, 0.0)
            d = por_prov.setdefault(code, {
                "almacen": code, "email": code_a_email.get(code),
                "pausado": code_a_email.get(code) in pausados,
                "libros_odoo": 0, "libros_proveedor": 0,
                "unidades_odoo": 0.0, "unidades_proveedor": 0.0,
                "coinciden": 0, "sobra_en_odoo": 0, "falta_en_odoo": 0,
                "cantidad_distinta": 0,
                "unidades_fantasma": 0.0, "unidades_perdidas": 0.0,
            })
            m = muestras.setdefault(code, {"sobra": [], "falta": [], "distinta": []})
            if r > 0:
                d["libros_odoo"] += 1
                d["unidades_odoo"] += r
            if e > 0:
                d["libros_proveedor"] += 1
                d["unidades_proveedor"] += e

            if r > 0 and e <= 0:
                d["sobra_en_odoo"] += 1
                d["unidades_fantasma"] += r
                if len(m["sobra"]) < MUESTRA:
                    m["sobra"].append({"isbn": barcode.get(oid), "odoo_id": oid,
                                       "odoo": r})
            elif e > 0 and r <= 0:
                d["falta_en_odoo"] += 1
                d["unidades_perdidas"] += e
                if len(m["falta"]) < MUESTRA:
                    m["falta"].append({"isbn": barcode.get(oid), "odoo_id": oid,
                                       "proveedor": e})
            elif r > 0 and e > 0:
                if abs(r - e) > TOLERANCIA_UNIDADES:
                    d["cantidad_distinta"] += 1
                    if len(m["distinta"]) < MUESTRA:
                        m["distinta"].append({"isbn": barcode.get(oid),
                                              "odoo": r, "proveedor": e})
                else:
                    d["coinciden"] += 1

        for code, d in por_prov.items():
            base = d["libros_odoo"] or d["libros_proveedor"] or 1
            d["fiabilidad_pct"] = round(100.0 * d["coinciden"] / base, 1)
            d["muestras"] = muestras.get(code, {})
            for k in ("unidades_odoo", "unidades_proveedor",
                      "unidades_fantasma", "unidades_perdidas"):
                d[k] = round(d[k])
        job["proveedores"] = dict(sorted(por_prov.items(),
                                         key=lambda x: -x[1]["sobra_en_odoo"]))

        tot = {k: sum(d[k] for d in por_prov.values()) for k in
               ("libros_odoo", "libros_proveedor", "coinciden", "sobra_en_odoo",
                "falta_en_odoo", "cantidad_distinta", "unidades_odoo",
                "unidades_proveedor", "unidades_fantasma", "unidades_perdidas")}
        base = tot["libros_odoo"] or 1
        tot["fiabilidad_pct"] = round(100.0 * tot["coinciden"] / base, 1)
        job["totales"] = tot

        # ── Por libro: sumando los catorce almacenes ────────────────────
        # Un libro que a AZETA le falta pero Icaro tiene SI se vende. Lo que
        # cuesta dinero es el que esta a cero en todas partes.
        real_libro: dict[int, float] = {}
        esp_libro: dict[int, float] = {}
        for (oid, code), v in real.items():
            if code in propios:
                real_libro[oid] = real_libro.get(oid, 0.0) + v
        for (oid, code), v in esperado.items():
            esp_libro[oid] = esp_libro.get(oid, 0.0) + v

        pl = {"vendibles": 0, "no_vendibles": 0, "unidades_no_vendibles": 0.0,
              "fantasma": 0, "unidades_fantasma": 0.0, "muestra_no_vendibles": []}
        for oid in set(real_libro) | set(esp_libro):
            r = real_libro.get(oid, 0.0)
            e = esp_libro.get(oid, 0.0)
            if e > 0 and r > 0:
                pl["vendibles"] += 1
            elif e > 0 and r <= 0:
                pl["no_vendibles"] += 1
                pl["unidades_no_vendibles"] += e
                if len(pl["muestra_no_vendibles"]) < MUESTRA:
                    pl["muestra_no_vendibles"].append(
                        {"isbn": barcode.get(oid), "odoo_id": oid, "proveedor": e})
            elif r > 0 and e <= 0:
                pl["fantasma"] += 1
                pl["unidades_fantasma"] += r
        base = pl["vendibles"] + pl["no_vendibles"] or 1
        pl["cobertura_pct"] = round(100.0 * pl["vendibles"] / base, 1)
        pl["unidades_no_vendibles"] = round(pl["unidades_no_vendibles"])
        pl["unidades_fantasma"] = round(pl["unidades_fantasma"])
        job["por_libro"] = pl

        # Stock en Odoo cuyo libro no lo manda NINGUN proveedor: ni en su
        # almacen ni en ningun otro. Es el legado de la carga de mayo.
        conocidos = {oid for oid, _ in esperado}
        huer = {}
        for (oid, code), r in real.items():
            if r > 0 and oid not in conocidos:
                h = huer.setdefault(code, {"libros": 0, "unidades": 0.0})
                h["libros"] += 1
                h["unidades"] += r
        for h in huer.values():
            h["unidades"] = round(h["unidades"])
        job["huerfanos"] = {
            "por_almacen": dict(sorted(huer.items(), key=lambda x: -x[1]["libros"])),
            "libros": sum(h["libros"] for h in huer.values()),
            "unidades": sum(h["unidades"] for h in huer.values()),
        }

        job["hallazgos"] = _hallazgos(job)
        if job["status"] == "running":
            job["status"] = "completed"
        job["stage"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e}"[:300])
        print(f"[Auditoria] FAIL: {e!r}")
    finally:
        job["elapsed_s"] = round(time.monotonic() - t0, 1)
        try:
            import audit_log
            t = job.get("totales", {})
            audit_log.log_event(
                "auditoria", "revision",
                f"Auditoria de stock: {t.get('fiabilidad_pct', 0)}% fiable, "
                f"{t.get('sobra_en_odoo', 0):,} libros con stock fantasma, "
                f"{t.get('falta_en_odoo', 0):,} sin subir ({job['elapsed_s']}s)",
                detalle={"totales": t, "catalogo": job.get("catalogo"),
                         "huerfanos": job.get("huerfanos"),
                         "hallazgos": job.get("hallazgos")},
                nivel="error" if job["status"] == "error" else "info")
        except Exception:
            pass
    return job


def _hallazgos(job: dict) -> list[dict]:
    """Lo que hay que mirar, ordenado por lo que cuesta dinero."""
    out = []
    t = job.get("totales", {})
    c = job.get("catalogo", {})
    hu = job.get("huerfanos", {})
    pl = job.get("por_libro", {})

    # Lo primero, lo que se nota en la caja: libros que no se pueden vender
    # aunque alguien los tenga, y libros que se venden sin que nadie los tenga.
    if pl.get("fantasma"):
        out.append({
            "nivel": "error",
            "titulo": f"{pl['fantasma']:,} libros a la venta que ningun "
                      f"proveedor tiene",
            "detalle": f"{pl['unidades_fantasma']:,} unidades. Se pueden "
                       f"comprar y no hay quien los sirva: pedido que no "
                       f"se puede cumplir.",
            "accion": "Poner a cero los confirmados",
        })
    if pl.get("no_vendibles"):
        out.append({
            "nivel": "aviso",
            "titulo": f"{pl['no_vendibles']:,} libros que algun proveedor "
                      f"tiene y en Odoo estan a cero en TODOS los almacenes",
            "detalle": f"{pl['unidades_no_vendibles']:,} unidades que no se "
                       f"pueden vender aunque existan.",
            "accion": "Forzar resync del proveedor que los tiene",
        })
    if t.get("sobra_en_odoo"):
        out.append({
            "nivel": "aviso",
            "titulo": f"{t['sobra_en_odoo']:,} libros con stock en un almacen "
                      f"cuyo proveedor ya no los tiene",
            "detalle": f"{t['unidades_fantasma']:,} unidades. Si otro almacen "
                       f"lo tiene el libro se sirve igual, pero el reparto "
                       f"entre proveedores esta mal.",
            "accion": "Revisar por almacen; poner a cero los confirmados",
        })
    if hu.get("libros"):
        out.append({
            "nivel": "error",
            "titulo": f"{hu['libros']:,} libros con stock que no aparecen en "
                      f"ningun fichero de proveedor",
            "detalle": f"{hu['unidades']:,} unidades, herencia de la carga de "
                       f"mayo. Nadie las actualiza nunca.",
            "accion": "Decidir si se ponen a cero o se dan de baja",
        })
    if t.get("falta_en_odoo"):
        out.append({
            "nivel": "info",
            "titulo": f"{t['falta_en_odoo']:,} huecos por almacen: el proveedor "
                      f"lo tiene y su almacen esta a cero",
            "detalle": f"{t['unidades_perdidas']:,} unidades. La mayoria SI se "
                       f"venden porque otro proveedor las cubre; lo que se "
                       f"pierde es saber quien lo sirve mas barato.",
            "accion": "Mirar la columna «Sin subir» para ver que espejo va mal",
        })
    if t.get("cantidad_distinta"):
        out.append({
            "nivel": "aviso",
            "titulo": f"{t['cantidad_distinta']:,} libros con cantidad distinta "
                      f"entre proveedor y Odoo",
            "detalle": "Normal si el fichero llego despues del ultimo sync.",
            "accion": "Preocupa solo si no baja tras el siguiente ciclo",
        })
    if c.get("productos_duplicados_de_mas"):
        out.append({
            "nivel": "error",
            "titulo": f"{c['barcodes_duplicados']:,} ISBN repetidos en Odoo",
            "detalle": f"{c['productos_duplicados_de_mas']:,} productos de mas. "
                       f"El stock se reparte entre copias y ninguna cuadra.",
            "accion": "Fusionar o archivar las copias",
        })
    if c.get("sin_barcode"):
        out.append({
            "nivel": "aviso",
            "titulo": f"{c['sin_barcode']:,} productos en Odoo sin ISBN",
            "detalle": "Sin ISBN no se les puede asignar stock ni proveedor.",
            "accion": "Revisar a mano: suelen ser articulos que no son libros",
        })
    if c.get("no_almacenables"):
        out.append({
            "nivel": "aviso",
            "titulo": f"{c['no_almacenables']:,} productos que no admiten stock",
            "detalle": "is_storable a false: aunque llegue stock, no se guarda.",
            "accion": "Reparar catalogo desde Mantenimiento",
        })
    if abs(job.get("espejo", {}).get("desfase") or 0) > 500:
        out.append({
            "nivel": "aviso",
            "titulo": f"El espejo local va {abs(job['espejo']['desfase']):,} "
                      f"libros por detras de Odoo",
            "detalle": f"Ultima actualizacion: {job['espejo'].get('actualizado')}",
            "accion": "Lanzar la sincronizacion de catalogo",
        })
    for code, d in job.get("proveedores", {}).items():
        if d.get("pausado") and d.get("libros_odoo"):
            out.append({
                "nivel": "error",
                "titulo": f"{code} esta pausado pero conserva stock en Odoo",
                "detalle": f"{d['libros_odoo']:,} libros, {d['unidades_odoo']:,} "
                           f"unidades. Al pausar deberia haber quedado a cero.",
                "accion": "Volver a pausarlo para que baje el stock",
            })
    if not out:
        out.append({"nivel": "ok", "titulo": "Sin incidencias",
                    "detalle": "Odoo y los ficheros de proveedor coinciden.",
                    "accion": ""})
    return out


if __name__ == "__main__":
    r = asyncio.run(auditar())
    t = r["totales"]
    print(f"\n{'='*66}\nAUDITORIA  ({r['elapsed_s']}s)  estado={r['status']}\n{'='*66}")
    pl = r.get("por_libro", {})
    print(f"\n--- POR LIBRO (lo comercial: sumando los 14 almacenes) ---")
    print(f"   se pueden vender      {pl.get('vendibles',0):>10,}  "
          f"({pl.get('cobertura_pct')}% de los que algun proveedor tiene)")
    print(f"   NO se pueden vender   {pl.get('no_vendibles',0):>10,}  "
          f"({pl.get('unidades_no_vendibles',0):,} uds paradas)")
    print(f"   fantasma (nadie tiene){pl.get('fantasma',0):>10,}  "
          f"({pl.get('unidades_fantasma',0):,} uds)")

    print(f"\n--- POR ALMACEN (fidelidad de cada espejo de proveedor) ---")
    print(f"FIABILIDAD GLOBAL DEL STOCK: {t.get('fiabilidad_pct')}%")
    print(f"   coinciden          {t.get('coinciden',0):>10,}")
    print(f"   sobra en Odoo      {t.get('sobra_en_odoo',0):>10,}  "
          f"({t.get('unidades_fantasma',0):,} uds fantasma)")
    print(f"   falta en Odoo      {t.get('falta_en_odoo',0):>10,}  "
          f"({t.get('unidades_perdidas',0):,} uds sin vender)")
    print(f"   cantidad distinta  {t.get('cantidad_distinta',0):>10,}")

    print(f"\n{'almacen':<9}{'fiab%':>7}{'coinciden':>11}{'sobra':>9}"
          f"{'falta':>9}{'distinta':>10}")
    for code, d in r["proveedores"].items():
        print(f"{code:<9}{d['fiabilidad_pct']:>7}{d['coinciden']:>11,}"
              f"{d['sobra_en_odoo']:>9,}{d['falta_en_odoo']:>9,}"
              f"{d['cantidad_distinta']:>10,}")

    if r.get("almacenes_sin_proveedor"):
        print(f"\nALMACENES SIN PROVEEDOR (no los alimenta ningun fichero):")
        for code, a in r["almacenes_sin_proveedor"].items():
            print(f"   {code:<9}{a['libros']:>9,} libros{a['unidades']:>11,} uds")

    print(f"\nESPEJO:")
    for k, v in r.get("espejo", {}).items():
        print(f"   {k:<32}{v}")

    print(f"\nCATALOGO:")
    for k, v in r["catalogo"].items():
        if k != "muestra_duplicados":
            print(f"   {k:<32}{v}")

    print(f"\nHUERFANOS (stock sin proveedor): {r['huerfanos'].get('libros',0):,} "
          f"libros, {r['huerfanos'].get('unidades',0):,} uds")
    for code, h in r["huerfanos"].get("por_almacen", {}).items():
        print(f"   {code:<9}{h['libros']:>9,} libros{h['unidades']:>11,} uds")

    print(f"\nHALLAZGOS:")
    for h in r["hallazgos"]:
        print(f"   [{h['nivel'].upper():<5}] {h['titulo']}")
        print(f"           {h['detalle']}")
        if h["accion"]:
            print(f"           -> {h['accion']}")
