import sys
import asyncio
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, Request, Form, Query, BackgroundTasks, Depends, HTTPException, Header, UploadFile, File, status
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
# Las filas del catalogo publicable llevan fechas y Decimal, que JSONResponse
# no sabe serializar por su cuenta.
from fastapi.encoders import jsonable_encoder
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
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

# Paths que son publicos (sin auth). El webhook de Telegram va aqui porque
# Telegram no manda credenciales; /login y /logout para poder autenticarse.
_PUBLIC_PATHS = {
    "/api/v1/notify/telegram-webhook",
    "/login",
    "/logout",
}


def _basic_ok(credentials: HTTPBasicCredentials | None) -> bool:
    if not credentials:
        return False
    u = secrets.compare_digest(credentials.username.encode("utf8"),
                               APP_USERNAME.encode("utf8"))
    p = secrets.compare_digest(credentials.password.encode("utf8"),
                               APP_PASSWORD.encode("utf8"))
    return bool(u and p)


def verify_credentials(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
):
    if request.url.path in _PUBLIC_PATHS:
        return "public"
    # 1) Sesion de navegador (login web)
    try:
        user = request.session.get("user")
    except Exception:
        user = None
    if user:
        return user
    # 2) HTTP Basic (n8n / API) — sin cambios, para no romper integraciones
    if _basic_ok(credentials):
        return credentials.username
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or incorrect credentials",
        headers={"WWW-Authenticate": "Basic"},
    )

app = FastAPI(
    title="Kalamo — Panel de Operaciones",
    description="Panel de sincronización de stock y catálogo de Kalamo.",
    version="2.0.0",
    dependencies=[Depends(verify_credentials)],
)

# Sesion firmada para el login web (cookie). El Basic de /api sigue intacto.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", APP_PASSWORD + "-kalamo-session"),
    max_age=60 * 60 * 12,  # 12h
    same_site="lax",
    https_only=False,
)


@app.exception_handler(StarletteHTTPException)
async def _auth_redirect(request: Request, exc: StarletteHTTPException):
    """401 en navegacion HTML -> redirige al login (en vez del popup feo).
    En /api mantiene el 401 JSON para que n8n reciba el error normal."""
    if exc.status_code == 401:
        accept = request.headers.get("accept", "")
        if "text/html" in accept and not request.url.path.startswith("/api"):
            return RedirectResponse("/login", status_code=302)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None) or {},
    )

# Setup templates for the web interface
templates = Jinja2Templates(directory="templates")


@app.middleware("http")
async def api_request_logger(request: Request, call_next):
    """
    Registra en api_request_log cada POST a /api/ (las ACCIONES: syncs,
    pushes, CEGALD, crones...). Los GET de polling no se registran (ruido).
    Visible en la tab Auditoria -> Peticiones API.
    """
    import time as _time
    is_action = request.method in ("POST", "DELETE", "PUT") and \
        request.url.path.startswith("/api/")
    body_snippet = ""
    if is_action:
        try:
            body_bytes = await request.body()  # Starlette lo cachea para el handler
            if body_bytes:
                body_snippet = body_bytes[:2000].decode("utf-8", errors="replace")
        except Exception:
            pass
    t0 = _time.monotonic()
    response = await call_next(request)
    if is_action:
        try:
            import base64
            import audit_log
            username = ""
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("basic "):
                try:
                    username = base64.b64decode(auth[6:]).decode(
                        "utf-8", errors="replace").split(":", 1)[0]
                except Exception:
                    pass
            client_ip = request.headers.get("x-forwarded-for",
                                            request.client.host if request.client else "")
            client_ip = client_ip.split(",")[0].strip()
            audit_log.log_api_request(
                method=request.method,
                path=request.url.path,
                query=str(request.url.query or ""),
                body=body_snippet,
                status_code=response.status_code,
                duration_ms=int((_time.monotonic() - t0) * 1000),
                client_ip=client_ip,
                username=username,
                user_agent=request.headers.get("user-agent", ""),
            )
        except Exception as e:
            print(f"[ApiLog] middleware FAIL: {e}")
    return response


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

    # Columnas de pausa de proveedores (idempotente)
    try:
        import proveedores_admin
        proveedores_admin.ensure_schema()
    except Exception as e:
        print(f"[Startup] proveedores ensure_schema FALLO: {type(e).__name__}: {e}")

    # Auto-arrancar el cron de stock AZETA si la env var lo pide
    if os.environ.get("AZETA_STOCK_CRON_ENABLED", "").lower() in ("1", "true", "yes"):
        try:
            import azeta_push_odoo
            if azeta_push_odoo.start_stock_cron():
                print(f"[Startup] AZETA stock cron AUTO-ARRANCADO "
                      f"(intervalo {azeta_push_odoo.CRON_INTERVAL_S}s)")
            else:
                print("[Startup] AZETA stock cron NO arrancado (ya activo)")
        except Exception as e:
            print(f"[Startup] AZETA stock cron FALLO: {type(e).__name__}: {e}")

    # Auto-arrancar el cron del sync SINLI si la env var lo pide
    if os.environ.get("SYNC_STOCK_CRON_ENABLED", "").lower() in ("1", "true", "yes"):
        try:
            import sync_stock_sinli
            if sync_stock_sinli.start_cron():
                print(f"[Startup] SINLI sync cron AUTO-ARRANCADO "
                      f"(intervalo {sync_stock_sinli.CRON_INTERVAL_S}s)")
            else:
                print("[Startup] SINLI sync cron NO arrancado (ya activo)")
        except Exception as e:
            print(f"[Startup] SINLI sync cron FALLO: {type(e).__name__}: {e}")

    # Auto-arrancar el refresco del catalogo publicable (lo lee el feed de
    # marketplace). Sin esto publica la foto del ultimo refresco manual: se
    # quedo tres dias parado del 21 al 24 de agosto.
    if os.environ.get("CATALOGO_CRON_ENABLED", "").lower() in ("1", "true", "yes"):
        try:
            import catalogo_publicable
            if catalogo_publicable.start_cron():
                print(f"[Startup] Catalogo publicable cron AUTO-ARRANCADO "
                      f"(cada {catalogo_publicable.CRON_INTERVAL_S}s)")
            else:
                print("[Startup] Catalogo publicable cron NO arrancado (ya activo)")
        except Exception as e:
            print(f"[Startup] Catalogo publicable cron FALLO: {type(e).__name__}: {e}")

    # Auto-arrancar la sincronizacion de stock a Shopify
    if os.environ.get("SHOPIFY_STOCK_CRON_ENABLED", "").lower() in ("1", "true", "yes"):
        try:
            import shopify_stock
            if shopify_stock.start_cron():
                print(f"[Startup] Shopify stock cron AUTO-ARRANCADO "
                      f"(cada {shopify_stock.CRON_INTERVAL_S}s, completa a las "
                      f"{shopify_stock.HORA_COMPLETA}:00)")
            else:
                print("[Startup] Shopify stock cron NO arrancado (ya activo)")
        except Exception as e:
            print(f"[Startup] Shopify stock cron FALLO: {type(e).__name__}: {e}")

    # Auto-arrancar el vigilante de salud
    if os.environ.get("VIGILANTE_ENABLED", "").lower() in ("1", "true", "yes"):
        try:
            import vigilante
            if vigilante.start_cron():
                print(f"[Startup] Vigilante AUTO-ARRANCADO "
                      f"(cada {vigilante.INTERVALO_S}s)")
        except Exception as e:
            print(f"[Startup] Vigilante FALLO: {type(e).__name__}: {e}")

    # Auto-arrancar el ciclo diario de libros nuevos (auto-scrape)
    if os.environ.get("AUTO_SCRAPE_CRON_ENABLED", "").lower() in ("1", "true", "yes"):
        try:
            import auto_scrape
            if auto_scrape.start_cron():
                print(f"[Startup] Auto-scrape cron AUTO-ARRANCADO "
                      f"(intervalo {auto_scrape.CRON_INTERVAL_S}s)")
            else:
                print("[Startup] Auto-scrape cron NO arrancado (ya activo)")
        except Exception as e:
            print(f"[Startup] Auto-scrape cron FALLO: {type(e).__name__}: {e}")


# ─── Web Interface (HTML) ────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    """Pantalla de login (publica). Si ya hay sesion, va al panel."""
    if request.session.get("user"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_submit(request: Request,
                       username: str = Form(...), password: str = Form(...)):
    """Valida credenciales y abre sesion de navegador."""
    u = secrets.compare_digest(username.encode("utf8"), APP_USERNAME.encode("utf8"))
    p = secrets.compare_digest(password.encode("utf8"), APP_PASSWORD.encode("utf8"))
    if u and p:
        request.session["user"] = username
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request, "login.html",
        {"error": "Usuario o contraseña incorrectos."}, status_code=401)


@app.get("/logout", include_in_schema=False)
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


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


@app.post("/api/v1/odoo/mirror/cdl-http-fill", tags=["Odoo"])
async def odoo_mirror_cdl_http_fill(
    concurrency: int = Query(20, ge=5, le=50),
    chunk_size: int = Query(500, ge=100, le=2000),
):
    """
    Bulk scrape CDL via aiohttp+BeautifulSoup (sin browser, 60-80x mas rapido).
    Mantiene TODOS los campos: peso, alto, ancho, encuadernacion, traductor,
    ilustrador, coleccion, descripcion, categorias, autor, editorial, paginas,
    idioma, fecha, ISBN, imagen.

    Throughput tipico: ~2000 libros/min con concurrency 20.
    Auto-throttle: si CDL devuelve 429/403, se detiene tras 3 hits.
    """
    import threading
    import sys
    import odoo_mirror

    if odoo_mirror.get_cdl_http_fill_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "CDL HTTP fill ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                odoo_mirror.fill_from_cdl_http(
                    concurrency=concurrency, chunk_size=chunk_size,
                )
            )
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={"status": "started", "concurrency": concurrency})


@app.post("/api/v1/odoo/mirror/cdl-http-fill-stop", tags=["Odoo"])
async def odoo_mirror_cdl_http_fill_stop():
    import odoo_mirror
    if odoo_mirror.stop_cdl_http_fill():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={"status": "error", "message": "No hay job corriendo."})


@app.get("/api/v1/odoo/mirror/cdl-http-fill-status", tags=["Odoo"])
async def odoo_mirror_cdl_http_fill_status():
    import odoo_mirror
    return JSONResponse(content=odoo_mirror.get_cdl_http_fill_status())


# ─── REST API — AZETA Stock Sync ─────────────────────────────────────
#
# AZETA expone su stock por HTTP CSV (no por SINLI email como los otros 11
# proveedores). Este fetcher descarga el CSV y popula libros_proveedor para
# que el sync SINLI -> Odoo (otro Claude) lo recoja y lo lleve a Odoo AZE01.
# NO escribimos stock.quant en Odoo desde aqui (eso es del sync por contrato).

@app.post("/api/v1/azeta/stock-sync", tags=["AZETA"])
async def azeta_stock_sync(batch_size: int = Query(500, ge=100, le=2000)):
    """
    Descarga el CSV de stock de AZETA y popula libros_proveedor.
    Idempotente — el patron IS DISTINCT FROM solo mueve actualizado_en si
    el stock cambio realmente.
    """
    import threading
    import sys
    import azeta_stock

    if azeta_stock.get_azeta_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "AZETA sync ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                azeta_stock.run_azeta_sync(batch_size=batch_size)
            )
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={"status": "started"})


@app.post("/api/v1/azeta/stock-sync-stop", tags=["AZETA"])
async def azeta_stock_sync_stop():
    import azeta_stock
    if azeta_stock.stop_azeta_sync():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "No hay job corriendo."
    })


@app.get("/api/v1/azeta/stock-status", tags=["AZETA"])
async def azeta_stock_status():
    """Estado del job + stats persistentes (cuantos libros AZETA tienen stock)."""
    import azeta_stock
    return JSONResponse(content=azeta_stock.get_azeta_status())


@app.post("/api/v1/azeta/catalog-sync", tags=["AZETA"])
async def azeta_catalog_sync(batch_size: int = Query(500, ge=100, le=2000)):
    """
    Descarga el CATALOGO completo de AZETA (~1M libros, ZIP 200MB) y carga
    al odoo_books_mirror todos los campos descriptivos: titulo, autor,
    editorial, precio EUR, peso, dimensiones, encuadernacion, categorias,
    descripcion, portada, idioma, fecha edicion.

    Solo actualiza libros que YA estan en el mirror (no crea nuevos).
    Reusa columnas cdl_* con inferred_source='azeta_catalog'.
    El precio EUR va a azeta_price_eur (no cdl_price que es text/COP).

    NO push a Odoo desde aqui — eso sera la Fase 2 cuando el mirror este
    confirmado. Tarda ~5-15 min completo.
    """
    import threading
    import sys
    import azeta_catalog

    if azeta_catalog.get_catalog_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "AZETA catalog sync ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                azeta_catalog.run_catalog_sync(batch_size=batch_size)
            )
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={"status": "started"})


@app.post("/api/v1/azeta/catalog-sync-stop", tags=["AZETA"])
async def azeta_catalog_sync_stop():
    import azeta_catalog
    if azeta_catalog.stop_catalog_sync():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "No hay job corriendo."
    })


@app.get("/api/v1/azeta/catalog-status", tags=["AZETA"])
async def azeta_catalog_status():
    import azeta_catalog
    return JSONResponse(content=azeta_catalog.get_catalog_status())


# ─── Fase 2: push de datos AZETA del mirror a Odoo ─────────────────

@app.post("/api/v1/azeta/push-to-odoo", tags=["AZETA"])
async def azeta_push_to_odoo(
    test_isbn: str | None = Query(None, description="ISBN único para validar (modo test)"),
    max_books: int | None = Query(None, ge=1, description="Tope de libros (None = todos)"),
    batch_size: int = Query(200, ge=10, le=1000),
):
    """
    Fase 2 AZETA. Lee odoo_books_mirror WHERE azeta_fetched_at IS NOT NULL
    y escribe en Odoo:
      - product.template: description, weight (kg), list_price (EUR)
      - product.template.categ_id (desde cache de odoo_product_categories_cache)
      - stock.quant en AZE01 (location 14, cap 50, stock=0 NO borra)

    test_isbn: procesa solo ese ISBN — útil para validar con 1 libro antes
    de soltar 1M. max_books: tope total. Sin args = push completo de todos
    los libros AZETA enriquecidos.

    Requiere: cache de categorías (push_categories_to_odoo + assign) corrido.
    """
    import threading
    import sys
    import azeta_push_odoo

    if azeta_push_odoo.get_push_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "AZETA push ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                azeta_push_odoo.run_azeta_push(
                    batch_size=batch_size,
                    test_isbn=test_isbn,
                    max_books=max_books,
                )
            )
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={"status": "started", "test_isbn": test_isbn,
                                  "max_books": max_books})


@app.post("/api/v1/azeta/push-to-odoo-stop", tags=["AZETA"])
async def azeta_push_to_odoo_stop():
    import azeta_push_odoo
    if azeta_push_odoo.stop_push():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "No hay job corriendo."
    })


@app.get("/api/v1/azeta/push-to-odoo-status", tags=["AZETA"])
async def azeta_push_to_odoo_status():
    import azeta_push_odoo
    return JSONResponse(content=azeta_push_odoo.get_push_status())


# ─── AZETA STOCK-ONLY PUSH + CRON ────────────────────────────────────

@app.post("/api/v1/azeta/stock-push-only", tags=["AZETA"])
async def azeta_stock_push_only(
    test_isbn: str | None = Query(None, description="ISBN único (modo test)"),
    max_books: int | None = Query(None, ge=1, description="Tope de libros"),
    concurrency: int | None = Query(None, ge=1, le=32, description="workers en paralelo"),
):
    """
    Push SOLO de stock.quant en AZE01. No toca description/weight/categ.
    Mucho más rápido que push-to-odoo completo.

    concurrency: workers en paralelo (default env AZETA_PUSH_CONCURRENCY o 8).

    Usa libros_proveedor AZETA como fuente de qty (debes haber corrido
    /api/v1/azeta/stock-sync antes para tenerlo fresco).
    """
    import threading
    import sys
    import azeta_push_odoo

    if azeta_push_odoo.get_stock_push_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "AZETA stock push ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                azeta_push_odoo.run_azeta_stock_push_only(
                    test_isbn=test_isbn, max_books=max_books,
                    concurrency=concurrency,
                )
            )
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={"status": "started", "test_isbn": test_isbn,
                                  "max_books": max_books,
                                  "concurrency": concurrency})


@app.post("/api/v1/azeta/stock-push-only-stop", tags=["AZETA"])
async def azeta_stock_push_only_stop():
    import azeta_push_odoo
    if azeta_push_odoo.stop_stock_push():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "No hay job corriendo."
    })


@app.get("/api/v1/azeta/stock-push-only-status", tags=["AZETA"])
async def azeta_stock_push_only_status():
    import azeta_push_odoo
    return JSONResponse(content=azeta_push_odoo.get_stock_push_status())


@app.post("/api/v1/azeta/stock-cycle", tags=["AZETA"])
async def azeta_stock_cycle():
    """
    Ciclo completo AZETA en UNA llamada: fetcher CSV -> libros_proveedor
    -> push incremental a Odoo AZE01 (solo libros cuyo stock cambio desde
    el ultimo ciclo, via marker) -> avanza marker.

    Pensado para schedulers externos (n8n cada 1h). Idempotente. Si ya
    hay un ciclo corriendo devuelve 409. El resultado queda en event_log
    (tab Auditoria) y en /api/v1/azeta/stock-push-only-status.
    """
    import threading
    import sys
    import azeta_push_odoo
    import azeta_stock

    if (azeta_push_odoo.get_stock_push_status().get("status") == "running"
            or azeta_stock.get_azeta_status().get("status") == "running"):
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Ciclo AZETA ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(azeta_push_odoo.run_full_stock_cycle())
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={"status": "started", "mode": "full_cycle"})


@app.post("/api/v1/auto-scrape/run", tags=["Auto-Scrape"])
async def auto_scrape_run(
    max_new: int | None = Query(None, ge=1, description="Tope de nuevos (test)"),
    test_sample: int | None = Query(None, ge=1, le=5000,
        description="Modo prueba: reporte de N ya creados + webhook, sin crear/scrapear"),
):
    """
    Ciclo autonomo de libros nuevos: detecta ISBNs con stock que no estan en
    Odoo -> enriquece via CDL -> crea + etiqueta -> genera reporte Excel ->
    avisa a Server A por webhook (ellos envian el correo). 1x/dia via n8n.
    409 si ya hay uno corriendo.
    """
    import threading
    import sys
    import auto_scrape

    if auto_scrape.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Auto-scrape ya esta corriendo."})

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(auto_scrape.run_auto_scrape_cycle(
                max_new=max_new, test_sample=test_sample))
        finally:
            loop.close()

    threading.Thread(target=_run_in_thread, daemon=True).start()
    return JSONResponse(content={"status": "started", "max_new": max_new,
                                 "test_sample": test_sample})


@app.get("/api/v1/auto-scrape/status", tags=["Auto-Scrape"])
async def auto_scrape_status():
    import auto_scrape
    return JSONResponse(content=auto_scrape.get_status())


@app.post("/api/v1/auto-scrape/cron/start", tags=["Auto-Scrape"])
async def auto_scrape_cron_start():
    """
    Activa el ciclo diario de libros nuevos (detectar -> scrapear CDL ->
    crear en Odoo -> Excel -> webhook a Server A para el correo).
    Para auto-arrancar tras reboot: AUTO_SCRAPE_CRON_ENABLED=1.
    """
    import auto_scrape
    if auto_scrape.start_cron():
        return JSONResponse(content={"status": "started",
                                      "interval_s": auto_scrape.CRON_INTERVAL_S})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "Cron ya corriendo o sin event loop."})


@app.post("/api/v1/auto-scrape/cron/stop", tags=["Auto-Scrape"])
async def auto_scrape_cron_stop():
    import auto_scrape
    if auto_scrape.stop_cron():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "Cron no estaba corriendo."})


@app.get("/api/v1/auto-scrape/cron/status", tags=["Auto-Scrape"])
async def auto_scrape_cron_status():
    import auto_scrape
    return JSONResponse(content=auto_scrape.get_cron_status())


@app.get("/api/v1/reportes/{report_id}", tags=["Auto-Scrape"])
async def get_reporte(report_id: str):
    """Sirve el Excel de un reporte de auto-scrape. Protegido por el Basic
    Auth global (Server A lo descarga con las credenciales de la API)."""
    import re
    import auto_scrape
    if not re.fullmatch(r"[A-Za-z0-9_-]+", report_id):
        return JSONResponse(status_code=400, content={"error": "id invalido"})
    path = os.path.join(auto_scrape.REPORTS_DIR, f"{report_id}.xlsx")
    if not os.path.isfile(path):
        return JSONResponse(status_code=404, content={"error": "reporte no encontrado"})
    return FileResponse(
        path, filename=f"{report_id}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/v1/manual/dates", tags=["Relleno Manual"])
async def manual_dates(tipo: str = Query("no_scrapeados")):
    import manual_fill
    if tipo not in ("no_scrapeados", "todos"):
        return JSONResponse(status_code=400, content={"error": "tipo invalido"})
    return JSONResponse(content={"tipo": tipo, "fechas": manual_fill.get_dates(tipo)})


@app.get("/api/v1/manual/pending", tags=["Relleno Manual"])
async def manual_pending(
    tipo: str = Query("no_scrapeados"),
    fecha: str = Query(...),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    import manual_fill
    if tipo not in ("no_scrapeados", "todos"):
        return JSONResponse(status_code=400, content={"error": "tipo invalido"})
    return JSONResponse(content=manual_fill.get_pending(tipo, fecha, page, page_size))


@app.post("/api/v1/manual/save", tags=["Relleno Manual"])
async def manual_save(request: Request):
    import manual_fill
    data = await request.json()
    if not data.get("isbn"):
        return JSONResponse(status_code=400, content={"error": "isbn requerido"})
    try:
        res = await manual_fill.save_book(data)
        return JSONResponse(content=res)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {str(e)[:200]}"})


@app.post("/api/v1/pricing/mass-update", tags=["Precios"])
async def pricing_mass_update(
    dry_run: bool = Query(True, description="True = solo calcular, sin escribir"),
    limit: int | None = Query(None, ge=1, description="Tope (test)"),
):
    """
    Motor de precios (API-15, Capa 1): aplica el suplemento por PVP bajo
    sobre pvp_base y apaga (active=False) los < 2,90 y sin precio.
    Idempotente. dry_run=True por defecto. 409 si ya corre.
    """
    import threading
    import sys
    import pricing_engine

    if pricing_engine.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Actualizacion de precios ya corriendo."})

    def _run():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(pricing_engine.run_price_update(dry_run=dry_run, limit=limit))
        finally:
            loop.close()

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse(content={"status": "started", "dry_run": dry_run})


@app.get("/api/v1/pricing/status", tags=["Precios"])
async def pricing_status():
    import pricing_engine
    return JSONResponse(content=pricing_engine.get_status())


@app.post("/api/v1/azeta/absence-shutdown", tags=["AZETA"])
async def azeta_absence_shutdown(
    dry_run: bool = Query(True, description="True = solo calcular, sin escribir"),
):
    """
    Apagado por ausencia AZETA: libros con stock en AZE01 que NO vinieron
    en el ultimo CSV de stock -> stock 0. Guardas: frescura <6h,
    completitud >=250k presentes (CSV truncado aborta), tope 15% apagado.
    SIEMPRE dry_run primero.
    """
    import threading
    import sys
    import azeta_push_odoo

    if azeta_push_odoo.get_absence_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Apagado AZETA ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                azeta_push_odoo.run_azeta_absence_shutdown(dry_run=dry_run))
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={"status": "started", "dry_run": dry_run})


@app.get("/api/v1/azeta/absence-status", tags=["AZETA"])
async def azeta_absence_status():
    import azeta_push_odoo
    return JSONResponse(content=azeta_push_odoo.get_absence_status())


@app.post("/api/v1/azeta/stock-cron/start", tags=["AZETA"])
async def azeta_stock_cron_start():
    """
    Arranca el cron interno: cada 1h descarga CSV de stock AZETA al
    mirror y luego pushea stock.quant a Odoo AZE01.

    Idempotente. El estado vive en memoria — si reinicias el servidor el
    cron NO se reinicia salvo que AZETA_STOCK_CRON_ENABLED=1 en env.
    """
    import azeta_push_odoo
    if azeta_push_odoo.start_stock_cron():
        return JSONResponse(content={
            "status": "started",
            "interval_s": azeta_push_odoo.CRON_INTERVAL_S,
        })
    return JSONResponse(status_code=400, content={
        "status": "error",
        "message": "Cron ya estaba corriendo o no hay event loop."
    })


@app.post("/api/v1/azeta/stock-cron/stop", tags=["AZETA"])
async def azeta_stock_cron_stop():
    import azeta_push_odoo
    if azeta_push_odoo.stop_stock_cron():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "Cron no estaba corriendo."
    })


@app.get("/api/v1/azeta/stock-cron/status", tags=["AZETA"])
async def azeta_stock_cron_status():
    import azeta_push_odoo
    return JSONResponse(content=azeta_push_odoo.get_cron_status())


@app.post("/api/v1/azeta/stock-marker-to-now", tags=["AZETA"])
async def azeta_stock_marker_to_now():
    """
    Setea el marker stock_actualizado_en a NOW(). Hacer DESPUES del push
    inicial completo para que el cron solo procese cambios futuros.
    """
    import azeta_push_odoo
    azeta_push_odoo._set_azeta_marker_to_now()
    marker = azeta_push_odoo._get_azeta_marker()
    return JSONResponse(content={"status": "ok", "marker": str(marker)})


# ─── Odoo Tags: clasificación Completo/Web/Foto/Stock/Bloqueado ──────

@app.post("/api/v1/odoo/tags/classify", tags=["Odoo Tags"])
async def odoo_tags_classify(
    dry_run: bool = Query(False, description="Si True, no escribe en Odoo"),
):
    """
    Clasifica los libros del mirror y asigna tags en Odoo:
    Completo / Web / Foto / Stock. Respeta Bloqueado si esta puesto.
    """
    import threading
    import sys
    import odoo_tags

    if odoo_tags.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Tag job ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                odoo_tags.run_tag_classification(dry_run=dry_run)
            )
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={"status": "started", "dry_run": dry_run})


@app.post("/api/v1/odoo/tags/classify-stop", tags=["Odoo Tags"])
async def odoo_tags_classify_stop():
    import odoo_tags
    if odoo_tags.stop():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "No hay job corriendo."
    })


@app.get("/api/v1/odoo/tags/classify-status", tags=["Odoo Tags"])
async def odoo_tags_classify_status():
    import odoo_tags
    return JSONResponse(content=odoo_tags.get_status())


@app.get("/api/v1/odoo/tags/list", tags=["Odoo Tags"])
async def odoo_tags_list():
    """Lista todos los tags + conteo de libros por tag."""
    from odoo_client import OdooClient
    async with OdooClient() as odoo:
        tags = await odoo.search_read("product.tag", [],
            ["id", "name", "color"], order="id")
        out = []
        for t in tags:
            n = await odoo.search_count("product.template",
                [["product_tag_ids", "in", [t["id"]]]])
            out.append({"id": t["id"], "name": t["name"],
                        "color": t.get("color"), "books_count": n})
    return JSONResponse(content={"tags": out})


@app.get("/api/v1/azeta/stock-marker", tags=["AZETA"])
async def azeta_stock_marker():
    """Devuelve el marker actual + cuántos libros pendientes desde ese marker."""
    import azeta_push_odoo
    import db as dbmod
    azeta_push_odoo._ensure_azeta_marker_row()
    marker = azeta_push_odoo._get_azeta_marker()

    conn = dbmod.get_connection()
    cur = conn.cursor()
    try:
        if marker:
            dbmod.execute_query(cur, """
                SELECT COUNT(*) FROM libros_proveedor lp
                JOIN odoo_books_mirror m ON m.barcode = lp.isbn
                WHERE lp.proveedor_email = ?
                  AND m.odoo_id IS NOT NULL
                  AND lp.stock_actualizado_en > ?
            """, (azeta_push_odoo.AZETA_PROVEEDOR_EMAIL, marker))
        else:
            dbmod.execute_query(cur, """
                SELECT COUNT(*) FROM libros_proveedor lp
                JOIN odoo_books_mirror m ON m.barcode = lp.isbn
                WHERE lp.proveedor_email = ?
                  AND m.odoo_id IS NOT NULL
            """, (azeta_push_odoo.AZETA_PROVEEDOR_EMAIL,))
        pending = int(cur.fetchone()[0])
    finally:
        conn.close()

    return JSONResponse(content={
        "marker": str(marker) if marker else None,
        "pendientes_desde_marker": pending,
    })


# ─── SYNC STOCK SINLI → Odoo (proveedores no-AZETA) ──────────────────

@app.post("/api/v1/sync-stock/run-once", tags=["SINLI Sync"])
async def sync_stock_run_once(
    solo_proveedor: str | None = Query(None, description="email del proveedor (filtro, opcional)"),
    max_books: int | None = Query(None, ge=1, description="tope total (None = sin tope)"),
    concurrency: int | None = Query(None, ge=1, le=32, description="workers en paralelo"),
):
    """
    Una pasada del sync: 1 lote de hasta 2000 libros SINLI (no-AZETA) que
    hayan cambiado desde el ultimo_timestamp.

    Si pasas solo_proveedor o max_books, NO avanza el marcapaginas (modo
    validación). Para procesar normalmente y avanzar marker, no pases filtros.
    """
    import threading
    import sys
    import sync_stock_sinli

    if sync_stock_sinli.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Sync SINLI ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                sync_stock_sinli.run_once(
                    loop_until_empty=False,
                    solo_proveedor=solo_proveedor,
                    max_books=max_books,
                    concurrency=concurrency,
                )
            )
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={
        "status": "started",
        "solo_proveedor": solo_proveedor,
        "max_books": max_books,
        "concurrency": concurrency,
    })


@app.post("/api/v1/sync-stock/run-backlog", tags=["SINLI Sync"])
async def sync_stock_run_backlog(
    solo_proveedor: str | None = Query(None, description="filtro por proveedor"),
    max_books: int | None = Query(None, ge=1, description="tope total"),
    concurrency: int | None = Query(None, ge=1, le=32),
):
    """
    Modo backlog: bucle hasta vaciar todos los pendientes (~104k libros la
    primera vez). Idempotente, se puede detener con /stop.

    Si pasas solo_proveedor o max_books, NO avanza marcapaginas (validación).
    """
    import threading
    import sys
    import sync_stock_sinli

    if sync_stock_sinli.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Sync SINLI ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                sync_stock_sinli.run_once(
                    loop_until_empty=True,
                    solo_proveedor=solo_proveedor,
                    max_books=max_books,
                    concurrency=concurrency,
                )
            )
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={
        "status": "started", "mode": "backlog",
        "solo_proveedor": solo_proveedor,
        "max_books": max_books,
        "concurrency": concurrency,
    })


@app.post("/api/v1/sync-stock/stop", tags=["SINLI Sync"])
async def sync_stock_stop():
    import sync_stock_sinli
    if sync_stock_sinli.stop():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "No hay sync corriendo."
    })


@app.get("/api/v1/sync-stock/status", tags=["SINLI Sync"])
async def sync_stock_status():
    import sync_stock_sinli
    return JSONResponse(content={
        "job": sync_stock_sinli.get_status(),
        "marker": sync_stock_sinli.get_marker_info(),
        "pending_count": sync_stock_sinli.get_pending_count(),
        "recent_errors": sync_stock_sinli.get_recent_errors(20),
    })


@app.post("/api/v1/sync-stock/cron/start", tags=["SINLI Sync"])
async def sync_stock_cron_start():
    """Activa cron 1h del sync SINLI."""
    import sync_stock_sinli
    if sync_stock_sinli.start_cron():
        return JSONResponse(content={
            "status": "started",
            "interval_s": sync_stock_sinli.CRON_INTERVAL_S,
        })
    return JSONResponse(status_code=400, content={
        "status": "error",
        "message": "Cron ya corriendo o sin event loop."
    })


@app.post("/api/v1/sync-stock/cron/stop", tags=["SINLI Sync"])
async def sync_stock_cron_stop():
    import sync_stock_sinli
    if sync_stock_sinli.stop_cron():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "Cron no estaba corriendo."
    })


@app.get("/api/v1/sync-stock/cron/status", tags=["SINLI Sync"])
async def sync_stock_cron_status():
    import sync_stock_sinli
    return JSONResponse(content=sync_stock_sinli.get_cron_status())


@app.post("/api/v1/sync-stock/marker-to-now", tags=["SINLI Sync"])
async def sync_stock_marker_to_now():
    """
    Setea ultimo_timestamp = NOW(). Usar SOLO después de terminar el
    backlog inicial — evita reprocesar todo en la siguiente corrida.
    """
    import sync_stock_sinli
    sync_stock_sinli._set_marker_to_now()
    return JSONResponse(content={"status": "ok",
                                  "marker": sync_stock_sinli.get_marker_info()})


@app.post("/api/v1/sync-stock/cegald-replace", tags=["SINLI Sync"])
async def sync_stock_cegald_replace(
    proveedor: str = Query(..., description="proveedor_email (ej. sinli@akal.com)"),
    dry_run: bool = Query(True, description="True = solo calcular, sin escribir"),
):
    """
    Reemplazo completo CEGALD para UN proveedor (spec Server A):
    lo presente en el ultimo CEGALD queda a 1 (via sync normal); lo que
    tiene stock en Odoo pero YA NO viene en el CEGALD se apaga (stock 0),
    SOLO en el almacen de ese proveedor.

    Salvaguardas: si presentes < 50% del stock actual, o el CEGALD tiene
    mas de 48h, NO se apaga nada (solo reporta). Empezar SIEMPRE con
    dry_run=true.
    """
    import threading
    import sys
    import sync_stock_sinli

    if sync_stock_sinli.get_cegald_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "CEGALD replacement ya esta corriendo."
        })

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                sync_stock_sinli.run_cegald_replacement(
                    proveedor, dry_run=dry_run)
            )
        finally:
            new_loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    return JSONResponse(content={"status": "started",
                                  "proveedor": proveedor, "dry_run": dry_run})


@app.get("/api/v1/sync-stock/cegald-status", tags=["SINLI Sync"])
async def sync_stock_cegald_status():
    import sync_stock_sinli
    return JSONResponse(content=sync_stock_sinli.get_cegald_status())


@app.post("/api/v1/sync-stock/cegald-stop", tags=["SINLI Sync"])
async def sync_stock_cegald_stop():
    import sync_stock_sinli
    if sync_stock_sinli.stop_cegald():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "No hay CEGALD job corriendo."
    })


# ─── Proveedores / almacenes (pausar, reactivar, alta) ───────────────

@app.get("/api/v1/proveedores", tags=["Proveedores"])
async def proveedores_listar(
    con_stats: bool = Query(True, description="totales en BD/Odoo + ultimo fichero"),
    con_odoo: bool = Query(True, description="consultar stock encendido en Odoo (mas lento)"),
):
    """
    Proveedores con almacen mapeado: totales en BD (libros / con stock),
    cuantos existen ya en Odoo, cuantos estan encendidos en su almacen,
    ultimo CEGALD y ultimo fichero recibido, y estado activo/pausado.
    Ademas, los que mandan libros pero NO tienen almacen.
    """
    import proveedores_admin
    provs = proveedores_admin.listar(con_stats=con_stats)
    if con_odoo and provs:
        try:
            encendidos = await proveedores_admin.stock_odoo_por_almacen(
                [p["warehouse_code"] for p in provs])
            for p in provs:
                p["encendidos_odoo"] = encendidos.get(p["warehouse_code"])
        except Exception as e:
            for p in provs:
                p["encendidos_odoo"] = None
            print(f"[Proveedores] stock Odoo FALLO: {type(e).__name__}: {e}")
    return JSONResponse(content={
        "proveedores": provs,
        "sin_mapear": proveedores_admin.sin_mapear(),
        "job": proveedores_admin.get_status(),
    })


@app.post("/api/v1/proveedores/pausar", tags=["Proveedores"])
async def proveedores_pausar(
    email: str = Query(..., description="proveedor_email"),
    dry_run: bool = Query(True, description="True = solo contar, sin escribir"),
    motivo: str | None = Query(None, description="ej. 'cierre verano'"),
):
    """
    Pausa un proveedor: deja de sincronizarse Y su stock en Odoo va a 0
    (solo en SU almacen). Con dry_run=true devuelve cuantos quants apagaria.
    Reversible con /reactivar, pero el stock solo vuelve con su proximo archivo.
    """
    import threading
    import sys
    import proveedores_admin

    if proveedores_admin.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Ya hay una pausa/alta corriendo."
        })

    if dry_run:
        return JSONResponse(content=await proveedores_admin.pausar(
            email, dry_run=True, motivo=motivo))

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                proveedores_admin.pausar(email, dry_run=False, motivo=motivo))
        finally:
            new_loop.close()

    threading.Thread(target=_run_in_thread, daemon=True).start()
    return JSONResponse(content={"status": "started", "proveedor": email,
                                  "dry_run": False, "motivo": motivo})


@app.post("/api/v1/proveedores/reactivar", tags=["Proveedores"])
async def proveedores_reactivar(
    email: str = Query(...),
    empujar: bool = Query(True, description="marcar sus libros para re-empuje"),
):
    """
    Quita la pausa y marca sus libros para que el sync vuelva a subir su
    stock en la proxima pasada. Con empujar=false solo quita la marca (el
    stock solo volveria para los libros cuya cantidad cambie).
    """
    import proveedores_admin
    res = proveedores_admin.reactivar(email, empujar=empujar)
    code = 400 if res.get("status") == "error" else 200
    return JSONResponse(status_code=code, content=res)


@app.post("/api/v1/proveedores/empujar-ahora", tags=["Proveedores"])
async def proveedores_empujar_ahora(email: str = Query(...)):
    """
    Marca los libros del proveedor (los que ya existen en Odoo) como
    cambiados ahora, para que el sync los empuje sin esperar a su proximo
    fichero. No cambia stock ni precio, solo el timestamp del marcapaginas.
    """
    import proveedores_admin
    res = proveedores_admin.forzar_resync(email)
    code = 400 if res.get("status") == "error" else 200
    return JSONResponse(status_code=code, content=res)


@app.post("/api/v1/proveedores/reparar-catalogo", tags=["Proveedores"])
async def proveedores_reparar_catalogo(
    email: str | None = Query(None, description="proveedor_email; vacio = todo el catalogo"),
    dry_run: bool = Query(True, description="True = solo contar"),
):
    """
    Arregla los dos estados que impiden que un libro lleve stock:
    (1) sin Track Inventory (is_storable=False) — Odoo rechaza sus quants;
    (2) variante archivada con plantilla activa — el sync no encuentra el
    product.product. Empezar siempre con dry_run=true.
    """
    import threading
    import sys
    import proveedores_admin

    if proveedores_admin.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Ya hay un job de proveedores corriendo."
        })

    if dry_run:
        return JSONResponse(content=await
            proveedores_admin.reparar_catalogo(email, dry_run=True))

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                proveedores_admin.reparar_catalogo(email, dry_run=False))
        finally:
            new_loop.close()

    threading.Thread(target=_run_in_thread, daemon=True).start()
    return JSONResponse(content={"status": "started", "proveedor": email or "TODOS"})


@app.post("/api/v1/proveedores/conciliar", tags=["Proveedores"])
async def proveedores_conciliar(
    email: str | None = Query(None, description="proveedor_email; vacio = todos"),
    dry_run: bool = Query(True, description="True = solo contar lo que falta"),
):
    """
    Compara lo que DEBERIA tener stock (stock > 0 en BD, existe en Odoo,
    PVP >= 2,90) con los quants reales de cada almacen, y marca para
    re-empuje solo lo que falta. Detecta libros que entraron con stock y
    nunca cambiaron de cantidad: el sync no los mira y se quedan sin quant.
    AZETA queda fuera (su stock lo empuja su propio job).
    """
    import threading
    import sys
    import proveedores_admin

    if proveedores_admin.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Ya hay un job de proveedores corriendo."
        })

    if dry_run:
        return JSONResponse(content=await
            proveedores_admin.conciliar(email, dry_run=True))

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                proveedores_admin.conciliar(email, dry_run=False))
        finally:
            new_loop.close()

    threading.Thread(target=_run_in_thread, daemon=True).start()
    return JSONResponse(content={"status": "started", "proveedor": email or "TODOS"})


@app.post("/api/v1/proveedores/crear-nuevos", tags=["Proveedores"])
async def proveedores_crear_nuevos(
    email: str = Query(...),
    valor: bool = Query(..., description="True = crear sus libros nuevos en Odoo"),
):
    """
    Enciende/apaga la creacion automatica de los libros nuevos de un
    proveedor. En false, el ciclo diario ignora sus novedades (caso
    PODIPRINT: 100.000 titulos sin ficha en Casa del Libro).
    """
    import proveedores_admin
    res = proveedores_admin.set_crear_nuevos(email, valor)
    code = 400 if res.get("status") == "error" else 200
    return JSONResponse(status_code=code, content=res)


@app.get("/api/v1/proveedores/status", tags=["Proveedores"])
async def proveedores_status():
    """Estado del job de pausa (apagado de stock en curso)."""
    import proveedores_admin
    return JSONResponse(content=proveedores_admin.get_status())


@app.post("/api/v1/proveedores/stop", tags=["Proveedores"])
async def proveedores_stop():
    import proveedores_admin
    if proveedores_admin.stop():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "No hay job de proveedores corriendo."
    })


@app.post("/api/v1/proveedores/alta", tags=["Proveedores"])
async def proveedores_alta(
    email: str = Query(..., description="proveedor_email tal como llega en el SINLI"),
    nombre: str = Query(..., description="nombre real, ej. PODIPRINT"),
    warehouse_code: str = Query(..., description="codigo de almacen, ej. POD01"),
    warehouse_name: str | None = Query(None, description="nombre del almacen (default: nombre)"),
):
    """
    Da de alta un proveedor: crea su almacen en Odoo si no existe, guarda el
    mapeo proveedor->almacen y corrige su nombre en la tabla proveedores.
    Idempotente.
    """
    import proveedores_admin
    res = await proveedores_admin.alta(email, nombre, warehouse_code,
                                        warehouse_name)
    code = 400 if res.get("status") == "error" else 200
    return JSONResponse(status_code=code, content=res)


# ─── Auditoría: event_log + resumen para la tab Auditoría ───────────

@app.get("/api/v1/audit/events", tags=["Auditoria"])
async def audit_events(
    categoria: str | None = Query(None),
    nivel: str | None = Query(None, description="info | error"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Eventos del event_log, mas reciente primero."""
    import audit_log
    audit_log.ensure_table()
    return JSONResponse(content={
        "events": audit_log.get_events(categoria=categoria, nivel=nivel,
                                        limit=limit, offset=offset),
    })


@app.get("/api/v1/audit/requests", tags=["Auditoria"])
async def audit_requests(
    limit: int = Query(100, ge=1, le=500),
    path_like: str | None = Query(None, description="filtro por path, ej. stock-cycle"),
):
    """
    Peticiones API entrantes (acciones POST/PUT/DELETE): quien llamo que
    endpoint (IP, usuario Basic, user-agent), con que query/body, status
    y duracion. Los GET de polling no se registran.
    """
    import audit_log
    audit_log.ensure_table()
    return JSONResponse(content={
        "requests": audit_log.get_api_requests(limit=limit, path_like=path_like),
    })


@app.get("/api/v1/audit/cegalds", tags=["Auditoria"])
async def audit_cegalds():
    """
    Auditoría CEGALD por proveedor: último evento (goteo incluido),
    último CEGALD COMPLETO (corrida grande, >= max(500, 20% del stock)),
    su tamaño, total con stock y fantasmas (stock > 0 no reportado desde
    el último CEGALD completo).
    """
    import audit_log
    return JSONResponse(content={"cegalds": audit_log.get_cegald_overview()})


@app.get("/api/v1/audit/summary", tags=["Auditoria"])
async def audit_summary(days: int = Query(7, ge=1, le=30)):
    """
    Resumen para auditar: libros recibidos por dia/proveedor (BD),
    eventos por categoria/dia, stats de hoy, ultimo evento por categoria.
    """
    import audit_log
    audit_log.ensure_table()
    return JSONResponse(content=audit_log.get_summary(days=days))


@app.get("/api/v1/audit/stock-proveedor", tags=["Auditoria"])
async def audit_stock_proveedor():
    """
    Stock ENCENDIDO por proveedor (stock.quant con quantity>0 en su almacen).
    Los almacenes salen de proveedor_almacen_odoo, no de una lista fija: un
    proveedor nuevo aparece aqui solo. Consulta Odoo en paralelo. Solo
    productos activos (los apagados por la regla de precios no son
    publicables)."""
    import proveedores_admin
    provs = proveedores_admin.listar(con_stats=False)
    encendidos = await proveedores_admin.stock_odoo_por_almacen(
        [p["warehouse_code"] for p in provs])
    rows = [{"proveedor": p["nombre"], "almacen": p["warehouse_code"],
             "encendidos": encendidos.get(p["warehouse_code"])}
            for p in provs]
    rows.sort(key=lambda r: -(r["encendidos"] or 0))
    total = sum(r["encendidos"] for r in rows if r["encendidos"])
    return JSONResponse(content={"proveedores": rows, "total": total})


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


@app.get("/api/v1/admin/inferred-categories-summary", tags=["Admin"])
async def admin_inferred_categories_summary(top_n: int = Query(50, ge=10, le=500)):
    """
    Resumen de inferred_categories en odoo_books_mirror + readiness por campo
    para el push a Odoo (Fase 2).

    Categorias:
      - total_books_with_categ, distinct_paths, distinct_leaves, distinct_roots
      - by_source, depth_distribution
      - top_paths (top_n por conteo), top_roots (top_n root por libros)

    Readiness para push a Odoo (cuantos libros AZETA tienen cada campo lleno):
      - azeta_books_total: libros AZETA enriquecidos (azeta_fetched_at NOT NULL)
      - with_description, with_weight, with_dimensions, with_image,
        with_price_eur, with_categ
      - fully_ready: libros que tienen TODO (desc + peso + dim + imagen +
        precio + categ) — listos para push sin lagunas
    """
    import db as dbmod
    conn = dbmod.get_connection()
    cur = conn.cursor()
    try:
        out: dict = {}

        # Total libros con categoria
        dbmod.execute_query(cur, """
            SELECT COUNT(*) FROM odoo_books_mirror
            WHERE inferred_categories IS NOT NULL AND inferred_categories <> ''
        """)
        out["total_books_with_categ"] = int(cur.fetchone()[0])

        # Distinct paths
        dbmod.execute_query(cur, """
            SELECT COUNT(DISTINCT inferred_categories) FROM odoo_books_mirror
            WHERE inferred_categories IS NOT NULL AND inferred_categories <> ''
        """)
        out["distinct_paths"] = int(cur.fetchone()[0])

        # Por inferred_source
        dbmod.execute_query(cur, """
            SELECT COALESCE(inferred_source, 'unknown') AS src, COUNT(*) AS n
            FROM odoo_books_mirror
            WHERE inferred_categories IS NOT NULL AND inferred_categories <> ''
            GROUP BY COALESCE(inferred_source, 'unknown')
            ORDER BY n DESC
        """)
        out["by_source"] = [{"source": r[0], "books": int(r[1])} for r in cur.fetchall()]

        # Top N paths por conteo
        dbmod.execute_query(cur, """
            SELECT inferred_categories AS path, COUNT(*) AS n
            FROM odoo_books_mirror
            WHERE inferred_categories IS NOT NULL AND inferred_categories <> ''
            GROUP BY inferred_categories
            ORDER BY n DESC
            LIMIT ?
        """, (top_n,))
        out["top_paths"] = [
            {"path": r[0], "books": int(r[1])} for r in cur.fetchall()
        ]

        # Para distribución de profundidad y top roots: cargar todos los paths
        # con conteo (suele ser <100k filas, manejable en memoria)
        dbmod.execute_query(cur, """
            SELECT inferred_categories AS path, COUNT(*) AS n
            FROM odoo_books_mirror
            WHERE inferred_categories IS NOT NULL AND inferred_categories <> ''
            GROUP BY inferred_categories
        """)
        all_paths = cur.fetchall()

        # Distribucion de profundidad (cuantos " > " hay)
        depth_dist: dict[int, int] = {}
        leaves: set[str] = set()
        roots_count: dict[str, int] = {}
        for path, n in all_paths:
            parts = [p.strip() for p in path.split(" > ") if p.strip()]
            if not parts:
                continue
            d = len(parts)
            depth_dist[d] = depth_dist.get(d, 0) + 1
            leaves.add(parts[-1])
            roots_count[parts[0]] = roots_count.get(parts[0], 0) + int(n)
        out["distinct_leaves"] = len(leaves)
        out["distinct_roots"] = len(roots_count)
        out["depth_distribution"] = [
            {"depth": k, "distinct_paths": v}
            for k, v in sorted(depth_dist.items())
        ]
        out["top_roots"] = [
            {"root": k, "books_total": v}
            for k, v in sorted(roots_count.items(), key=lambda x: -x[1])[:top_n]
        ]

        # ── Readiness por campo (libros AZETA con cada dato lleno) ──
        # NULLIF para tratar string vacio como NULL en TEXT columns.
        dbmod.execute_query(cur, """
            SELECT
              COUNT(*) FILTER (WHERE azeta_fetched_at IS NOT NULL)                                     AS azeta_total,
              COUNT(*) FILTER (WHERE azeta_fetched_at IS NOT NULL AND NULLIF(description, '')   IS NOT NULL) AS with_description,
              COUNT(*) FILTER (WHERE azeta_fetched_at IS NOT NULL AND NULLIF(cdl_weight, '')    IS NOT NULL) AS with_weight,
              COUNT(*) FILTER (WHERE azeta_fetched_at IS NOT NULL AND NULLIF(cdl_height, '')    IS NOT NULL
                                                                  AND NULLIF(cdl_width, '')     IS NOT NULL) AS with_dimensions,
              COUNT(*) FILTER (WHERE azeta_fetched_at IS NOT NULL AND NULLIF(cdl_image_url, '') IS NOT NULL) AS with_image,
              COUNT(*) FILTER (WHERE azeta_fetched_at IS NOT NULL AND azeta_price_eur            IS NOT NULL) AS with_price_eur,
              COUNT(*) FILTER (WHERE azeta_fetched_at IS NOT NULL AND NULLIF(inferred_categories, '') IS NOT NULL) AS with_categ,
              COUNT(*) FILTER (WHERE azeta_fetched_at IS NOT NULL
                                AND NULLIF(description, '')         IS NOT NULL
                                AND NULLIF(cdl_weight, '')          IS NOT NULL
                                AND NULLIF(cdl_height, '')          IS NOT NULL
                                AND NULLIF(cdl_width, '')           IS NOT NULL
                                AND NULLIF(cdl_image_url, '')       IS NOT NULL
                                AND azeta_price_eur                 IS NOT NULL
                                AND NULLIF(inferred_categories, '') IS NOT NULL) AS fully_ready
            FROM odoo_books_mirror
        """)
        r = cur.fetchone()
        total = int(r[0] or 0)

        def _pct(n: int) -> str:
            return f"{(n/total*100):.1f}%" if total else "0.0%"

        ready = {
            "azeta_books_total": total,
            "with_description":  {"n": int(r[1] or 0), "pct": _pct(int(r[1] or 0))},
            "with_weight":       {"n": int(r[2] or 0), "pct": _pct(int(r[2] or 0))},
            "with_dimensions":   {"n": int(r[3] or 0), "pct": _pct(int(r[3] or 0))},
            "with_image":        {"n": int(r[4] or 0), "pct": _pct(int(r[4] or 0))},
            "with_price_eur":    {"n": int(r[5] or 0), "pct": _pct(int(r[5] or 0))},
            "with_categ":        {"n": int(r[6] or 0), "pct": _pct(int(r[6] or 0))},
            "fully_ready":       {"n": int(r[7] or 0), "pct": _pct(int(r[7] or 0))},
        }
        out["readiness_for_odoo"] = ready

        return JSONResponse(content=out)
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": f"{type(e).__name__}: {e}"
        })
    finally:
        conn.close()


@app.get("/api/v1/admin/stock-debug", tags=["Admin"])
async def admin_stock_debug(isbn: str = Query(..., description="ISBN/barcode a inspeccionar")):
    """
    Devuelve el estado de un ISBN en BD (libros_proveedor + mirror) y en
    Odoo (product.template + product.product + stock.quant en todas las
    locations). Util para validar cualquier desincronización.
    """
    import db as dbmod
    from odoo_client import OdooClient

    isbn = (isbn or "").strip()
    if not isbn:
        return JSONResponse(status_code=400, content={"error": "isbn requerido"})

    out: dict[str, Any] = {"isbn": isbn}
    conn = dbmod.get_connection()
    cur = conn.cursor()
    try:
        # 1. libros_proveedor
        dbmod.execute_query(cur, """
            SELECT proveedor_email, stock_disponible, stock_actualizado_en,
                   precio_con_iva, actualizado_en
            FROM libros_proveedor WHERE isbn = ?
            ORDER BY proveedor_email
        """, (isbn,))
        out["libros_proveedor"] = [{
            "proveedor_email": r[0], "stock_disponible": r[1],
            "stock_actualizado_en": str(r[2]) if r[2] else None,
            "precio_con_iva": float(r[3]) if r[3] is not None else None,
            "actualizado_en": str(r[4]) if r[4] else None,
        } for r in cur.fetchall()]

        # 2. mirror
        dbmod.execute_query(cur, """
            SELECT odoo_id, name, list_price, azeta_fetched_at,
                   azeta_price_eur, supplier_names
            FROM odoo_books_mirror WHERE barcode = ?
        """, (isbn,))
        r = cur.fetchone()
        out["mirror"] = {
            "odoo_id": r[0], "name": r[1],
            "list_price": float(r[2]) if r[2] is not None else None,
            "azeta_fetched_at": str(r[3]) if r[3] else None,
            "azeta_price_eur": float(r[4]) if r[4] is not None else None,
            "supplier_names": r[5],
        } if r else None

        # 3. mapeo proveedor->warehouse
        dbmod.execute_query(cur, """
            SELECT proveedor_email, warehouse_code, nombre_proveedor
            FROM proveedor_almacen_odoo
        """)
        out["proveedor_warehouse_map"] = {
            r[0]: {"code": r[1], "nombre": r[2]} for r in cur.fetchall()
        }

        # 4. errores recientes para este ISBN
        dbmod.execute_query(cur, """
            SELECT mensaje_error, proveedor_email, creado_en, intentos
            FROM sync_errores WHERE isbn = ?
            ORDER BY creado_en DESC LIMIT 10
        """, (isbn,))
        out["sync_errores"] = [{
            "mensaje": r[0], "proveedor": r[1],
            "cuando": str(r[2]), "intentos": r[3],
        } for r in cur.fetchall()]
    finally:
        conn.close()

    # 5. Odoo: template + variantes + quants en TODAS las locations
    try:
        async with OdooClient() as odoo:
            templates = await odoo.search_read(
                "product.template", [["barcode", "=", isbn]],
                ["id", "name", "list_price", "categ_id", "weight",
                 "qty_available"], limit=5,
            )
            out["odoo_templates"] = []
            for t in templates:
                tmpl_entry: dict = {
                    "id": t["id"], "name": t["name"],
                    "list_price": t["list_price"],
                    "qty_available_total": t.get("qty_available"),
                    "weight": t.get("weight"),
                    "categ_id": t.get("categ_id"),
                    "variants": [],
                }
                variants = await odoo.search_read(
                    "product.product",
                    [["product_tmpl_id", "=", t["id"]]],
                    ["id", "name"], limit=10,
                )
                for v in variants:
                    quants = await odoo.search_read(
                        "stock.quant",
                        [["product_id", "=", v["id"]]],
                        ["id", "location_id", "quantity",
                         "inventory_quantity", "write_date"],
                    )
                    tmpl_entry["variants"].append({
                        "product_id": v["id"], "name": v["name"],
                        "quants": [{
                            "id": q["id"],
                            "location": q["location_id"],
                            "quantity": q["quantity"],
                            "inventory_quantity": q["inventory_quantity"],
                            "write_date": q["write_date"],
                            "synced": abs(float(q["quantity"] or 0)
                                          - float(q["inventory_quantity"] or 0)) < 0.01,
                        } for q in quants],
                    })
                out["odoo_templates"].append(tmpl_entry)
    except Exception as e:
        out["odoo_error"] = f"{type(e).__name__}: {e}"

    return JSONResponse(content=out)


@app.get("/api/v1/admin/odoo-warehouses", tags=["Admin"])
async def admin_odoo_warehouses():
    """
    Lista los warehouses de Odoo con su code y lot_stock_id. Util para que
    el sync SINLI verifique que sus codigos (AZE01, ICA01, LES01...) coinciden
    con los reales antes de escribir stock.quant.
    """
    from odoo_client import OdooClient
    try:
        async with OdooClient() as odoo:
            rows = await odoo.search_read(
                "stock.warehouse",
                [],
                ["id", "code", "name", "lot_stock_id"],
                order="code",
            )
        out = []
        for r in rows:
            lot = r.get("lot_stock_id")
            lot_id = lot[0] if isinstance(lot, list) and lot else None
            lot_name = lot[1] if isinstance(lot, list) and len(lot) > 1 else None
            out.append({
                "id": r.get("id"),
                "code": r.get("code") or "",
                "name": r.get("name") or "",
                "lot_stock_id": lot_id,
                "lot_stock_name": lot_name,
            })
        return JSONResponse(content={"warehouses": out, "count": len(out)})
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": f"{type(e).__name__}: {e}"
        })


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


@app.get("/api/v1/odoo/mirror/browse", tags=["Odoo"])
async def odoo_mirror_browse(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=200),
    search: str = Query(""),
    has_category: str = Query("any"),
    has_description: str = Query("any"),
    has_supplier: str = Query("any"),
    has_image: str = Query("any"),
    supplier: str = Query(""),
):
    """
    Lista paginada del mirror con filtros utiles para auditar el progreso
    del enriquecimiento. Para detalle de un libro -> /odoo/mirror/book/{id}.
    """
    import db as dbmod
    where = ["1=1"]
    params = []

    if search.strip():
        s = f"%{search.strip()}%"
        where.append("(m.barcode ILIKE ? OR m.name ILIKE ? OR m.cdl_author ILIKE ?)")
        params.extend([s, s, s])

    def _flag(col, mode):
        if mode == "yes":
            where.append(f"({col} IS NOT NULL AND {col} <> '')")
        elif mode == "no":
            where.append(f"({col} IS NULL OR {col} = '')")

    _flag("m.inferred_categories", has_category)
    _flag("m.description", has_description)
    _flag("m.supplier_names", has_supplier)
    _flag("COALESCE(m.cdl_image_url, m.gbooks_thumbnail)", has_image)

    if supplier.strip():
        where.append("m.supplier_names ILIKE ?")
        params.append(f"%{supplier.strip()}%")

    where_sql = " AND ".join(where)
    offset = (page - 1) * per_page

    conn = dbmod.get_connection()
    cur = conn.cursor()
    try:
        dbmod.execute_query(cur, f"SELECT COUNT(*) FROM odoo_books_mirror m WHERE {where_sql}", tuple(params))
        total = cur.fetchone()[0]

        dbmod.execute_query(cur, f"""
            SELECT
                m.odoo_id, m.barcode,
                COALESCE(NULLIF(m.name, ''), '') AS titulo,
                m.cdl_author AS autor,
                COALESCE(m.cdl_editorial, m.gbooks_publisher) AS editorial,
                m.inferred_categories AS categorias,
                CASE WHEN m.description IS NOT NULL AND m.description <> '' THEN 1 ELSE 0 END AS tiene_desc,
                m.supplier_names AS proveedor,
                COALESCE(m.cdl_image_url, m.gbooks_thumbnail) AS imagen,
                m.cdl_fetched_at, m.gbooks_fetched_at, m.suppliers_synced_at
            FROM odoo_books_mirror m
            WHERE {where_sql}
            ORDER BY m.odoo_id
            LIMIT ? OFFSET ?
        """, tuple(params) + (per_page, offset))

        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            for k in ("cdl_fetched_at", "gbooks_fetched_at", "suppliers_synced_at"):
                if r.get(k) is not None:
                    r[k] = str(r[k])
        return JSONResponse(content={
            "total": total, "page": page, "per_page": per_page,
            "pages": (total + per_page - 1) // per_page,
            "rows": rows,
        })
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {e}"})
    finally:
        conn.close()


@app.get("/api/v1/odoo/mirror/book/{odoo_id}", tags=["Odoo"])
async def odoo_mirror_book_detail(odoo_id: int):
    """Vista 360 de un libro: mirror + JOIN con books (CDL) + distributor_books."""
    import db as dbmod
    conn = dbmod.get_connection()
    cur = conn.cursor()
    try:
        dbmod.execute_query(cur, """
            SELECT m.*, b.author AS books_author, b.editorial AS books_editorial,
                   b.weight AS books_weight, b.height AS books_height,
                   b.width AS books_width, b.translator AS books_translator,
                   b.illustrator AS books_illustrator, b.collection AS books_collection,
                   b.url AS books_url, b.image_url AS books_image,
                   d.fuente AS dist_fuente, d.author AS dist_author,
                   d.editorial AS dist_editorial, d.price AS dist_price
            FROM odoo_books_mirror m
            LEFT JOIN books b ON m.barcode = b.isbn
            LEFT JOIN distributor_books d ON m.barcode = d.isbn
            WHERE m.odoo_id = ?
        """, (odoo_id,))
        row = cur.fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": "not found"})
        cols = [c[0] for c in cur.description]
        d = dict(zip(cols, row))
        from decimal import Decimal
        for k, v in list(d.items()):
            if isinstance(v, Decimal):
                d[k] = float(v)
            elif hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        return JSONResponse(content=d)
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {e}"})
    finally:
        conn.close()


@app.get("/api/v1/odoo/mirror/suppliers-list", tags=["Odoo"])
async def odoo_mirror_suppliers_list():
    """Top proveedores por conteo. Util como datalist en filtros de browse."""
    import db as dbmod
    conn = dbmod.get_connection()
    cur = conn.cursor()
    try:
        dbmod.execute_query(cur, """
            SELECT supplier_names, COUNT(*) AS n
            FROM odoo_books_mirror
            WHERE supplier_names IS NOT NULL AND supplier_names <> ''
            GROUP BY supplier_names
            ORDER BY n DESC
            LIMIT 50
        """)
        rows = [{"supplier": r[0], "count": r[1]} for r in cur.fetchall()]
        return JSONResponse(content={"suppliers": rows})
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {e}"})
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


# ─── Shopify: auditar, generar fichas, publicar ──────────────────────

def _shopify_en_hilo(fn, *args, **kwargs):
    """Lanza un job de Shopify en segundo plano con su propio event loop."""
    import threading
    import sys

    def _run():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            fn(*args, **kwargs)
        finally:
            loop.close()

    threading.Thread(target=_run, daemon=True).start()


@app.get("/api/v1/shopify/estado", tags=["Shopify"])
async def shopify_estado(con_tienda: bool = Query(False, description="consultar Shopify en vivo")):
    """
    Cuantos productos hay publicados, cuantas fichas generadas sin subir y
    cuantos libros quedan por publicar, con el motivo de los descartados.
    """
    import shopify_pub as sp
    out = {
        "resumen": sp.resumen(),
        "candidatos": len(sp.candidatos_sin_publicar()),
        "descartados": sp.candidatos_descartados(),
        "job": sp.get_status(),
    }
    if con_tienda:
        try:
            import shopify_api as sa
            out["tienda"] = sa.info_tienda()
            out["productos_en_shopify"] = sa.contar_productos()
        except Exception as e:
            out["tienda_error"] = f"{type(e).__name__}: {e}"
    return JSONResponse(content=out)


@app.post("/api/v1/shopify/auditar", tags=["Shopify"])
async def shopify_auditar(escribir: bool = Query(True, description="anotar los que falten")):
    """
    Compara la tienda con nuestra tabla. Los productos que estan en Shopify y
    no teniamos fichados se anotan como publicados, para no regenerarlos.
    """
    import shopify_pub as sp
    if sp.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Ya hay un job de Shopify corriendo."})
    _shopify_en_hilo(sp.auditar, escribir)
    return JSONResponse(content={"status": "started", "accion": "auditar"})


@app.post("/api/v1/shopify/generar", tags=["Shopify"])
async def shopify_generar(
    limite: int | None = Query(None, ge=1, description="cuantas fichas generar"),
    concurrencia: int | None = Query(None, ge=1, le=30),
):
    """
    Genera con IA las fichas de los libros pendientes y las deja guardadas.
    NO publica nada: eso es el paso siguiente.
    """
    import shopify_pub as sp
    if sp.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Ya hay un job de Shopify corriendo."})
    _shopify_en_hilo(sp.generar_fichas, limite, concurrencia)
    return JSONResponse(content={"status": "started", "accion": "generar",
                                  "limite": limite})


@app.post("/api/v1/shopify/publicar", tags=["Shopify"])
async def shopify_publicar(
    limite: int | None = Query(None, ge=1),
    dry_run: bool = Query(True, description="True = solo decir que subiria"),
):
    """
    Sube a Shopify las fichas generadas. Tope diario por el limite de Shopify
    (1.000 variantes nuevas al dia). Empezar siempre con dry_run.
    """
    import shopify_pub as sp
    if sp.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Ya hay un job de Shopify corriendo."})
    if dry_run:
        return JSONResponse(content=sp.publicar(limite=limite, dry_run=True))
    _shopify_en_hilo(sp.publicar, limite, False)
    return JSONResponse(content={"status": "started", "accion": "publicar"})


@app.post("/api/v1/shopify/exportar", tags=["Shopify"])
async def shopify_exportar(estado: str = Query("generado")):
    """Escribe el XLSX Matrixify de las fichas en ese estado."""
    import shopify_pub as sp
    if sp.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Ya hay un job de Shopify corriendo."})
    return JSONResponse(content=sp.exportar_xlsx(estado=estado))


@app.get("/api/v1/shopify/descargar/{fichero}", tags=["Shopify"])
async def shopify_descargar(fichero: str):
    """Descarga un XLSX generado. Protegido por la auth de la app."""
    from fastapi.responses import FileResponse
    import shopify_pub as sp
    if "/" in fichero or "\\" in fichero or ".." in fichero:
        return JSONResponse(status_code=400, content={"error": "nombre no valido"})
    ruta = os.path.join(sp.DIR_SALIDA, fichero)
    if not os.path.isfile(ruta):
        return JSONResponse(status_code=404, content={"error": "no existe"})
    return FileResponse(ruta, filename=fichero, media_type=
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/v1/shopify/ficheros", tags=["Shopify"])
async def shopify_ficheros():
    """XLSX disponibles para descargar, del mas reciente al mas antiguo."""
    import shopify_pub as sp
    try:
        nombres = [f for f in os.listdir(sp.DIR_SALIDA)
                   if f.startswith("Kalamo_Matrixify_") and f.endswith(".xlsx")]
    except FileNotFoundError:
        nombres = []
    from datetime import datetime as _dt
    ficheros = []
    for n in sorted(nombres, reverse=True)[:20]:
        ruta = os.path.join(sp.DIR_SALIDA, n)
        ficheros.append({"nombre": n,
                         "mb": round(os.path.getsize(ruta) / 1024 / 1024, 1),
                         "cuando": _dt.fromtimestamp(
                             os.path.getmtime(ruta)).isoformat()})
    return JSONResponse(content={"ficheros": ficheros})


@app.post("/api/v1/shopify/stop", tags=["Shopify"])
async def shopify_stop():
    import shopify_pub as sp
    if sp.stop():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "No hay job de Shopify corriendo."})


# ─── Vigilante: salud del sistema ────────────────────────────────────

@app.get("/api/v1/vigilante/estado", tags=["Vigilante"])
async def vigilante_estado():
    """Ultima revision hecha y estado del cron del vigilante."""
    import vigilante
    return JSONResponse(content={
        "ultima": vigilante.get_estado(),
        "cron": vigilante.get_cron_status(),
    })


@app.post("/api/v1/vigilante/revisar", tags=["Vigilante"])
async def vigilante_revisar(
    arreglar: bool = Query(True, description="arreglar lo que sea seguro"),
):
    """
    Pasa todas las comprobaciones ahora mismo. Con arreglar=true levanta los
    crones parados y libera los locks atascados; el resto solo lo reporta.
    """
    import vigilante
    return JSONResponse(content=await vigilante.revisar(arreglar=arreglar))


@app.get("/api/v1/vigilante/historial", tags=["Vigilante"])
async def vigilante_historial(limit: int = Query(25, ge=1, le=100)):
    """Las ultimas revisiones registradas, con lo que cambio en cada una."""
    import audit_log
    return JSONResponse(content={
        "revisiones": audit_log.get_events(categoria="vigilante", limit=limit),
    })


@app.post("/api/v1/vigilante/cron/start", tags=["Vigilante"])
async def vigilante_cron_start():
    """
    Deja el vigilante corriendo. Para que arranque solo tras un reinicio:
    VIGILANTE_ENABLED=1.
    """
    import vigilante
    if vigilante.start_cron():
        return JSONResponse(content={"status": "started",
                                      "interval_s": vigilante.INTERVALO_S})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "Ya estaba corriendo o sin event loop."})


@app.post("/api/v1/vigilante/cron/stop", tags=["Vigilante"])
async def vigilante_cron_stop():
    import vigilante
    if vigilante.stop_cron():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "No estaba corriendo."})


# ─── Stock de Odoo a Shopify, por nuestra cuenta ─────────────────────

@app.post("/api/v1/shopify/stock/exportar-inventario", tags=["Shopify"])
async def shopify_stock_exportar():
    """
    Trae de Shopify el identificador de inventario de cada libro. Hace falta
    una vez antes de sincronizar: para escribir stock no vale el ISBN, hace
    falta el inventoryItemId de la variante. Tarda unos minutos.
    """
    import threading
    import shopify_stock

    if shopify_stock.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Ya hay una operacion corriendo."})
    threading.Thread(target=shopify_stock.exportar_inventario,
                     daemon=True).start()
    return JSONResponse(content={"status": "started"})


@app.post("/api/v1/shopify/stock/sincronizar", tags=["Shopify"])
async def shopify_stock_sincronizar(
    dry_run: bool = Query(True, description="empezar siempre por aqui"),
    completo: bool = Query(False, description="ignorar el marcapaginas"),
    limite: int | None = Query(None, ge=1),
):
    """
    Lleva a Shopify el stock que ha cambiado en Odoo. Con dry_run=true dice
    cuantos libros cambiaria y una muestra, sin tocar la tienda.

    La cantidad publicada es el total de Odoo sumando los catorce almacenes,
    que es lo que se puede servir.
    """
    import threading
    import sys
    import shopify_stock

    if shopify_stock.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Ya hay una sincronizacion corriendo."})

    if dry_run:
        return JSONResponse(content=await shopify_stock.sincronizar(
            dry_run=True, completo=completo, limite=limite))

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(shopify_stock.sincronizar(
                dry_run=False, completo=completo, limite=limite))
        finally:
            new_loop.close()

    threading.Thread(target=_run_in_thread, daemon=True).start()
    return JSONResponse(content={"status": "started", "completo": completo,
                                 "limite": limite})


@app.get("/api/v1/shopify/stock/estado", tags=["Shopify"])
async def shopify_stock_estado():
    """Avance o resultado de la ultima operacion de stock."""
    import shopify_stock
    import db as _db
    out = {"job": shopify_stock.get_status(),
           "cron": shopify_stock.get_cron_status()}
    try:
        conn = _db.get_connection()
        cur = conn.cursor()
        cur.execute(f"""SELECT count(*), count(inventory_item_gid),
                               max(leido_en), max(escrito_en)
                        FROM {shopify_stock.TABLA}""")
        n, con_item, leido, escrito = cur.fetchone()
        cur.execute("""SELECT ultimo_timestamp, ultima_ejecucion, items_procesados
                       FROM sync_state WHERE entidad = %s""",
                    (shopify_stock.ENTIDAD,))
        r = cur.fetchone()
        conn.close()
        out["inventario"] = {
            "libros": n, "con_identificador": con_item,
            "ultima_lectura": leido.isoformat() if leido else None,
            "ultima_escritura": escrito.isoformat() if escrito else None,
        }
        out["marcapaginas"] = {
            "hasta": str(r[0]) if r and r[0] else None,
            "ultima_corrida": r[1].isoformat() if r and r[1] else None,
            "ultimos_items": r[2] if r else None,
        } if r else {}
    except Exception as e:
        out["error"] = str(e)[:200]
    return JSONResponse(content=out)


@app.post("/api/v1/shopify/stock/cron/start", tags=["Shopify"])
async def shopify_stock_cron_start():
    """
    Deja el stock sincronizandose solo: rapida cada hora y completa una vez
    al dia de madrugada. La completa hace falta porque es la unica que
    detecta los libros que se quedaron sin existencias.

    Para que arranque solo tras un reinicio: SHOPIFY_STOCK_CRON_ENABLED=1.
    """
    import shopify_stock
    if shopify_stock.start_cron():
        return JSONResponse(content={
            "status": "started",
            "interval_s": shopify_stock.CRON_INTERVAL_S,
            "hora_completa": shopify_stock.HORA_COMPLETA})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "Ya estaba corriendo o sin event loop."})


@app.post("/api/v1/shopify/stock/cron/stop", tags=["Shopify"])
async def shopify_stock_cron_stop():
    import shopify_stock
    if shopify_stock.stop_cron():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "No estaba corriendo."})


@app.post("/api/v1/shopify/stock/parar", tags=["Shopify"])
async def shopify_stock_parar():
    import shopify_stock
    if shopify_stock.stop():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "No estaba corriendo."})


# ─── Catalogo publicable: el contrato para quien vende ───────────────

@app.get("/api/v1/catalogo-publicable", tags=["Catalogo publicable"])
async def catalogo_publicable_leer(
    desde: str | None = Query(None, description="solo lo cambiado despues de esta fecha (ISO)"),
    con_stock: bool = Query(True, description="solo lo que se puede vender"),
    limite: int = Query(1000, ge=1, le=50000),
    offset: int = Query(0, ge=0),
):
    """
    Lo que se puede vender AHORA, una fila por ISBN y ya filtrado.

    Esta es la tabla que deben leer los feeds -marketplace incluido- en vez
    de libros_proveedor, que es una tabla de trabajo sin filtrar: alli el
    stock de un proveedor pausado, mudo o con el dato rancio sigue como si
    nada. El 19/08 eran 309.517 libros de 674.642.

    Con `desde` se piden solo los cambios: sirve para refrescar sin bajar
    el catalogo entero. Las filas retiradas salen con stock 0, no
    desaparecen, para que el feed sepa que hay que darlas de baja.
    """
    import catalogo_publicable as cp
    import db as _db
    conn = _db.get_connection()
    cur = conn.cursor()
    try:
        cond, params = [], []
        if con_stock:
            cond.append("stock > 0")
        if desde:
            cond.append("actualizado_en > %s")
            params.append(desde)
        where = ("WHERE " + " AND ".join(cond)) if cond else ""
        cur.execute(f"SELECT count(*) FROM {cp.TABLA} {where}", params)
        total = cur.fetchone()[0]
        cur.execute(f"""
            SELECT isbn, titulo, stock, precio_marketplace, precio_web,
                   precio_odoo, proveedor, precio_coste, confirmado_en,
                   actualizado_en
            FROM {cp.TABLA} {where}
            ORDER BY actualizado_en DESC, isbn
            LIMIT %s OFFSET %s
        """, params + [limite, offset])
        cols = ["isbn", "titulo", "stock", "precio_marketplace", "precio_web",
                "precio_odoo", "proveedor", "precio_coste", "confirmado_en",
                "actualizado_en"]
        libros = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    return JSONResponse(content=jsonable_encoder({
        "total": total, "devueltos": len(libros),
        "limite": limite, "offset": offset, "libros": libros,
    }))


@app.post("/api/v1/catalogo-publicable/refrescar", tags=["Catalogo publicable"])
async def catalogo_publicable_refrescar(
    dry_run: bool = Query(False, description="solo contar, sin escribir"),
):
    """Reconstruye el catalogo publicable desde Odoo. Tarda unos minutos."""
    import threading
    import sys
    import catalogo_publicable as cp

    if cp.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Ya hay un refresco corriendo."})

    if dry_run:
        return JSONResponse(content=await cp.refrescar(dry_run=True))

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        nl = asyncio.new_event_loop()
        asyncio.set_event_loop(nl)
        try:
            nl.run_until_complete(cp.refrescar(dry_run=False))
        finally:
            nl.close()

    threading.Thread(target=_run_in_thread, daemon=True).start()
    return JSONResponse(content={"status": "started"})


@app.post("/api/v1/catalogo-publicable/cron/start", tags=["Catalogo publicable"])
async def catalogo_publicable_cron_start():
    """
    Deja el catalogo refrescandose solo cada hora. Sin esto, el feed de
    marketplace publica la foto del ultimo refresco manual.
    Para que arranque tras un reinicio: CATALOGO_CRON_ENABLED=1.
    """
    import catalogo_publicable as cp
    if cp.start_cron():
        return JSONResponse(content={"status": "started",
                                     "interval_s": cp.CRON_INTERVAL_S})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "Ya estaba corriendo o sin event loop."})


@app.post("/api/v1/catalogo-publicable/cron/stop", tags=["Catalogo publicable"])
async def catalogo_publicable_cron_stop():
    import catalogo_publicable as cp
    if cp.stop_cron():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "No estaba corriendo."})


@app.get("/api/v1/catalogo-publicable/estado", tags=["Catalogo publicable"])
async def catalogo_publicable_estado():
    import catalogo_publicable as cp
    import db as _db
    out = {"job": cp.get_status(), "cron": cp.get_cron_status()}
    try:
        conn = _db.get_connection()
        cur = conn.cursor()
        cur.execute(f"""SELECT count(*), count(*) FILTER (WHERE stock > 0),
                               COALESCE(sum(stock), 0), max(actualizado_en)
                        FROM {cp.TABLA}""")
        n, con, uds, ult = cur.fetchone()
        conn.close()
        out["tabla"] = {"filas": n, "vendibles": con, "unidades": int(uds or 0),
                        "actualizado": ult.isoformat() if ult else None}
    except Exception as e:
        out["error"] = str(e)[:200]
    return JSONResponse(content=out)


# ─── Auditoria de datos: el stock que decimos tener vs el que hay ────

@app.post("/api/v1/auditoria/lanzar", tags=["Auditoria"])
async def auditoria_lanzar():
    """
    Compara libro a libro el stock de cada proveedor con el de su almacen en
    Odoo. Tarda varios minutos porque lee todo el inventario; se sigue con
    /estado. No modifica nada: solo mide.
    """
    import threading
    import sys
    import auditoria

    if auditoria.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Ya hay una auditoria corriendo."})

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(auditoria.auditar())
        finally:
            new_loop.close()

    threading.Thread(target=_run_in_thread, daemon=True).start()
    return JSONResponse(content={"status": "started"})


@app.get("/api/v1/auditoria/estado", tags=["Auditoria"])
async def auditoria_estado():
    """Resultado de la ultima auditoria, o su avance si esta corriendo."""
    import auditoria
    return JSONResponse(content=auditoria.get_status())


@app.post("/api/v1/auditoria/parar", tags=["Auditoria"])
async def auditoria_parar():
    import auditoria
    if auditoria.stop():
        return JSONResponse(content={"status": "stopping"})
    return JSONResponse(status_code=400, content={
        "status": "error", "message": "No estaba corriendo."})


@app.post("/api/v1/auditoria/crear-faltantes", tags=["Auditoria"])
async def auditoria_crear_faltantes(
    dry_run: bool = Query(True, description="empezar siempre por aqui"),
    limite: int | None = Query(None, ge=1),
    scrapear: bool = Query(True, description="buscar ficha en Casa del Libro antes"),
    solo_proveedor: str | None = Query(None),
):
    """
    Crea en Odoo los libros que un proveedor manda y no estan alli, aunque
    vengan con stock 0. Sirve para que se puedan buscar por ISBN: sin stock
    no se venden igual.

    Se crean ACTIVOS a proposito. Con la regla del ciclo diario (sin precio
    -> archivado) casi todos naceran invisibles, que es justo lo que se
    quiere evitar.
    """
    import threading
    import sys
    import crear_faltantes

    if crear_faltantes.get_status().get("status") == "running":
        return JSONResponse(status_code=409, content={
            "status": "error", "message": "Ya hay una creacion corriendo."})

    if dry_run:
        return JSONResponse(content=await crear_faltantes.crear(
            dry_run=True, limite=limite, scrapear=False,
            solo_proveedor=solo_proveedor))

    def _run_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(crear_faltantes.crear(
                dry_run=False, limite=limite, scrapear=scrapear,
                solo_proveedor=solo_proveedor))
        finally:
            new_loop.close()

    threading.Thread(target=_run_in_thread, daemon=True).start()
    return JSONResponse(content={"status": "started", "limite": limite})


@app.get("/api/v1/auditoria/crear-faltantes/estado", tags=["Auditoria"])
async def auditoria_crear_faltantes_estado():
    import crear_faltantes
    return JSONResponse(content=crear_faltantes.get_status())


@app.get("/api/v1/auditoria/historial", tags=["Auditoria"])
async def auditoria_historial(limit: int = Query(20, ge=1, le=100)):
    """Auditorias anteriores, para ver si la fiabilidad mejora o empeora."""
    import audit_log
    return JSONResponse(content={
        "auditorias": audit_log.get_events(categoria="auditoria", limit=limit)})


@app.get("/api/v1/buscar-isbn", tags=["Auditoria"])
async def buscar_isbn(isbn: str = Query(..., description="ISBN o EAN, con o sin guiones")):
    """
    La vida entera de un libro en una consulta: que proveedores lo tienen y
    desde cuando, su ficha, su stock real en cada almacen de Odoo y si esta
    publicado en Shopify con inventario.

    Lo importante es el diagnostico: explica POR QUE un libro con stock no
    se puede comprar en la web, y que boton lo arregla.
    """
    import buscador_isbn
    return JSONResponse(content=await buscador_isbn.buscar(isbn))
