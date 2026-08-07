"""
Crear en Odoo los libros que un proveedor nos manda y no existen alli,
incluidos los que vienen con stock 0.

Por que hace falta un modulo aparte y no vale auto_scrape: el ciclo diario
solo crea libros CON stock (`stock_disponible > 0`), que es lo correcto para
vender. Pero el cliente busca ISBNs en Odoo y, si no estan, entiende que se
han perdido. Casi siempre no se han perdido: el proveedor los manda con cero
existencias. Crearlos con stock 0 los hace consultables sin hacerlos
vendibles.

La diferencia importante con auto_scrape esta en `active`:

    auto_scrape   active = (hay precio y es >= 2,90)
    aqui          active = True siempre

No es un descuido. Con la regla de auto_scrape, 5.815 de los 5.866 se
crearian ARCHIVADOS —no tienen precio porque el proveedor no lo manda si no
hay existencias— y seguirian sin verse al buscarlos. Se crearia el problema
otra vez y encima con 5.815 productos nuevos. Como no tienen stock no se
pueden vender igualmente, asi que estar activos no expone nada.

Antes de crear se intenta sacar el titulo de Casa del Libro para los ISBN
espanoles, que si no se quedan con el ISBN por nombre.
"""
import asyncio
import time
from datetime import datetime

import db
import auto_scrape
from odoo_client import OdooClient
from odoo_tags import _resolve_tags

LOTE = 200

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


def faltantes(limite: int | None = None,
              solo_proveedor: str | None = None) -> list[dict]:
    """
    ISBNs que algun proveedor nos manda y no estan en Odoo, con o sin stock.
    Misma ficha que auto_scrape: lo scrapeado en CDL y, si no, el catalogo
    del propio distribuidor.

    Solo codigos 978/979, que son los ISBN de verdad. Anaya y Machado
    mezclan en sus ficheros 415 codigos internos suyos (rango EAN 20-29 y
    otros); crearlos como productos ensuciaria Odoo y ademas nadie los va a
    buscar, porque el cliente pregunta por ISBN.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    filtro, params = "", []
    if solo_proveedor:
        filtro = " AND lp.proveedor_email = %s"
        params = [solo_proveedor]
    q = f"""
        SELECT lp.isbn,
               MAX(lp.precio_con_iva) AS precio,
               COALESCE(MAX(lp.stock_disponible), 0) AS stock,
               string_agg(DISTINCT lp.proveedor_email, ', ') AS proveedores,
               COALESCE(MAX(NULLIF(b.title, '')), MAX(NULLIF(d.title, ''))) AS title,
               COALESCE(MAX(NULLIF(b.author, '')), MAX(NULLIF(d.author, ''))) AS author,
               COALESCE(MAX(NULLIF(b.editorial, '')), MAX(NULLIF(d.editorial, ''))) AS editorial,
               COALESCE(MAX(NULLIF(b.image_url, '')), MAX(NULLIF(d.image_url, ''))) AS image_url,
               COALESCE(MAX(NULLIF(b.description, '')), MAX(NULLIF(d.description, ''))) AS description,
               COALESCE(MAX(b.weight), MAX(d.weight)) AS weight,
               COALESCE(MAX(b.height), MAX(d.height)) AS height,
               COALESCE(MAX(b.width), MAX(d.width)) AS width
        FROM libros_proveedor lp
        LEFT JOIN odoo_books_mirror m ON m.barcode = lp.isbn
        LEFT JOIN books b ON b.isbn = lp.isbn
        LEFT JOIN distributor_books d ON d.isbn = lp.isbn
        WHERE m.odoo_id IS NULL AND lp.isbn IS NOT NULL
          AND lp.isbn ~ '^97[89][0-9]{{10}}$'{filtro}
        GROUP BY lp.isbn
        ORDER BY lp.isbn
    """
    if limite:
        q += f" LIMIT {int(limite)}"
    try:
        cur.execute(q, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


async def _crear(odoo: OdooClient, tag_ids: dict, targets: list[dict],
                 job: dict):
    for i in range(0, len(targets), LOTE):
        if job["status"] != "running":
            break
        lote = targets[i:i + LOTE]
        vals = []
        for t in lote:
            nombre = (t.get("title") or "").strip() or t["isbn"]
            precio = float(t["precio"]) if t.get("precio") else 0.0
            vals.append({
                "name": nombre[:250], "barcode": t["isbn"],
                "default_code": t["isbn"],
                "list_price": precio,
                # A diferencia de auto_scrape, siempre activo: ver cabecera.
                "active": True,
                "type": "consu", "sale_ok": True, "purchase_ok": True,
                "is_storable": True,
            })
        try:
            nuevos = await odoo.execute_kw("product.template", "create", [vals])
        except Exception as e:
            job["errors"].append(f"lote@{i}: {type(e).__name__}: {str(e)[:140]}")
            job["fallidos"] += len(lote)
            continue

        for t, oid in zip(lote, nuevos):
            t["odoo_id"] = oid
            t["tag"] = auto_scrape._classify(
                t.get("image_url"), t.get("description"),
                t.get("weight"), t.get("height"), t.get("width"))
        auto_scrape._mirror_upsert(lote)

        por_tag: dict[str, list[int]] = {}
        for t in lote:
            por_tag.setdefault(t["tag"], []).append(t["odoo_id"])
        for tag, ids in por_tag.items():
            if tag_ids.get(tag):
                try:
                    await odoo.write("product.template", ids,
                                     {"product_tag_ids": [(4, tag_ids[tag])]})
                except Exception as e:
                    job["errors"].append(f"etiqueta {tag}: {str(e)[:100]}")
        job["creados"] += len(lote)
        if job["creados"] % 1000 < LOTE:
            print(f"[CrearFaltantes] {job['creados']:,}/{len(targets):,}",
                  flush=True)


async def crear(dry_run: bool = True, limite: int | None = None,
                scrapear: bool = True,
                solo_proveedor: str | None = None) -> dict:
    """
    Crea en Odoo los libros que faltan, activos y con stock 0.
    Empezar siempre con dry_run: dice cuantos crearia y con cuanta ficha.
    """
    global _job
    _job = {
        "status": "running", "dry_run": dry_run,
        "started_at": datetime.now().isoformat(), "stage": "buscando",
        "faltantes": 0, "con_titulo": 0, "sin_titulo": 0, "espanoles": 0,
        "scrapeados": 0, "creados": 0, "fallidos": 0,
        "errors": [], "elapsed_s": 0, "muestra": [],
    }
    job = _job
    t0 = time.monotonic()
    try:
        targets = faltantes(limite=limite, solo_proveedor=solo_proveedor)
        job["faltantes"] = len(targets)
        job["espanoles"] = sum(
            1 for t in targets
            if str(t["isbn"]).startswith(auto_scrape.PREFIJOS_ES))
        job["muestra"] = [
            {"isbn": t["isbn"], "titulo": t.get("title"),
             "precio": float(t["precio"]) if t.get("precio") else None,
             "stock": float(t.get("stock") or 0),
             "proveedores": t.get("proveedores")}
            for t in targets[:25]]

        if not targets:
            job["stage"] = "nada que crear"
            job["status"] = "completed"
            return job

        if scrapear and not dry_run:
            job["stage"] = "buscando fichas en Casa del Libro"
            try:
                job["scrapeados"] = await auto_scrape._scrape_missing(targets)
            except Exception as e:
                job["errors"].append(f"scraping: {type(e).__name__}: {str(e)[:140]}")

        job["con_titulo"] = sum(1 for t in targets if (t.get("title") or "").strip())
        job["sin_titulo"] = len(targets) - job["con_titulo"]

        if dry_run:
            job["stage"] = "dry_run_done"
            job["status"] = "completed"
            return job

        job["stage"] = "creando en Odoo"
        async with OdooClient() as odoo:
            tag_ids = await _resolve_tags(odoo)
            await _crear(odoo, tag_ids, targets, job)
        job["stage"] = "done"
        if job["status"] == "running":
            job["status"] = "completed"
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e}"[:300])
        print(f"[CrearFaltantes] FAIL: {e!r}")
    finally:
        job["elapsed_s"] = round(time.monotonic() - t0, 1)
        try:
            import audit_log
            audit_log.log_event(
                "crear_faltantes",
                "dry_run" if dry_run else "creacion",
                f"Libros que faltaban en Odoo{' [DRY RUN]' if dry_run else ''}: "
                f"{job['faltantes']:,} detectados, {job['scrapeados']:,} "
                f"con ficha nueva, {job['creados']:,} creados con stock 0 "
                f"({job['elapsed_s']}s)",
                detalle={k: job.get(k) for k in
                         ("faltantes", "espanoles", "con_titulo", "sin_titulo",
                          "scrapeados", "creados", "fallidos")},
                nivel="error" if job["status"] == "error" else "info")
        except Exception:
            pass
    return job


if __name__ == "__main__":
    import sys
    seco = "--apply" not in sys.argv
    lim = None
    for a in sys.argv:
        if a.startswith("--limite="):
            lim = int(a.split("=")[1])
    r = asyncio.run(crear(dry_run=seco, limite=lim))
    print(f"\n{'='*62}\n{'DRY RUN' if seco else 'CREACION'}  estado={r['status']}  "
          f"({r['elapsed_s']}s)\n{'='*62}")
    print(f"   faltan en Odoo     {r['faltantes']:>8,}")
    print(f"   espanoles          {r['espanoles']:>8,}  (scrapeables en CDL)")
    print(f"   con titulo         {r['con_titulo']:>8,}")
    print(f"   sin titulo         {r['sin_titulo']:>8,}  (se crean con el ISBN por nombre)")
    print(f"   fichas scrapeadas  {r['scrapeados']:>8,}")
    print(f"   creados            {r['creados']:>8,}")
    print(f"   fallidos           {r['fallidos']:>8,}")
    if r["muestra"]:
        print("\n   muestra:")
        for m in r["muestra"][:10]:
            print(f"      {m['isbn']}  stock={m['stock']:<5.0f} "
                  f"precio={str(m['precio'] or '—'):<7} "
                  f"{(m['titulo'] or '(sin titulo)')[:34]}")
    for e in r["errors"][:5]:
        print(f"   ERROR: {e}")
