import sys
import asyncio
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, Request, Form, Query, BackgroundTasks, Depends, HTTPException, Header, UploadFile, File, status
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
import os
import pandas as pd
import sqlite3
from fastapi.templating import Jinja2Templates
import uvicorn
from scraper import get_book_data, init_db
from bulk_scraper import (
    bulk_scrape, stop_job, get_job_status,
    get_categories, get_all_books, get_books_count,
    discover_categories,
)
from enrichment import (
    run_enrichment_job, stop_enrichment_job,
    get_enrichment_status, get_notfound_count,
    run_build_isbn_index_job, get_isbn_index_job_status,
    get_isbn_index_count,
    retry_notfound_books,
)
import db as dbmod

security = HTTPBasic(auto_error=False)
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "admin123")

# Paths que son publicos (sin HTTP Basic). El webhook de Telegram va aqui
# porque Telegram no manda credenciales — lo aseguramos con chat_id check
# + (opcionalmente) secret_token en el header.
_PUBLIC_PATHS = {"/api/v1/notify/telegram-webhook"}


def verify_credentials(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
):
    if request.url.path in _PUBLIC_PATHS:
        return "public"
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    correct_username = secrets.compare_digest(credentials.username.encode("utf8"), APP_USERNAME.encode("utf8"))
    correct_password = secrets.compare_digest(credentials.password.encode("utf8"), APP_PASSWORD.encode("utf8"))
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

app = FastAPI(
    title="Buscador de Libros API",
    description="API para buscar información de libros en La Casa del Libro Colombia. "
                "Soporta búsqueda por ISBN o nombre del libro.",
    version="2.0.0",
    dependencies=[Depends(verify_credentials)],
)

# Setup templates for the web interface
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
async def startup():
    init_db()
    # Diagnostico de proxy pool — visible al instante en cualquier log viewer.
    from scraper import PROXY_POOL, PROXY_URL, parse_proxy
    print("=" * 60)
    print(f"[Startup] WORKER_NAME={os.environ.get('WORKER_NAME', '(unset)')}")
    if PROXY_POOL:
        valid = [p for p in PROXY_POOL if parse_proxy(p)]
        print(f"[Startup] PROXY_POOL: {len(valid)}/{len(PROXY_POOL)} proxies validos")
        for i, p in enumerate(valid[:3]):
            parsed = parse_proxy(p)
            print(f"[Startup]   #{i+1}: {parsed['server']}")
        if len(valid) > 3:
            print(f"[Startup]   ... y {len(valid) - 3} mas")
    elif PROXY_URL:
        print(f"[Startup] PROXY_URL (singular): {PROXY_URL}")
    else:
        print("[Startup] PROXY: NINGUNO — saliendo con IP directa del server")
    print("=" * 60)


# ─── Web Interface (HTML) ────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def read_root(request: Request):
    """Render the web search interface."""
    return templates.TemplateResponse(request, "index.html")


@app.post("/search", response_class=HTMLResponse, include_in_schema=False)
async def search_book_html(request: Request, query: str = Form(...)):
    """HTML form search (used by the web interface)."""
    print(f"Web Search Request: {query}")
    try:
        data = await get_book_data(query)
        if data:
            return templates.TemplateResponse(request, "index.html", {"result": data})
        else:
            return templates.TemplateResponse(request, "index.html", {"result": {"error": f"No se encontraron resultados para: {query}"}})
    except Exception as e:
        return templates.TemplateResponse(request, "index.html", {"result": {"error": f"Ocurrió un error: {str(e)}"}})


# ─── REST API — Single Book Search ───────────────────────────────────

@app.get(
    "/api/v1/books",
    summary="Buscar libro",
    description="Busca un libro por ISBN o nombre.",
    tags=["Libros"],
)
async def search_book_api(
    q: str = Query(
        ...,
        description="ISBN o nombre del libro",
        min_length=1,
        examples=["9788499899619", "Dune", "Cien años de soledad"],
    )
):
    """Buscar libro por ISBN o nombre."""
    print(f"API Search Request: {q}")
    try:
        data = await get_book_data(q)
        if not data:
            return JSONResponse(status_code=404, content={"status": "error", "message": f"No se encontraron resultados para: {q}"})

        book = {
            "title": data.get("title", ""), "author": data.get("author", ""),
            "editorial": data.get("editorial", ""), "isbn": data.get("isbn", ""),
            "price": data.get("price", ""), "original_price": data.get("original_price", ""),
            "discount": data.get("discount", ""), "description": data.get("description", ""),
            "translator": data.get("translator", ""), "illustrator": data.get("illustrator", ""),
            "language": data.get("language", ""), "pages": data.get("pages", ""),
            "reading_time": data.get("reading_time", ""), "binding": data.get("binding", ""),
            "release_date": data.get("release_date", ""), "edition_year": data.get("edition_year", ""),
            "edition_place": data.get("edition_place", ""), "collection": data.get("collection", ""),
            "dimensions": {"height": data.get("height", ""), "width": data.get("width", ""), "weight": data.get("weight", "")},
            "origin": data.get("origin", ""), "url": data.get("url", ""), "image_url": data.get("image_url", ""),
        }
        return {"status": "success", "data": book}

    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": f"Error interno: {str(e)}"})


# ─── REST API — Bulk Scraping ────────────────────────────────────────

@app.get("/api/v1/bulk/categories", tags=["Scraping Masivo"])
async def list_categories():
    """Lista de categorías disponibles para scraping masivo."""
    return JSONResponse(content={"categories": get_categories()})


@app.post("/api/v1/bulk/discover-categories", tags=["Scraping Masivo"])
async def api_discover_categories():
    """Descubre dinámicamente todas las categorías desde casadellibro.com/libros.
    Actualiza el catálogo interno con categorías nuevas encontradas."""
    discovered = await discover_categories()
    return JSONResponse(content={
        "discovered": len(discovered),
        "categories": discovered,
        "total_registered": len(get_categories()),
    })


@app.post("/api/v1/bulk/start", tags=["Scraping Masivo"])
async def start_bulk_scrape(
    category: str = Query(..., description="Key de la categoría a scrapear"),
    max_books: int = Query(None, description="Máximo de libros a scrapear (None = todos)", ge=1),
):
    """Inicia un job de scraping masivo en background.
    En modo 'all' se hace round-robin infinito: avanza N páginas en cada categoría
    por ronda y rota hasta agotar todas. bulk_scrape se encarga de descubrir
    subcategorías al inicio."""
    cats = {c["key"] for c in get_categories()}
    if category not in cats:
        return JSONResponse(status_code=400, content={"status": "error", "message": f"Categoría '{category}' no encontrada."})

    import threading
    import sys

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(bulk_scrape(category, max_books))
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()

    # Wait briefly for the job_id to be created
    await asyncio.sleep(2)

    # Find the job that was just created
    from bulk_scraper import active_jobs
    latest_job = None
    for jid, job in active_jobs.items():
        if job["category_key"] == category and job["status"] == "running":
            latest_job = job
            break

    if latest_job:
        return JSONResponse(content={"status": "started", "job_id": latest_job["id"]})
    else:
        return JSONResponse(status_code=500, content={"status": "error", "message": "No se pudo iniciar el job."})


@app.get("/api/v1/bulk/status/{job_id}", tags=["Scraping Masivo"])
async def bulk_status(job_id: str):
    """Estado actual de un job de scraping masivo."""
    status = get_job_status(job_id)
    if not status:
        return JSONResponse(status_code=404, content={"status": "error", "message": "Job no encontrado."})
    # Return a safe copy (limit errors list)
    safe = {**status, "errors": status["errors"][-5:]}
    return JSONResponse(content=safe)


@app.post("/api/v1/bulk/stop/{job_id}", tags=["Scraping Masivo"])
async def stop_bulk_scrape(job_id: str):
    """Detiene un job de scraping masivo."""
    if stop_job(job_id):
        return JSONResponse(content={"status": "stopped", "job_id": job_id})
    return JSONResponse(status_code=400, content={"status": "error", "message": "Job no está corriendo o no existe."})


# ─── REST API — Book Library ─────────────────────────────────────────

@app.get("/api/v1/library", tags=["Biblioteca"])
async def list_books(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    search: str = Query("", description="Filtrar por título, autor o ISBN"),
):
    """Lista todos los libros guardados en la BD con paginación."""
    result = get_all_books(page=page, per_page=per_page, search=search)
    return result


@app.get("/api/v1/library/count", tags=["Biblioteca"])
async def books_count():
    """Cantidad total de libros en la BD."""
    return JSONResponse(content={"count": get_books_count()})


@app.get("/api/v1/library/export", tags=["Biblioteca"])
async def export_library(background_tasks: BackgroundTasks):
    """Exporta toda la biblioteca a un archivo Excel (.xlsx)."""
    file_path = "biblioteca_export.xlsx"
    try:
        import db
        conn = db.get_connection()
        df = pd.read_sql_query("SELECT * FROM books", conn)
        conn.close()
        
        df.to_excel(file_path, index=False)
        
        background_tasks.add_task(lambda: os.remove(file_path) if os.path.exists(file_path) else None)
        
        return FileResponse(
            path=file_path,
            filename="Biblioteca_CasaDelLibro.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": f"Error exportando Excel: {e}"})


# ─── REST API — Odoo Enrichment ──────────────────────────────────────

@app.post("/api/v1/odoo/sync/start", tags=["Odoo"])
async def odoo_sync_start():
    """Inicia el job de enriquecimiento de Odoo en background.
    Lee libros sin description_sale del Odoo, scrapea en CDL, y escribe
    HTML enriquecido en el campo `description` del producto."""
    import threading
    import sys

    status = get_enrichment_status()
    if status.get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error",
            "message": "Ya hay un job de enriquecimiento corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(run_enrichment_job())
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    await asyncio.sleep(1)
    return JSONResponse(content={"status": "started"})


@app.post("/api/v1/odoo/sync/stop", tags=["Odoo"])
async def odoo_sync_stop():
    """Detiene el job de enriquecimiento en curso (los workers terminan su tarea actual)."""
    if stop_enrichment_job():
        return JSONResponse(content={"status": "stopped"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "No hay job corriendo."
    })


@app.get("/api/v1/odoo/sync/status", tags=["Odoo"])
async def odoo_sync_status():
    """Estado del job de enriquecimiento + conteos de la cola."""
    return JSONResponse(content=get_enrichment_status())


@app.get("/api/v1/odoo/notfound/count", tags=["Odoo"])
async def odoo_notfound_count():
    return JSONResponse(content={"count": get_notfound_count()})


@app.post("/api/v1/odoo/notfound/retry", tags=["Odoo"])
async def odoo_notfound_retry(
    older_than_hours: int = Query(12, ge=0),
    limit: int = Query(50000, ge=1, le=500000),
):
    """
    Mueve libros marcados notfound de vuelta a la cola para reintento.
    Util tras un periodo de ban/throttle — muchos notfound son falsos.

    older_than_hours: solo libros marcados notfound hace mas de X horas
                     (default 12, evita recuperar los que acabamos de descartar).
    limit: max filas a mover de un golpe (default 50k).
    """
    try:
        n = retry_notfound_books(older_than_hours=older_than_hours, limit=limit)
        return JSONResponse(content={"status": "ok", "moved_to_pending": n})
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "status": "error", "message": f"{type(e).__name__}: {e}"
        })


# ─── REST API — Odoo Mirror (jala todos los libros a Postgres) ───────

@app.post("/api/v1/odoo/mirror/start", tags=["Odoo"])
async def odoo_mirror_start(
    only_pending: bool = Query(True, description="True = solo sin description_sale (~727k). False = todos (~1M)."),
    batch_size: int = Query(1000, ge=100, le=5000),
):
    """
    Arranca un job en background que espeja product.template a la tabla
    local odoo_books_mirror. Idempotente — re-ejecutar actualiza los
    registros existentes.
    Tiempo estimado: 15-40 min dependiendo del alcance y la salud de Odoo.
    """
    import threading
    import sys
    import odoo_mirror

    if odoo_mirror.get_mirror_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Mirror job ya está corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                odoo_mirror.run_mirror_job(only_pending=only_pending, batch_size=batch_size)
            )
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={
        "status": "started",
        "only_pending": only_pending,
        "batch_size": batch_size,
    })


@app.post("/api/v1/odoo/mirror/stop", tags=["Odoo"])
async def odoo_mirror_stop():
    import odoo_mirror
    if odoo_mirror.stop_mirror_job():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "No hay mirror job corriendo."
    })


@app.get("/api/v1/odoo/mirror/status", tags=["Odoo"])
async def odoo_mirror_status():
    """Estado del job + cuantos libros tenemos espejados localmente."""
    import odoo_mirror
    return JSONResponse(content=odoo_mirror.get_mirror_status())


@app.post("/api/v1/odoo/mirror/suppliers-sync", tags=["Odoo"])
async def odoo_mirror_suppliers_sync(batch_size: int = Query(2000, ge=500, le=5000)):
    """
    Pulla product.supplierinfo + res.partner desde Odoo y espeja el vendor
    de cada libro en odoo_books_mirror.supplier_names. SOLO LECTURA en Odoo.
    Idempotente. Tarda ~5 min para ~200k libros con vendor cargado.
    """
    import threading
    import sys
    import odoo_mirror

    if odoo_mirror.get_suppliers_sync_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Sync de proveedores ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                odoo_mirror.sync_suppliers_from_odoo(batch_size=batch_size)
            )
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={"status": "started"})


@app.post("/api/v1/odoo/mirror/suppliers-sync-stop", tags=["Odoo"])
async def odoo_mirror_suppliers_sync_stop():
    import odoo_mirror
    if odoo_mirror.stop_suppliers_sync():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={"status": "error", "message": "No hay job corriendo."})


@app.get("/api/v1/odoo/mirror/suppliers-sync-status", tags=["Odoo"])
async def odoo_mirror_suppliers_sync_status():
    import odoo_mirror
    return JSONResponse(content=odoo_mirror.get_suppliers_sync_status())


@app.post("/api/v1/odoo/mirror/sync-categories", tags=["Odoo"])
async def odoo_mirror_sync_categories():
    """
    Pulla todas las product.public.category de Odoo y las cachea localmente.
    Tras esto, public_categ_ids -> public_categ_names se resuelven legibles.
    Si Odoo no tiene categorias asignadas a los libros, este endpoint no
    ayuda — usar /infer-categories en su lugar.
    """
    import odoo_mirror
    try:
        result = await odoo_mirror.sync_public_categories()
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "status": "error", "message": f"{type(e).__name__}: {e}"
        })


@app.post("/api/v1/odoo/mirror/infer-categories", tags=["Odoo"])
async def odoo_mirror_infer_categories():
    """
    Llena inferred_categories en odoo_books_mirror cruzando ISBN con las
    categorias scrapeadas de CDL (tabla books) y los XLSX de distribuidores
    (distributor_books). Prefiere books > distribuidores.
    Util cuando Odoo no tiene public_categ_ids asignados pero el scraper si.
    """
    import threading
    import sys
    import odoo_mirror

    if odoo_mirror.get_infer_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Inferencia ya esta corriendo."
        })

    def _run_in_thread():
        try:
            odoo_mirror.infer_categories_from_scraped()
        except Exception:
            pass

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={"status": "started"})


@app.get("/api/v1/odoo/mirror/infer-status", tags=["Odoo"])
async def odoo_mirror_infer_status():
    import odoo_mirror
    return JSONResponse(content=odoo_mirror.get_infer_status())


@app.post("/api/v1/odoo/categories/push", tags=["Odoo"])
async def odoo_categories_push():
    """
    Crea/encuentra en Odoo todas las product.category necesarias para
    las inferred_categories del mirror. Construye la jerarquia.
    Cachea cada path para idempotencia.
    """
    import threading
    import sys
    import odoo_mirror

    if odoo_mirror.get_push_categ_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Push de categorias ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(odoo_mirror.push_categories_to_odoo())
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={"status": "started"})


@app.post("/api/v1/odoo/categories/push-stop", tags=["Odoo"])
async def odoo_categories_push_stop():
    import odoo_mirror
    if odoo_mirror.stop_push_categories():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={"status": "error", "message": "No hay job corriendo."})


@app.get("/api/v1/odoo/categories/push-status", tags=["Odoo"])
async def odoo_categories_push_status():
    import odoo_mirror
    return JSONResponse(content=odoo_mirror.get_push_categ_status())


@app.post("/api/v1/odoo/categories/assign", tags=["Odoo"])
async def odoo_categories_assign(batch_size: int = Query(100, ge=10, le=500)):
    """
    Asigna product.template.categ_id a cada libro del mirror que tenga
    inferred_categories. Usa el cache local para resolver path -> categ_id.
    Requiere haber corrido /push antes.
    """
    import threading
    import sys
    import odoo_mirror

    if odoo_mirror.get_assign_categ_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Assign de categorias ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                odoo_mirror.assign_books_to_odoo_categories(batch_size=batch_size)
            )
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={"status": "started"})


@app.post("/api/v1/odoo/categories/assign-stop", tags=["Odoo"])
async def odoo_categories_assign_stop():
    import odoo_mirror
    if odoo_mirror.stop_assign_categories():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={"status": "error", "message": "No hay job corriendo."})


@app.get("/api/v1/odoo/categories/assign-status", tags=["Odoo"])
async def odoo_categories_assign_status():
    import odoo_mirror
    return JSONResponse(content=odoo_mirror.get_assign_categ_status())


@app.post("/api/v1/odoo/mirror/gbooks-fill", tags=["Odoo"])
async def odoo_mirror_gbooks_fill(
    concurrency: int = Query(15, ge=1, le=30),
    chunk_size: int = Query(1000, ge=100, le=5000),
):
    """
    Bulk fill desde Google Books — para cada libro del mirror sin
    gbooks_fetched_at, llama a la API y llena description, categorias
    y otros campos. Async puro, 100-1000x mas rapido que CDL.
    Tip: settea GOOGLE_BOOKS_API_KEY para subir limite a 100k req/dia.
    """
    import threading
    import sys
    import odoo_mirror

    if odoo_mirror.get_gbooks_fill_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Bulk fill ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                odoo_mirror.fill_from_google_books(
                    concurrency=concurrency, chunk_size=chunk_size,
                )
            )
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={"status": "started"})


@app.post("/api/v1/odoo/mirror/gbooks-fill-stop", tags=["Odoo"])
async def odoo_mirror_gbooks_fill_stop():
    import odoo_mirror
    if odoo_mirror.stop_gbooks_fill():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={"status": "error", "message": "No hay job corriendo."})


@app.get("/api/v1/odoo/mirror/gbooks-fill-status", tags=["Odoo"])
async def odoo_mirror_gbooks_fill_status():
    import odoo_mirror
    return JSONResponse(content=odoo_mirror.get_gbooks_fill_status())


@app.post("/api/v1/odoo/mirror/gbooks-reset-recent", tags=["Odoo"])
async def odoo_mirror_gbooks_reset_recent(
    hours_back: int = Query(24, ge=1, le=720,
                            description="Cuantas horas hacia atras buscar fetched_at sin data real"),
):
    """
    Resetea gbooks_fetched_at = NULL para libros marcados como fetched en
    las ultimas N horas que NO recibieron data real de Google Books.
    Util tras un episodio de rate limit donde se marcaron miles de
    libros como 'no match' falsamente.
    """
    import odoo_mirror
    try:
        n = odoo_mirror.reset_recent_gbooks_fetched(hours_back=hours_back)
        return JSONResponse(content={"reset": n, "hours_back": hours_back})
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "status": "error", "message": f"{type(e).__name__}: {e}"
        })


@app.post("/api/v1/odoo/mirror/cdl-fill", tags=["Odoo"])
async def odoo_mirror_cdl_fill(chunk_size: int = Query(500, ge=100, le=2000)):
    """
    Bulk scrape Casa del Libro para libros del mirror que estan en
    cdl_isbn_index. Usa proxies + Playwright (direct URL = fast).
    Guarda a books table y rellena inferred_categories + description
    en el mirror. Corre en PARALELO con gbooks-fill sin conflicto.
    """
    import threading
    import sys
    import odoo_mirror

    if odoo_mirror.get_cdl_fill_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "CDL bulk fill ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                odoo_mirror.fill_from_cdl_mirror(chunk_size=chunk_size)
            )
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={"status": "started"})


@app.post("/api/v1/odoo/mirror/cdl-fill-stop", tags=["Odoo"])
async def odoo_mirror_cdl_fill_stop():
    import odoo_mirror
    if odoo_mirror.stop_cdl_fill():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={"status": "error", "message": "No hay job corriendo."})


@app.get("/api/v1/odoo/mirror/cdl-fill-status", tags=["Odoo"])
async def odoo_mirror_cdl_fill_status():
    import odoo_mirror
    return JSONResponse(content=odoo_mirror.get_cdl_fill_status())


@app.post("/api/v1/odoo/mirror/cdl-search-fill", tags=["Odoo"])
async def odoo_mirror_cdl_search_fill(chunk_size: int = Query(200, ge=50, le=1000)):
    """
    Bulk scrape Casa del Libro buscando cada ISBN del mirror via search.
    NO requiere cdl_isbn_index — cubre TODOS los libros del mirror sin
    cdl_fetched_at. Mas lento por libro que /cdl-fill (sitemap) pero
    cubertura total. Corre en PARALELO con /cdl-fill y /gbooks-fill.
    """
    import threading
    import sys
    import odoo_mirror

    if odoo_mirror.get_cdl_search_fill_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "CDL search fill ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                odoo_mirror.fill_from_cdl_search(chunk_size=chunk_size)
            )
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={"status": "started"})


@app.post("/api/v1/odoo/mirror/cdl-search-fill-stop", tags=["Odoo"])
async def odoo_mirror_cdl_search_fill_stop():
    import odoo_mirror
    if odoo_mirror.stop_cdl_search_fill():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={"status": "error", "message": "No hay job corriendo."})


@app.get("/api/v1/odoo/mirror/cdl-search-fill-status", tags=["Odoo"])
async def odoo_mirror_cdl_search_fill_status():
    import odoo_mirror
    return JSONResponse(content=odoo_mirror.get_cdl_search_fill_status())


# ─── Admin: schema info + forzar migraciones sin redeploy ────────────

@app.get("/api/v1/admin/schema-info", tags=["Admin"])
async def admin_schema_info(table: str = Query("odoo_books_mirror")):
    """
    Lista las columnas reales de una tabla. Usa esto para confirmar que
    el ALTER TABLE corrio (ej: que existan cdl_author, cdl_image_url, etc.)
    """
    import db as dbmod
    conn = dbmod.get_connection()
    cur = conn.cursor()
    try:
        if dbmod.IS_POSTGRES:
            dbmod.execute_query(cur, """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = ?
                ORDER BY ordinal_position
            """, (table,))
            cols = [{"name": r[0], "type": r[1]} for r in cur.fetchall()]
        else:
            cur.execute(f"PRAGMA table_info({table})")
            cols = [{"name": r[1], "type": r[2]} for r in cur.fetchall()]
        return JSONResponse(content={"table": table, "columns": cols, "count": len(cols)})
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": f"{type(e).__name__}: {e}"
        })
    finally:
        conn.close()


@app.get("/api/v1/admin/odoo-modules", tags=["Admin"])
async def admin_odoo_modules():
    """
    Comprueba si modulos clave estan instalados en Odoo. Necesario para
    decidir si push de categorias va a `categ_id` (interna, siempre existe)
    o a `public_categ_ids` (e-commerce, requiere website_sale).
    """
    from odoo_client import OdooClient
    interesting = ["website_sale", "website", "stock", "sale", "purchase",
                   "product", "account"]
    try:
        async with OdooClient() as odoo:
            rows = await odoo.search_read(
                "ir.module.module",
                [["name", "in", interesting]],
                ["name", "state"],
            )
        installed = {r["name"]: r["state"] for r in rows}
        return JSONResponse(content={
            "modules": installed,
            "website_sale_installed": installed.get("website_sale") == "installed",
            "interpretation": (
                "website_sale instalado -> usar public_categ_ids para Shopify"
                if installed.get("website_sale") == "installed"
                else "website_sale NO instalado -> solo categ_id por ahora"
            ),
        })
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": f"{type(e).__name__}: {e}"
        })


@app.get("/api/v1/admin/throughput", tags=["Admin"])
async def admin_throughput():
    """
    Medidor HONESTO de velocidad basado en cdl_fetched_at / gbooks_fetched_at
    persistido en la BD. A diferencia de los counters del job (que se
    resetean con cada redeploy), esto cuenta filas reales con timestamp
    en los ultimos N minutos. Si lleva dias sin cambiar, hay un problema.
    """
    import db as dbmod
    out = {"windows": {}}
    conn = dbmod.get_connection()
    cur = conn.cursor()
    for minutes in (15, 60, 240, 1440):  # 15min, 1h, 4h, 24h
        ranges = {}
        try:
            dbmod.execute_query(cur,
                f"SELECT COUNT(*) FROM odoo_books_mirror "
                f"WHERE cdl_fetched_at >= NOW() - INTERVAL '{minutes} minutes'")
            ranges["cdl_fetched"] = cur.fetchone()[0]
        except Exception:
            try: conn.rollback()
            except Exception: pass
            ranges["cdl_fetched"] = None
        try:
            dbmod.execute_query(cur,
                f"SELECT COUNT(*) FROM odoo_books_mirror "
                f"WHERE gbooks_fetched_at >= NOW() - INTERVAL '{minutes} minutes'")
            ranges["gbooks_fetched"] = cur.fetchone()[0]
        except Exception:
            try: conn.rollback()
            except Exception: pass
            ranges["gbooks_fetched"] = None
        if ranges["cdl_fetched"]:
            ranges["cdl_per_min"] = round(ranges["cdl_fetched"] / minutes, 2)
        else:
            ranges["cdl_per_min"] = 0
        if ranges["gbooks_fetched"]:
            ranges["gbooks_per_min"] = round(ranges["gbooks_fetched"] / minutes, 2)
        else:
            ranges["gbooks_per_min"] = 0
        out["windows"][f"{minutes}min"] = ranges
    conn.close()
    return JSONResponse(content=out)


@app.get("/api/v1/admin/diagnose", tags=["Admin"])
async def admin_diagnose():
    """
    Diagnostico completo: shard config + counts reales por filtro.
    Util cuando "Target: 0" — te dice exactamente por que no encuentra libros.
    """
    import db as dbmod
    import odoo_mirror as om
    conn = dbmod.get_connection()
    cur = conn.cursor()
    out = {
        "shard": {
            "index": om.WORKER_SHARD_INDEX,
            "count": om.WORKER_SHARD_COUNT,
            "clause": om._shard_clause(),
        },
        "counts": {},
        "errors": [],
    }
    queries = [
        ("total_mirror", "SELECT COUNT(*) FROM odoo_books_mirror"),
        ("total_mirror_shard",
            f"SELECT COUNT(*) FROM odoo_books_mirror WHERE {om._shard_clause()}"),
        ("with_barcode_shard",
            f"SELECT COUNT(*) FROM odoo_books_mirror WHERE barcode IS NOT NULL AND barcode <> '' AND {om._shard_clause()}"),
        ("without_categ_shard",
            f"SELECT COUNT(*) FROM odoo_books_mirror "
            f"WHERE barcode IS NOT NULL AND barcode <> '' "
            f"AND (inferred_categories IS NULL OR inferred_categories = '') "
            f"AND {om._shard_clause()}"),
        ("not_cdl_fetched_shard",
            f"SELECT COUNT(*) FROM odoo_books_mirror "
            f"WHERE barcode IS NOT NULL AND barcode <> '' "
            f"AND cdl_fetched_at IS NULL "
            f"AND {om._shard_clause()}"),
        ("cdl_search_target_shard",
            f"SELECT COUNT(*) FROM odoo_books_mirror "
            f"WHERE barcode IS NOT NULL AND barcode <> '' "
            f"AND (inferred_categories IS NULL OR inferred_categories = '') "
            f"AND cdl_fetched_at IS NULL "
            f"AND {om._shard_clause()}"),
        ("cdl_sitemap_target_shard",
            f"SELECT COUNT(*) FROM odoo_books_mirror m "
            f"INNER JOIN cdl_isbn_index ci ON m.barcode = ci.isbn "
            f"WHERE m.barcode IS NOT NULL AND m.barcode <> '' "
            f"AND (m.inferred_categories IS NULL OR m.inferred_categories = '') "
            f"AND m.cdl_fetched_at IS NULL "
            f"AND {om._shard_clause('m.odoo_id')}"),
        ("gbooks_target_shard",
            f"SELECT COUNT(*) FROM odoo_books_mirror "
            f"WHERE barcode IS NOT NULL AND barcode <> '' "
            f"AND gbooks_fetched_at IS NULL "
            f"AND {om._shard_clause()}"),
        ("isbn_index_total", "SELECT COUNT(*) FROM cdl_isbn_index"),
        # ── Proveedores espejados desde Odoo (product.supplierinfo) ──
        ("with_supplier_odoo",
            "SELECT COUNT(*) FROM odoo_books_mirror "
            "WHERE supplier_count IS NOT NULL AND supplier_count > 0"),
        # ── Stats de precios desde Odoo (list_price) ──
        # CAST a FLOAT obligatorio: psycopg2 devuelve Decimal y JSONResponse
        # no sabe serializarlo -> 500 'Internal Server Error'.
        ("price_from_odoo_any",
            "SELECT COUNT(*) FROM odoo_books_mirror WHERE list_price IS NOT NULL"),
        ("price_from_odoo_nonzero",
            "SELECT COUNT(*) FROM odoo_books_mirror WHERE list_price > 0"),
        ("price_avg_odoo",
            "SELECT CAST(ROUND(AVG(list_price)::numeric, 2) AS FLOAT) FROM odoo_books_mirror WHERE list_price > 0"),
        ("price_min_odoo",
            "SELECT CAST(MIN(list_price) AS FLOAT) FROM odoo_books_mirror WHERE list_price > 0"),
        ("price_max_odoo",
            "SELECT CAST(MAX(list_price) AS FLOAT) FROM odoo_books_mirror WHERE list_price > 0"),
    ]
    from decimal import Decimal
    for key, q in queries:
        try:
            dbmod.execute_query(cur, q)
            v = cur.fetchone()[0]
            # Decimal -> float para que JSONResponse pueda serializar
            if isinstance(v, Decimal):
                v = float(v)
            out["counts"][key] = v
        except Exception as e:
            out["counts"][key] = None
            out["errors"].append(f"{key}: {type(e).__name__}: {str(e)[:200]}")
            try: conn.rollback()
            except Exception: pass
    conn.close()
    return JSONResponse(content=out)


@app.post("/api/v1/admin/run-migrations", tags=["Admin"])
async def admin_run_migrations():
    """
    Re-ejecuta init_db() para aplicar todos los ALTER TABLE / CREATE TABLE
    pendientes. Idempotente — si ya estan aplicados, no hace nada. Sin
    redeploy. Devuelve la lista de columnas resultante de odoo_books_mirror.
    """
    from scraper import init_db
    import db as dbmod
    try:
        init_db()
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": f"init_db() FAILED: {type(e).__name__}: {e}"
        })

    # Verificar que las columnas criticas existen
    conn = dbmod.get_connection()
    cur = conn.cursor()
    try:
        if dbmod.IS_POSTGRES:
            dbmod.execute_query(cur, """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'odoo_books_mirror'
                ORDER BY ordinal_position
            """)
            cols = [r[0] for r in cur.fetchall()]
        else:
            cur.execute("PRAGMA table_info(odoo_books_mirror)")
            cols = [r[1] for r in cur.fetchall()]
        expected_cdl = [
            "cdl_author", "cdl_editorial", "cdl_image_url",
            "cdl_weight", "cdl_height", "cdl_width", "cdl_binding",
            "cdl_translator", "cdl_illustrator", "cdl_collection",
            "cdl_pages", "cdl_release_date", "cdl_url", "cdl_price",
            "cdl_language",
        ]
        missing = [c for c in expected_cdl if c not in cols]
        return JSONResponse(content={
            "status": "ok",
            "total_columns": len(cols),
            "cdl_columns_missing": missing,
            "cdl_columns_present": [c for c in expected_cdl if c in cols],
            "columns": cols,
        })
    finally:
        conn.close()


@app.get("/api/v1/odoo/mirror/export.csv", tags=["Odoo"])
async def odoo_mirror_export_csv(
    only_with_categories: bool = Query(
        False,
        description="Si True, solo libros con categoria. Default: todos.",
    ),
):
    """
    Descarga TODO el catalogo como CSV. JOIN automatico con books (CDL),
    distributor_books (XLSX) y datos de Google Books para sacar los
    campos mas ricos por libro (autor, peso, altura, traductor, etc.).
    Streamea sin cargar todo a memoria.
    """
    import odoo_mirror
    from fastapi.responses import StreamingResponse
    fname = "libros_completo.csv" if not only_with_categories else "libros_con_categorias.csv"
    headers = {
        "Content-Disposition": f'attachment; filename="{fname}"'
    }
    # text/csv con charset utf-8 + BOM en el body -> Excel ES abre como UTF-8
    # con ';' como separador, sin tildes rotas (Ã©)
    return StreamingResponse(
        odoo_mirror.export_csv_streaming(only_with_categories=only_with_categories),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )


# ─── REST API — Import desde distribuidores (Excel -> Postgres) ──────

@app.post("/api/v1/distributors/import", tags=["Distribuidores"])
async def distributors_import(
    file: UploadFile = File(..., description="XLSX de un distribuidor (ANAYA, PLANETA, PODIPRINT...)"),
    fuente: str = Query(None, description="Etiqueta de distribuidor. Si vacio, se adivina del nombre del archivo."),
    batch_size: int = Query(500, ge=100, le=2000),
):
    """
    Sube un Excel del catalogo de un distribuidor y lo upserta a la tabla
    `distributor_books` (PK por ISBN). Si el ISBN ya existe, se actualizan
    los campos con los del XLSX nuevo. Idempotente: re-subir el mismo
    archivo no duplica.
    """
    import distributor_import
    import threading
    import sys

    if distributor_import.get_import_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Otro import esta corriendo."
        })

    content = await file.read()
    fuente_hint = fuente or distributor_import._guess_fuente_from_path(file.filename or "")

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        try:
            distributor_import.import_xlsx_bytes(
                content, fuente_hint=fuente_hint, batch_size=batch_size
            )
        except Exception:
            pass

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={
        "status": "started",
        "filename": file.filename,
        "fuente": fuente_hint,
        "size_bytes": len(content),
    })


@app.get("/api/v1/distributors/status", tags=["Distribuidores"])
async def distributors_status():
    """Estado del ultimo import + total de filas en distributor_books."""
    import distributor_import
    return JSONResponse(content=distributor_import.get_import_status())


@app.get("/api/v1/distributors/stats", tags=["Distribuidores"])
async def distributors_stats():
    """Conteo por fuente + cross-stats vs odoo_books_mirror."""
    import distributor_import
    from db import get_connection, execute_query, IS_POSTGRES

    by_source = distributor_import.count_by_source()
    total = sum(s["count"] for s in by_source)

    # Cuantos de los distributor_books estan tambien en Odoo (matching ISBN/barcode)
    overlap = 0
    only_dist = 0
    try:
        conn = get_connection()
        cur = conn.cursor()
        execute_query(cur, """
            SELECT COUNT(*) FROM distributor_books d
            WHERE EXISTS (
                SELECT 1 FROM odoo_books_mirror m WHERE m.barcode = d.isbn
            )
        """)
        overlap = cur.fetchone()[0]
        execute_query(cur, """
            SELECT COUNT(*) FROM distributor_books d
            WHERE NOT EXISTS (
                SELECT 1 FROM odoo_books_mirror m WHERE m.barcode = d.isbn
            )
        """)
        only_dist = cur.fetchone()[0]
        conn.close()
    except Exception:
        pass

    return JSONResponse(content={
        "total_distributor_books": total,
        "by_source": by_source,
        "overlap_with_odoo": overlap,
        "only_in_distributors_not_in_odoo": only_dist,
    })


# ─── REST API — CDL ISBN Index (sitemap-based fast path) ─────────────

@app.post("/api/v1/cdl/build-isbn-index", tags=["Odoo"])
async def cdl_build_isbn_index():
    """
    Lee el sitemap-cdl-libros-tematicas y popula la tabla cdl_isbn_index.
    Job en background. Una vez completado, el scraper de Odoo puede usar
    direct-URL en vez de search→click para libros conocidos.
    """
    import threading
    import sys

    if get_isbn_index_job_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "ISBN index job ya está corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(run_build_isbn_index_job())
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    await asyncio.sleep(1)
    return JSONResponse(content={"status": "started"})


@app.get("/api/v1/cdl/isbn-index/status", tags=["Odoo"])
async def cdl_isbn_index_status():
    """Estado del job + cuantos ISBNs hay indexados en BD."""
    job = get_isbn_index_job_status()
    job["total_indexed"] = get_isbn_index_count()
    return JSONResponse(content=job)


# ─── REST API — Telegram notifications ────────────────────────────────

@app.post("/api/v1/notify/test", tags=["Notificaciones"])
async def notify_test():
    """
    Manda un mensaje de prueba al Telegram configurado para verificar que
    TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID son correctos.
    """
    import notify as nfy
    if not nfy.is_configured():
        return JSONResponse(status_code=400, content={
            "status": "error",
            "message": "TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID no estan configurados.",
            "worker": os.environ.get("WORKER_NAME", "default"),
        })
    ok = await nfy.send_telegram(
        "✅ *Test de notificacion*\nSi ves esto en tu celular, el bot esta listo."
    )
    if ok:
        return JSONResponse(content={"status": "sent", "worker": os.environ.get("WORKER_NAME", "default")})
    return JSONResponse(status_code=502, content={
        "status": "error",
        "message": "No se pudo entregar el mensaje. Revisa el token y el chat_id."
    })


@app.get("/api/v1/notify/status", tags=["Notificaciones"])
async def notify_status():
    """¿Esta configurado el Telegram en este server?"""
    import notify as nfy
    return JSONResponse(content={
        "configured": nfy.is_configured(),
        "worker": os.environ.get("WORKER_NAME", "default"),
    })


@app.get("/api/v1/proxies/status", tags=["Diagnostico"])
async def proxies_status():
    """
    Lista los proxies cargados desde la env var PROXY_POOL al arrancar.
    Si esta vacio, el server saldra con su IP directa hacia CDL.
    Incluye shard info — util para multi-worker (Server A + Server B).
    """
    from scraper import PROXY_POOL, PROXY_URL, parse_proxy
    import odoo_mirror
    parsed = []
    for spec in PROXY_POOL:
        p = parse_proxy(spec)
        if p:
            parsed.append({"server": p["server"], "auth": bool(p.get("username"))})
        else:
            parsed.append({"server": None, "raw": spec[:30], "error": "invalid format"})
    return JSONResponse(content={
        "worker": os.environ.get("WORKER_NAME", "default"),
        "shard_index": odoo_mirror.WORKER_SHARD_INDEX,
        "shard_count": odoo_mirror.WORKER_SHARD_COUNT,
        "shard_clause": odoo_mirror._shard_clause(),
        "proxy_pool_count": len(PROXY_POOL),
        "proxy_pool_valid": sum(1 for p in parsed if p.get("server")),
        "proxies": parsed,
        "proxy_url_legacy": bool(PROXY_URL),
    })


@app.get("/api/v1/proxies/health", tags=["Diagnostico"])
async def proxies_health():
    """
    Estado del tracker de health por proxy. Muestra fallos consecutivos,
    totales, si esta marcada muerta, y desde cuando.
    Los browsers nuevos saltan automaticamente las proxies muertas y los
    jobs caen a IP directa si TODAS mueren.
    """
    import proxy_health as ph
    return JSONResponse(content={
        "dead_threshold": ph.DEAD_THRESHOLD,
        "proxies": ph.snapshot(),
    })


@app.post("/api/v1/proxies/health/reset", tags=["Diagnostico"])
async def proxies_health_reset():
    """Resetea TODAS las proxies a vivas. Util tras un episodio de bloqueo
    temporal de CDL (cuando Webshare las habilita de nuevo)."""
    import proxy_health as ph
    ph.reset_all()
    return JSONResponse(content={"status": "reset_ok"})


@app.post("/api/v1/proxies/healthcheck", tags=["Diagnostico"])
async def proxies_healthcheck():
    """
    Prueba cada proxy contactando https://api.ipify.org para ver que IP
    aparece desde el lado del destino. Si la IP devuelta es la del proxy,
    funciona; si timeout o ConnectionError, el proxy esta muerto.
    Util tras un redeploy para detectar proxies bloqueados antes de
    arrancar un job.
    """
    import aiohttp
    from scraper import PROXY_POOL, parse_proxy

    if not PROXY_POOL:
        return JSONResponse(content={"error": "PROXY_POOL vacio", "results": []})

    async def _check_one(spec: str):
        proxy = parse_proxy(spec)
        if not proxy:
            return {"spec": spec[:30], "ok": False, "error": "invalid format"}
        proxy_url = proxy["server"]
        if proxy.get("username"):
            host = proxy_url.replace("http://", "")
            proxy_url = f"http://{proxy['username']}:{proxy['password']}@{host}"
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get("https://api.ipify.org?format=json", proxy=proxy_url) as resp:
                    body = await resp.json()
                    return {"spec": proxy["server"], "ok": True, "exit_ip": body.get("ip")}
        except Exception as e:
            return {"spec": proxy["server"], "ok": False, "error": f"{type(e).__name__}: {str(e)[:100]}"}

    results = await asyncio.gather(*(_check_one(s) for s in PROXY_POOL))
    ok_count = sum(1 for r in results if r.get("ok"))
    return JSONResponse(content={
        "checked": len(results),
        "alive": ok_count,
        "dead": len(results) - ok_count,
        "results": results,
    })


@app.get("/api/v1/gbooks/lookup", tags=["Diagnostico"])
async def gbooks_lookup(isbn: str = Query(..., description="ISBN-10 o ISBN-13", min_length=10)):
    """
    Prueba directa a Google Books con un ISBN. Devuelve los datos normalizados
    que el enricher usaria para ese libro, sin escribir nada a Odoo.
    Util para verificar que la cascada funciona antes de arrancar el job.
    """
    import google_books
    try:
        data = await google_books.fetch_by_isbn_with_session(isbn)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {e}"})
    if not data:
        return JSONResponse(status_code=404, content={"isbn": isbn, "found": False})
    return JSONResponse(content={"isbn": isbn, "found": True, "data": data})


@app.post("/api/v1/notify/telegram-webhook", tags=["Notificaciones"])
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(None),
):
    """
    Endpoint publico que recibe updates de Telegram. Telegram llama aqui
    cuando alguien manda un mensaje al bot. Procesa comandos como /status.

    Seguridad:
      1. Si TELEGRAM_WEBHOOK_SECRET esta seteado, verifica el header.
      2. handle_command() verifica que el chat_id sea el autorizado.
    """
    expected_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
        return JSONResponse(status_code=403, content={"ok": False, "error": "bad secret"})

    try:
        update = await request.json()
    except Exception:
        return JSONResponse(content={"ok": True})

    message = update.get("message") or update.get("edited_message")
    if not message:
        return JSONResponse(content={"ok": True})

    import notify as nfy
    reply = await nfy.handle_command(message)
    if reply:
        await nfy.send_telegram(reply)
    return JSONResponse(content={"ok": True})


@app.post("/api/v1/notify/setup-webhook", tags=["Notificaciones"])
async def notify_setup_webhook(request: Request):
    """
    Registra este server como webhook target del bot de Telegram.
    Lo llamas UNA VEZ, manualmente, desde el server que quieres que
    responda los comandos. Telegram solo guarda una URL — si lo llamas
    desde otro server despues, queda apuntando ahi.

    Telegram exige HTTPS. Forzamos el esquema https en la URL armada desde
    el Host header (el dominio publico). Si por alguna razon queres
    overridear (ej. dominio custom), seteas WEBHOOK_BASE_URL.
    """
    import notify as nfy
    if not nfy.TELEGRAM_BOT_TOKEN:
        return JSONResponse(status_code=400, content={
            "status": "error", "message": "TELEGRAM_BOT_TOKEN no configurado en este server"
        })

    override = os.environ.get("WEBHOOK_BASE_URL", "").rstrip("/")
    if override:
        base = override if override.startswith("http") else f"https://{override}"
    else:
        # Detras de Traefik/nginx el scheme de request.url es http; usamos el
        # Host header (lo que el cliente ve) y forzamos https.
        host = request.headers.get("host") or request.url.netloc
        base = f"https://{host}"
    webhook_url = f"{base}/api/v1/notify/telegram-webhook"
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    result = await nfy.register_webhook(webhook_url, secret=secret)
    return JSONResponse(content={
        "webhook_url": webhook_url,
        "telegram_response": result,
        "worker": os.environ.get("WORKER_NAME", "default"),
    })


@app.post("/api/v1/notify/delete-webhook", tags=["Notificaciones"])
async def notify_delete_webhook():
    """Quita el webhook (apaga los comandos sin tocar el token)."""
    import notify as nfy
    result = await nfy.delete_webhook()
    return JSONResponse(content={"telegram_response": result})


@app.get("/api/v1/odoo/notfound/export", tags=["Odoo"])
async def odoo_notfound_export(background_tasks: BackgroundTasks):
    """Exporta los libros no encontrados en CDL a Excel."""
    import uuid as _uuid
    file_path = f"notfound_{_uuid.uuid4().hex[:8]}.xlsx"
    try:
        conn = dbmod.get_connection()
        df = pd.read_sql_query(
            "SELECT odoo_id, barcode, name, reason, attempts, "
            "first_seen, last_attempt FROM notfound_books ORDER BY last_attempt DESC",
            conn,
        )
        conn.close()
        df.to_excel(file_path, index=False)
        background_tasks.add_task(
            lambda: os.remove(file_path) if os.path.exists(file_path) else None
        )
        return FileResponse(
            path=file_path,
            filename="Libros_No_Encontrados.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "status": "error", "message": f"Error exportando Excel: {e}"
        })


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
