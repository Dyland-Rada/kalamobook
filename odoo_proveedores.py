"""
Rellenar en Odoo que proveedor sirve cada libro (product.supplierinfo).

El campo estaba a medias desde una carga de mayo de 2026: AZETA tenia
1.035.632 relaciones y Grupo Anaya CERO, asi que al mirar un libro de Anaya
en Odoo aparecia AZETA. El dato correcto lo tenemos en libros_proveedor y
nunca se escribia.

Que hace:
  1. Crea como contacto los proveedores que faltan en Odoo
  2. Anade las relaciones libro-proveedor que faltan, con su precio real

Que NO hace: borrar. Las relaciones de mas se reportan pero no se tocan,
porque `supplierinfo` significa "este proveedor PUEDE servir este libro",
no "lo tiene ahora". AZETA lista casi un millon de titulos en su catalogo,
asi que muchas de sus relaciones son legitimas aunque hoy no tenga stock.
Borrar es una decision aparte y con mas consecuencias.
"""
import asyncio
import os
import time
from datetime import datetime

import db
from odoo_client import OdooClient

LOTE_CREAR = int(os.environ.get("ODOO_PROV_LOTE", "200"))
PAGINA_LECTURA = 40000

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


async def _contactos(odoo: OdooClient, crear_faltantes: bool) -> dict[str, int]:
    """
    {proveedor_email: id del contacto en Odoo}. Empareja por nombre y crea
    los que falten (ARCOBALENO, PODIPRINT y ALFAOMEGA no estaban).
    """
    partners = await odoo.search_read(
        "res.partner", [["supplier_rank", ">", 0]], ["id", "name"])
    por_nombre = {(p["name"] or "").strip().upper(): p["id"] for p in partners}

    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT DISTINCT lp.proveedor_email,
                   COALESCE(p.nombre, m.nombre_proveedor, lp.proveedor_email)
            FROM libros_proveedor lp
            LEFT JOIN proveedores p ON p.id = lp.proveedor_id
            LEFT JOIN proveedor_almacen_odoo m
                   ON m.proveedor_email = lp.proveedor_email
        """)
        filas = cur.fetchall()
    finally:
        conn.close()

    salida = {}
    for email, nombre in filas:
        n = (nombre or "").strip().upper()
        pid = por_nombre.get(n)
        if not pid:
            # Emparejado por prefijo, en los dos sentidos y con el nombre mas
            # corto como referencia. Comparar los 12 primeros caracteres de
            # ambos NO vale: 'AZETA' contra 'AZETA DISTRIBUCIONES' no casaba
            # y creo un contacto duplicado con 52.466 relaciones equivocadas.
            candidatos = [(k, v) for k, v in por_nombre.items()
                          if k and n and (k.startswith(n) or n.startswith(k))
                          and min(len(k), len(n)) >= 5]
            if candidatos:
                # el mas parecido en longitud, para no casar con un prefijo comun
                pid = min(candidatos, key=lambda x: abs(len(x[0]) - len(n)))[1]
        if not pid and crear_faltantes and nombre:
            # `company_type` no existe en este Odoo (es calculado): se marca
            # con is_company, que es el campo real.
            pid = await odoo.execute_kw("res.partner", "create", [{
                "name": nombre, "supplier_rank": 1, "is_company": True,
            }])
            por_nombre[n] = pid
            print(f"[OdooProv] contacto creado: {nombre} -> {pid}")
        if pid:
            salida[email] = pid
    return salida


def _relaciones_correctas(contactos: dict[str, int]) -> set[tuple[int, int]]:
    """{(odoo_id, partner_id)} de lo que SI deberia estar registrado."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT m.odoo_id, lp.proveedor_email, MAX(lp.precio_con_iva)
            FROM libros_proveedor lp
            JOIN odoo_books_mirror m ON m.barcode = lp.isbn
            WHERE m.odoo_id IS NOT NULL
            GROUP BY m.odoo_id, lp.proveedor_email
        """)
        salida = {}
        for odoo_id, email, precio in cur.fetchall():
            pid = contactos.get(email)
            if pid:
                salida[(int(odoo_id), pid)] = float(precio) if precio else 0.0
        return salida
    finally:
        conn.close()


async def _relaciones_existentes(odoo: OdooClient) -> set[tuple[int, int]]:
    """{(odoo_id, partner_id)} de lo que ya hay, leido por paginas."""
    existentes = set()
    offset = 0
    while True:
        pagina = await odoo.search_read(
            "product.supplierinfo", [], ["product_tmpl_id", "partner_id"],
            offset=offset, limit=PAGINA_LECTURA, order="id")
        for r in pagina:
            t = r.get("product_tmpl_id")
            p = r.get("partner_id")
            if t and p:
                existentes.add((t[0] if isinstance(t, list) else t,
                                p[0] if isinstance(p, list) else p))
        if len(pagina) < PAGINA_LECTURA:
            break
        offset += PAGINA_LECTURA
        print(f"[OdooProv] leidas {len(existentes):,} relaciones", flush=True)
    return existentes


async def sincronizar(dry_run: bool = True, limite: int | None = None) -> dict:
    """
    Anade las relaciones libro-proveedor que faltan en Odoo.
    Empezar siempre con dry_run: dice cuantas crearia y cuantas sobran.
    """
    global _job
    _job = {
        "status": "running", "dry_run": dry_run,
        "started_at": datetime.now().isoformat(), "stage": "empezando",
        "contactos": 0, "contactos_creados": 0,
        "deberian": 0, "existentes": 0, "faltan": 0, "sobran": 0,
        "creadas": 0, "errors": [], "elapsed_s": 0,
        "por_proveedor": {},
    }
    job = _job
    t0 = time.monotonic()
    try:
        async with OdooClient() as odoo:
            job["stage"] = "emparejando proveedores"
            antes = await odoo.search_count("res.partner", [["supplier_rank", ">", 0]])
            contactos = await _contactos(odoo, crear_faltantes=not dry_run)
            despues = await odoo.search_count("res.partner", [["supplier_rank", ">", 0]])
            job["contactos"] = len(contactos)
            job["contactos_creados"] = max(0, despues - antes)

            job["stage"] = "leyendo lo que deberia haber"
            correctas = _relaciones_correctas(contactos)
            job["deberian"] = len(correctas)

            job["stage"] = "leyendo lo que hay en Odoo"
            existentes = await _relaciones_existentes(odoo)
            job["existentes"] = len(existentes)

            faltan = [(k, v) for k, v in correctas.items() if k not in existentes]
            job["faltan"] = len(faltan)
            job["sobran"] = len(existentes - set(correctas))

            # desglose por proveedor, que es lo que se quiere ver
            nombres = {v: k for k, v in contactos.items()}
            cuenta = {}
            for (odoo_id, pid), _ in faltan:
                cuenta[nombres.get(pid, pid)] = cuenta.get(nombres.get(pid, pid), 0) + 1
            job["por_proveedor"] = dict(sorted(cuenta.items(),
                                               key=lambda x: -x[1])[:20])

            if dry_run:
                job["stage"] = "dry_run_done"
                job["status"] = "completed"
                return job

            if limite:
                faltan = faltan[:limite]
            job["stage"] = "creando relaciones"
            for i in range(0, len(faltan), LOTE_CREAR):
                if job["status"] != "running":
                    break
                trozo = faltan[i:i + LOTE_CREAR]
                vals = [{"product_tmpl_id": t, "partner_id": p, "price": precio}
                        for (t, p), precio in trozo]
                try:
                    await odoo.execute_kw("product.supplierinfo", "create", [vals])
                    job["creadas"] += len(vals)
                except Exception as e:
                    job["errors"].append(f"lote@{i}: {type(e).__name__}: {str(e)[:120]}")
                if job["creadas"] % 5000 < LOTE_CREAR:
                    print(f"[OdooProv] {job['creadas']:,}/{len(faltan):,} "
                          f"({time.monotonic() - t0:.0f}s)", flush=True)
            job["stage"] = "done"
        if job["status"] == "running":
            job["status"] = "completed"
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e}"[:300])
        print(f"[OdooProv] FAIL: {e!r}")
    finally:
        job["elapsed_s"] = round(time.monotonic() - t0, 1)
        try:
            import audit_log
            audit_log.log_event(
                "odoo_proveedores",
                "dry_run" if dry_run else "sincronizacion",
                f"Proveedor en Odoo{' [DRY RUN]' if dry_run else ''}: "
                f"{job['deberian']:,} deberian, {job['existentes']:,} existen, "
                f"{job['faltan']:,} faltan, {job['creadas']:,} creadas "
                f"({job['elapsed_s']}s)",
                detalle={k: job.get(k) for k in
                         ("deberian", "existentes", "faltan", "sobran",
                          "creadas", "contactos_creados", "por_proveedor")},
                nivel="error" if job["status"] == "error" else "info")
        except Exception:
            pass
    return job


if __name__ == "__main__":
    import sys
    seco = "--apply" not in sys.argv
    r = asyncio.run(sincronizar(dry_run=seco))
    print(f"\n{r['status']}  contactos={r['contactos']} (creados {r['contactos_creados']})")
    print(f"deberian={r['deberian']:,}  existen={r['existentes']:,}  "
          f"faltan={r['faltan']:,}  sobran={r['sobran']:,}  creadas={r['creadas']:,}")
    print("\nfaltan por proveedor:")
    for k, v in r["por_proveedor"].items():
        print(f"   {str(k)[:40]:<42}{v:>9,}")
