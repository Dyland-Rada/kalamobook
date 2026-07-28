"""
Ciclo autonomo de libros nuevos: detecta ISBNs con stock que no estan en
Odoo, los enriquece via CDL, los crea + etiqueta, genera un reporte Excel
y avisa a Server A por webhook (ellos envian el correo).

Pensado para correr 1x/dia (n8n) o via cron interno. Reutiliza:
  - cdl_http_client (scraping)
  - odoo_tags._classify / _resolve_tags (etiquetas Completo/Web/Foto/Stock)
  - OdooClient (crear product.template)

Endpoints en app.py: POST /api/v1/auto-scrape/run, GET /api/v1/reportes/{id}
"""
import asyncio
import json
import os
import time
import urllib.request
import uuid
from datetime import datetime

import aiohttp
import openpyxl

import db
import cdl_http_client as cdl
from odoo_client import OdooClient
from odoo_tags import _classify, _resolve_tags

REPORTS_DIR = os.environ.get("REPORTS_DIR", "reports")
WEBHOOK_URL = os.environ.get("SCRAPE_WEBHOOK_URL", "")
WEBHOOK_TOKEN = os.environ.get("SCRAPE_WEBHOOK_TOKEN", "")
PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "https://kalamob.reinventaconia.com")
PROXIES = [p.strip() for p in os.environ.get("PROXY_POOL", "").split(",") if p.strip()]
CONCURRENCY = int(os.environ.get("AUTO_SCRAPE_CONCURRENCY", "12"))

_BOOK_COLS = ["title", "author", "editorial", "image_url", "description",
              "weight", "height", "width"]

auto_scrape_job: dict = {"status": "idle"}


def get_status() -> dict:
    return dict(auto_scrape_job)


# ── 1. Detectar nuevos ────────────────────────────────────────────────
def _detect_new(limit=None) -> list[dict]:
    """ISBNs con stock, no en Odoo. Junta precio, proveedores y metadata
    de books si ya la tenemos."""
    conn = db.get_connection(); cur = conn.cursor()
    q = """
        SELECT lp.isbn,
               MAX(lp.precio_con_iva) AS precio,
               string_agg(DISTINCT p.nombre, ', ') AS proveedores,
               MAX(b.title) AS title, MAX(b.author) AS author,
               MAX(b.editorial) AS editorial, MAX(b.image_url) AS image_url,
               MAX(b.description) AS description, MAX(b.weight) AS weight,
               MAX(b.height) AS height, MAX(b.width) AS width
        FROM libros_proveedor lp
        LEFT JOIN odoo_books_mirror m ON m.barcode = lp.isbn
        LEFT JOIN proveedores p ON p.id = lp.proveedor_id
        LEFT JOIN books b ON b.isbn = lp.isbn
        WHERE lp.stock_disponible > 0 AND m.barcode IS NULL
        GROUP BY lp.isbn
    """
    if limit:
        q += f" LIMIT {int(limit)}"
    cur.execute(q)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows


# ── 2. Enriquecer via CDL los que no tengan titulo (solo espanoles) ──
def _save_books(results: list[dict]):
    if not results:
        return
    from psycopg2.extras import execute_values
    cols = ["isbn"] + _BOOK_COLS
    conn = db.get_connection(); cur = conn.cursor()
    vals = [tuple(r.get(c) or None for c in cols) for r in results]
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in _BOOK_COLS)
    tmpl = "(" + ",".join(["%s"] * len(cols)) + ", 'cdl', NOW())"
    execute_values(cur, f"""
        INSERT INTO books ({", ".join(cols)}, fuente, timestamp)
        VALUES %s
        ON CONFLICT (isbn) WHERE isbn IS NOT NULL
        DO UPDATE SET {updates}, fuente='cdl', timestamp=NOW()
    """, vals, template=tmpl, page_size=len(vals))
    conn.commit(); conn.close()


async def _scrape_missing(targets: list[dict]) -> int:
    """Scrapea CDL los targets espanoles sin titulo. Actualiza el dict
    in-memory y guarda en books. Devuelve cuantos consiguio."""
    pend = [t for t in targets
            if not (t.get("title") or "").strip() and str(t["isbn"]).startswith("97884")]
    if not pend:
        return 0
    auto_scrape_job["stage"] = f"scraping ({len(pend)})"
    queue = asyncio.Queue()
    for t in pend:
        queue.put_nowait(t)
    saved = []
    counter = {"i": 0, "found": 0}

    async def worker(session):
        while True:
            try:
                t = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            proxy = PROXIES[counter["i"] % len(PROXIES)] if PROXIES else None
            counter["i"] += 1
            try:
                d = await cdl.fetch_book_by_isbn(session, t["isbn"], timeout_s=15, proxy=proxy)
                if d and d.get("title"):
                    for c in _BOOK_COLS:
                        if d.get(c):
                            t[c] = d[c]
                    saved.append({"isbn": t["isbn"], **{c: t.get(c) for c in _BOOK_COLS}})
                    counter["found"] += 1
            except cdl.CDLBlocked:
                await asyncio.sleep(1.0)
                queue.put_nowait(t)
            except Exception:
                pass

    conn = aiohttp.TCPConnector(limit=CONCURRENCY * 2, ssl=False)
    async with aiohttp.ClientSession(connector=conn) as s:
        await asyncio.gather(*[worker(s) for _ in range(CONCURRENCY)])
    _save_books(saved)
    return counter["found"]


# ── 3. Crear + etiquetar en Odoo ─────────────────────────────────────
def _mirror_upsert(items: list[dict]):
    from psycopg2.extras import execute_values
    conn = db.get_connection(); cur = conn.cursor()
    vals = [(it["odoo_id"], it["isbn"], it.get("title"), it.get("precio"),
             it.get("image_url"), it.get("description"),
             it.get("weight"), it.get("height"), it.get("width")) for it in items]
    execute_values(cur, """
        INSERT INTO odoo_books_mirror
            (odoo_id, barcode, name, list_price, cdl_image_url, description,
             cdl_weight, cdl_height, cdl_width, synced_at)
        VALUES %s
        ON CONFLICT (odoo_id) DO UPDATE SET
            barcode=EXCLUDED.barcode, name=EXCLUDED.name,
            list_price=EXCLUDED.list_price, cdl_image_url=EXCLUDED.cdl_image_url,
            description=EXCLUDED.description, cdl_weight=EXCLUDED.cdl_weight,
            cdl_height=EXCLUDED.cdl_height, cdl_width=EXCLUDED.cdl_width, synced_at=NOW()
    """, vals, template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())", page_size=len(vals))
    conn.commit(); conn.close()


async def _create_and_tag(odoo, tag_ids, targets, chunk=200):
    for i in range(0, len(targets), chunk):
        batch = targets[i:i + chunk]
        vals = []
        for t in batch:
            name = (t.get("title") or "").strip() or t["isbn"]
            vals.append({
                "name": name[:250], "barcode": t["isbn"], "default_code": t["isbn"],
                "list_price": float(t["precio"]) if t.get("precio") else 0.0,
                "type": "consu", "sale_ok": True, "purchase_ok": True,
            })
        new_ids = await odoo.execute_kw("product.template", "create", [vals])
        for t, oid in zip(batch, new_ids):
            t["odoo_id"] = oid
            t["tag"] = _classify(t.get("image_url"), t.get("description"),
                                 t.get("weight"), t.get("height"), t.get("width"))
        _mirror_upsert(batch)
        by_tag = {}
        for t in batch:
            by_tag.setdefault(t["tag"], []).append(t["odoo_id"])
        for tag, ids in by_tag.items():
            if tag_ids.get(tag):
                await odoo.write("product.template", ids, {"product_tag_ids": [(4, tag_ids[tag])]})
        auto_scrape_job["creados"] = auto_scrape_job.get("creados", 0) + len(batch)


# ── 4. Reporte Excel ─────────────────────────────────────────────────
def _clean(s):
    if s is None:
        return ""
    return "".join(c for c in str(s)
                   if c in "\t\n\r" or ("\x20" <= c <= "퟿")
                   or ("" <= c <= "�") or c >= "\U00010000").strip()


def _purge_old_reports(days: int = 30):
    """Borra reportes de mas de N dias (limpieza de disco)."""
    try:
        cutoff = time.time() - days * 86400
        for f in os.listdir(REPORTS_DIR):
            fp = os.path.join(REPORTS_DIR, f)
            if f.endswith(".xlsx") and os.path.getmtime(fp) < cutoff:
                os.remove(fp)
    except Exception:
        pass


def _build_report(targets: list[dict], report_id: str) -> str:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    _purge_old_reports()
    path = os.path.join(REPORTS_DIR, f"{report_id}.xlsx")
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("Nuevos")
    ws.append(["ISBN", "Titulo", "Autor", "Editorial", "Precio", "Peso_g",
               "Imagen_URL", "Descripcion", "Proveedores", "Estado", "Etiqueta"])
    for t in targets:
        scraped = bool((t.get("title") or "").strip())
        ws.append([t["isbn"], _clean(t.get("title")), _clean(t.get("author")),
                   _clean(t.get("editorial")),
                   float(t["precio"]) if t.get("precio") else "",
                   _clean(t.get("weight")), _clean(t.get("image_url")),
                   _clean(t.get("description")), _clean(t.get("proveedores")),
                   "Scrapeado" if scraped else "No scrapeado", t.get("tag", "")])
    wb.save(path)
    return path


# ── 5. Webhook a Server A ────────────────────────────────────────────
def _send_webhook(summary: dict, archivo_url: str, muestra: list[str]) -> dict:
    if not WEBHOOK_URL:
        return {"sent": False, "reason": "SCRAPE_WEBHOOK_URL vacio"}
    payload = json.dumps({
        "evento": "auto_scrape_report",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "resumen": summary,
        "archivo_url": archivo_url,
        "no_scrapeados_muestra": muestra[:50],
    }).encode()
    req = urllib.request.Request(
        WEBHOOK_URL, data=payload, method="POST",
        headers={"Content-Type": "application/json", "x-kalamo-token": WEBHOOK_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"sent": True, "status": r.status}
    except Exception as e:
        return {"sent": False, "reason": str(e)[:150]}


# ── Orquestador ──────────────────────────────────────────────────────
async def run_auto_scrape_cycle(max_new: int | None = None) -> dict:
    global auto_scrape_job
    report_id = "autoscrape_" + uuid.uuid4().hex[:12]
    auto_scrape_job = {"status": "running", "stage": "detectando",
                       "started_at": datetime.now().isoformat(),
                       "report_id": report_id, "creados": 0}
    t0 = time.monotonic()
    try:
        targets = _detect_new(limit=max_new)
        auto_scrape_job["total"] = len(targets)
        if not targets:
            auto_scrape_job.update(status="completed", stage="sin nuevos",
                                   elapsed_s=round(time.monotonic() - t0, 1))
            return auto_scrape_job

        scraped = await _scrape_missing(targets)
        auto_scrape_job["scrapeados"] = scraped

        auto_scrape_job["stage"] = "creando+etiquetando"
        async with OdooClient() as odoo:
            tag_ids = await _resolve_tags(odoo)
            await _create_and_tag(odoo, tag_ids, targets)

        con_titulo = sum(1 for t in targets if (t.get("title") or "").strip())
        summary = {"total": len(targets), "scrapeados": con_titulo,
                   "no_scrapeados": len(targets) - con_titulo}
        auto_scrape_job["stage"] = "reporte"
        _build_report(targets, report_id)
        archivo_url = f"{PUBLIC_BASE}/api/v1/reportes/{report_id}"
        muestra = [t["isbn"] for t in targets if not (t.get("title") or "").strip()]

        auto_scrape_job["stage"] = "webhook"
        wh = _send_webhook(summary, archivo_url, muestra)
        auto_scrape_job.update(status="completed", stage="done", resumen=summary,
                               webhook=wh, archivo_url=archivo_url,
                               elapsed_s=round(time.monotonic() - t0, 1))
        try:
            import audit_log
            audit_log.log_event("auto_scrape", "cycle_done",
                                f"Nuevos: {summary['total']:,} creados, "
                                f"{summary['scrapeados']:,} scrapeados, "
                                f"{summary['no_scrapeados']:,} no. Webhook: {wh.get('sent')}",
                                detalle={"resumen": summary, "webhook": wh})
        except Exception:
            pass
        return auto_scrape_job
    except Exception as e:
        auto_scrape_job.update(status="error", error=f"{type(e).__name__}: {e!r}"[:300],
                               elapsed_s=round(time.monotonic() - t0, 1))
        return auto_scrape_job
