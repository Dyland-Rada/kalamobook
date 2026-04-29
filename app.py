import sys
import asyncio
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, Request, Form, Query, BackgroundTasks, Depends, HTTPException, status
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

security = HTTPBasic()
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "admin123")

def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
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


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
