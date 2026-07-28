"""
Enriquece via CDL los ISBNs con stock que NO existen en Odoo, y guarda el
resultado en la tabla `books`. Luego (otro paso) se crean en Odoo.

Solo ISBNs reales enriquecibles por CDL (libreria espanola): 978/979
excluyendo ingles (9780/9781), frances (9782) y aleman (9783), y los
codigos que no son ISBN. Resumible: salta los que ya estan en `books`.

Uso:
    DATABASE_URL=... PROXY_POOL=... python scripts/enrich_faltantes.py --limit 100
    DATABASE_URL=... PROXY_POOL=... python scripts/enrich_faltantes.py --concurrency 14
"""
import argparse
import asyncio
import os
import sys
import time

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import cdl_http_client as cdl

PROXIES = [p.strip() for p in os.environ.get("PROXY_POOL", "").split(",") if p.strip()]

# Columnas de `books` que rellenamos desde el scrape
COLS = ["isbn", "title", "author", "editorial", "image_url", "weight",
        "height", "width", "pages", "binding", "language", "release_date",
        "edition_year", "edition_place", "collection", "translator",
        "illustrator", "reading_time", "description", "url",
        "categoria_1", "categoria_2", "categoria_3"]


def target_isbns(limit=None):
    conn = db.get_connection(); cur = conn.cursor()
    q = """
        SELECT isbn FROM (
            SELECT DISTINCT lp.isbn
            FROM libros_proveedor lp
            LEFT JOIN odoo_books_mirror m ON m.barcode = lp.isbn
            LEFT JOIN books b ON b.isbn = lp.isbn AND b.title IS NOT NULL AND b.title <> ''
            WHERE lp.stock_disponible > 0
              AND m.barcode IS NULL
              AND b.isbn IS NULL
              AND lp.isbn LIKE '97884%'
        ) t
    """
    if limit:
        q += f" LIMIT {int(limit)}"
    cur.execute(q)
    out = [r[0] for r in cur.fetchall()]
    conn.close()
    return out


def save_batch(rows: list[dict]) -> int:
    if not rows:
        return 0
    from psycopg2.extras import execute_values
    conn = db.get_connection(); cur = conn.cursor()
    values = [tuple((r.get(c) or None) for c in COLS) for r in rows]
    colnames = ", ".join(COLS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLS if c != "isbn")
    sql = f"""
        INSERT INTO books ({colnames}, fuente, timestamp)
        VALUES %s
        ON CONFLICT (isbn) WHERE isbn IS NOT NULL
        DO UPDATE SET {updates}, fuente='cdl', timestamp=NOW()
    """
    tmpl = "(" + ", ".join(["%s"] * len(COLS)) + ", 'cdl', NOW())"
    execute_values(cur, sql, values, template=tmpl, page_size=len(values))
    conn.commit(); conn.close()
    return len(rows)


async def worker(name, session, queue, results, stats):
    while True:
        try:
            isbn = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        proxy = PROXIES[stats["i"] % len(PROXIES)] if PROXIES else None
        stats["i"] += 1
        try:
            d = await cdl.fetch_book_by_isbn(session, isbn, timeout_s=15, proxy=proxy)
            if d and d.get("title"):
                d["isbn"] = isbn
                results.append(d)
                stats["found"] += 1
            else:
                stats["notfound"] += 1
        except cdl.CDLBlocked:
            stats["blocked"] += 1
            await asyncio.sleep(1.0)
            queue.put_nowait(isbn)  # reintentar con otra proxy
        except Exception:
            stats["error"] += 1
        finally:
            stats["done"] += 1


async def main(limit, concurrency):
    isbns = target_isbns(limit)
    total = len(isbns)
    print(f"A enriquecer: {total:,} ISBNs | proxies: {len(PROXIES)} | concurrencia: {concurrency}", flush=True)
    if not total:
        return
    queue = asyncio.Queue()
    for i in isbns:
        queue.put_nowait(i)
    results = []
    stats = {"done": 0, "found": 0, "notfound": 0, "blocked": 0, "error": 0, "i": 0}
    t0 = time.time()
    connector = aiohttp.TCPConnector(limit=concurrency * 2, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [asyncio.create_task(worker(f"w{n}", session, queue, results, stats))
                 for n in range(concurrency)]
        # guardado periodico + progreso
        last_saved = 0
        while any(not t.done() for t in tasks):
            await asyncio.sleep(15)
            if len(results) - last_saved >= 200:
                n = save_batch(results[last_saved:])
                last_saved += n
            el = time.time() - t0
            rate = stats["done"] / el if el else 0
            eta = (total - stats["done"]) / rate / 60 if rate else 0
            print(f"  {stats['done']:,}/{total:,} | found={stats['found']:,} "
                  f"notfound={stats['notfound']:,} blocked={stats['blocked']} "
                  f"err={stats['error']} | {rate:.1f}/s ETA {eta:.0f}min", flush=True)
        await asyncio.gather(*tasks)
    # guardar lo que quede
    if len(results) > last_saved:
        save_batch(results[last_saved:])
    el = time.time() - t0
    print(f"LISTO en {el/60:.1f}min: {stats['found']:,} enriquecidos, "
          f"{stats['notfound']:,} no encontrados, {stats['error']} errores", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=12)
    a = ap.parse_args()
    asyncio.run(main(a.limit, a.concurrency))
