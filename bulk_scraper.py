"""
Bulk scraper for La Casa del Libro — crawl entire categories page by page.
Runs as a background task, reports progress, supports stop/resume.
"""

import asyncio
import db
import os
import random
import re
from datetime import datetime
from playwright.async_api import async_playwright
from scraper import (
    BASE_URL, PROXY_URL, scrape_book, save_to_db, init_db,
    DB_NAME, CHROMIUM_ARGS, _setup_page,
)
import sqlite3
import uuid

# Número de páginas Playwright scrapeando detalles de libros en paralelo.
# Configurable via variable de entorno BULK_POOL_SIZE (ej: en EasyPanel).
# Aumentar = más velocidad; disminuir si el sitio empieza a bloquear.
POOL_SIZE = int(os.environ.get("BULK_POOL_SIZE", "6"))

# Límite de páginas que se procesan por categoría en un job "all".
# Evita que una categoría muy profunda monopolice todo el job.
# 0 = sin límite (recomendado cuando se corre una categoría individual).
# Configurable via PAGES_PER_CATEGORY en EasyPanel.
# 0 = sin límite: scrapea TODAS las páginas de TODAS las categorías.
# Cambiar via variable de entorno PAGES_PER_CATEGORY si se quiere acotar.
PAGES_PER_CATEGORY = int(os.environ.get("PAGES_PER_CATEGORY", "0"))

# ── Category catalogue ─────────────────────────────────────────────────
CATEGORIES = {
    # ── Listas editoriales ──────────────────────────────────────────────
    "novedades": {
        "name": "📚 Novedades",
        "url": "/novedades-libros",
    },
    "mas-vendidos": {
        "name": "🏆 Más vendidos",
        "url": "/libros-mas-vendidos",
    },
    "recomendados": {
        "name": "⭐ Recomendados",
        "url": "/libros-recomendados",
    },
    # ── Literatura ──────────────────────────────────────────────────────
    "literatura": {
        "name": "📖 Literatura (general)",
        "url": "/libros/literatura/121000000",
    },
    "novela-contemporanea": {
        "name": "📖 Novela contemporánea",
        "url": "/libros/literatura/novela-contemporanea/121016000",
    },
    "narrativa-espanola": {
        "name": "🇪🇸 Narrativa española",
        "url": "/libros/literatura/novela-contemporanea/narrativa-espanola/121016003",
    },
    "narrativa-hispanoamericana": {
        "name": "🌎 Narrativa hispanoamericana",
        "url": "/libros/literatura/novela-contemporanea/narrativa-hispanoamericana/121016007",
    },
    "narrativa-anglosajona": {
        "name": "🇬🇧 Narrativa anglosajona",
        "url": "/libros/literatura/novela-contemporanea/narrativa-anglosajona/121016001",
    },
    "narrativa-italiana": {
        "name": "🇮🇹 Narrativa italiana",
        "url": "/libros/literatura/novela-contemporanea/narrativa-italiana/121016005",
    },
    "narrativa-francesa": {
        "name": "🇫🇷 Narrativa francesa",
        "url": "/libros/literatura/novela-contemporanea/narrativa-francesa/121016004",
    },
    "novela-negra": {
        "name": "🔪 Novela negra",
        "url": "/libros/literatura/novela-negra/121014000",
    },
    "novela-historica": {
        "name": "🏰 Novela histórica",
        "url": "/libros/literatura/novela-historica/121013000",
    },
    "novela-romantica": {
        "name": "💕 Novela romántica y erótica",
        "url": "/libros/literatura/novela-romantica-y-erotica/121015000",
    },
    "ciencia-ficcion": {
        "name": "🚀 Ciencia ficción",
        "url": "/libros/literatura/novela-de-ciencia-ficcion/121008000",
    },
    "narrativa-fantastica": {
        "name": "🧙 Narrativa fantástica",
        "url": "/libros/literatura/narrativa-fantastica/121012000",
    },
    "terror": {
        "name": "👻 Novela de terror",
        "url": "/libros/literatura/novela-de-terror/121010000",
    },
    "clasicos": {
        "name": "📜 Clásicos",
        "url": "/libros/literatura/clasicos/121001000",
    },
    "poesia": {
        "name": "✒️ Poesía",
        "url": "/libros/literatura/poesia/121006000",
    },
    "teatro": {
        "name": "🎭 Teatro",
        "url": "/libros/literatura/teatro/121007000",
    },
    "cuentos-relatos": {
        "name": "📝 Cuentos y relatos",
        "url": "/libros/literatura/cuentos-y-relatos/121003000",
    },
    "humor": {
        "name": "😂 Humor",
        "url": "/libros/literatura/humor/121005000",
    },
    "novela-aventura": {
        "name": "⚔️ Novela de aventuras",
        "url": "/libros/literatura/novela-de-aventuras/121011000",
    },
    # ── No ficción / Humanidades ────────────────────────────────────────
    "autoayuda": {
        "name": "🌱 Autoayuda y espiritualidad",
        "url": "/libros/autoayuda-y-espiritualidad/102000000",
    },
    "historia": {
        "name": "🏛️ Historia",
        "url": "/libros/historia/115000000",
    },
    "historia-espana": {
        "name": "🏛️ Historia de España",
        "url": "/libros/historia/historia-de-espana/115002000",
    },
    "historia-universal": {
        "name": "🌍 Historia universal",
        "url": "/libros/historia/historia-universal/115003000",
    },
    "biografia": {
        "name": "👤 Biografías y memorias",
        "url": "/libros/biografia/104000000",
    },
    "filosofia": {
        "name": "🧠 Filosofía",
        "url": "/libros/filosofia/112000000",
    },
    "religion": {
        "name": "✝️ Religión",
        "url": "/libros/religion/128000000",
    },
    "politica": {
        "name": "🏛️ Política y sociedad",
        "url": "/libros/politica-y-sociedad/127000000",
    },
    "psicologia": {
        "name": "🧩 Psicología",
        "url": "/libros/psicologia/125000000",
    },
    # ── Ciencias y tecnología ───────────────────────────────────────────
    "ciencias": {
        "name": "🔬 Ciencias y tecnología",
        "url": "/libros/ciencias/103000000",
    },
    "matematicas": {
        "name": "📐 Matemáticas",
        "url": "/libros/ciencias/matematicas/103008000",
    },
    "fisica": {
        "name": "⚛️ Física",
        "url": "/libros/ciencias/fisica/103006000",
    },
    "biologia": {
        "name": "🧬 Biología",
        "url": "/libros/ciencias/biologia/103002000",
    },
    "informatica": {
        "name": "💻 Informática",
        "url": "/libros/informatica/116000000",
    },
    "programacion": {
        "name": "👨‍💻 Programación",
        "url": "/libros/informatica/programacion/116008000",
    },
    # ── Economía y empresa ──────────────────────────────────────────────
    "economia": {
        "name": "📊 Economía y Empresa",
        "url": "/libros/economia-y-empresa/110000000",
    },
    "management": {
        "name": "📈 Management y liderazgo",
        "url": "/libros/economia-y-empresa/management-y-liderazgo/110004000",
    },
    "marketing": {
        "name": "📣 Marketing",
        "url": "/libros/economia-y-empresa/marketing-y-comunicacion/110005000",
    },
    # ── Salud y deporte ─────────────────────────────────────────────────
    "salud": {
        "name": "🏥 Salud",
        "url": "/libros/salud/130000000",
    },
    "medicina": {
        "name": "👨‍⚕️ Medicina",
        "url": "/libros/medicina/122000000",
    },
    "deporte": {
        "name": "⚽ Deportes",
        "url": "/libros/deportes/108000000",
    },
    # ── Arte y cultura ──────────────────────────────────────────────────
    "arte": {
        "name": "🎨 Arte",
        "url": "/libros/arte/101000000",
    },
    "arquitectura": {
        "name": "🏗️ Arquitectura y diseño",
        "url": "/libros/arquitectura-y-diseno/100000000",
    },
    "musica": {
        "name": "🎵 Música",
        "url": "/libros/musica/123000000",
    },
    "fotografia": {
        "name": "📷 Fotografía",
        "url": "/libros/fotografia/113000000",
    },
    "cine": {
        "name": "🎬 Cine y medios",
        "url": "/libros/cine-y-medios/105000000",
    },
    # ── Hogar y ocio ────────────────────────────────────────────────────
    "cocina": {
        "name": "🍳 Cocina",
        "url": "/libros/cocina/106000000",
    },
    "viajes": {
        "name": "✈️ Viajes",
        "url": "/libros/viajes/133000000",
    },
    "naturaleza": {
        "name": "🌿 Naturaleza",
        "url": "/libros/naturaleza/124000000",
    },
    "manualidades": {
        "name": "✂️ Manualidades y hobbies",
        "url": "/libros/manualidades-y-hobbies/118000000",
    },
    # ── Infantil y juvenil ──────────────────────────────────────────────
    "infantil": {
        "name": "🧒 Infantil",
        "url": "/libros/infantil/120000000",
    },
    "infantil-0-2": {
        "name": "👶 Infantil 0-2 años",
        "url": "/libros/infantil/de-0-a-2-anos/120001000",
    },
    "infantil-3-5": {
        "name": "🧸 Infantil 3-5 años",
        "url": "/libros/infantil/de-3-a-5-anos/120002000",
    },
    "infantil-6-9": {
        "name": "📚 Infantil 6-9 años",
        "url": "/libros/infantil/de-6-a-9-anos/120003000",
    },
    "infantil-10-12": {
        "name": "📗 Infantil 10-12 años",
        "url": "/libros/infantil/de-10-a-12-anos/120004000",
    },
    "juvenil": {
        "name": "🧑 Juvenil",
        "url": "/libros/juvenil/132000000",
    },
    # ── Educación / Referencia ──────────────────────────────────────────
    "educacion": {
        "name": "🎓 Educación",
        "url": "/libros/educacion/109000000",
    },
    "idiomas": {
        "name": "🌐 Idiomas",
        "url": "/libros/idiomas/117000000",
    },
    "ingles": {
        "name": "🇬🇧 Inglés",
        "url": "/libros/idiomas/ingles/117001000",
    },
    "comics": {
        "name": "💥 Cómics y manga",
        "url": "/libros/comics-y-manga/107000000",
    },
    "manga": {
        "name": "🇯🇵 Manga",
        "url": "/libros/comics-y-manga/manga/107002000",
    },
    "derecho": {
        "name": "⚖️ Derecho",
        "url": "/libros/derecho/131000000",
    },
    "geopolitica": {
        "name": "🗺️ Geografía",
        "url": "/libros/geografia/114000000",
    },
    # ── Categorías principales faltantes (de "Todas las temáticas") ─────
    "ciencias-humanas": {
        "name": "📚 Ciencias Humanas",
        "url": "/libros/ciencias-humanas/111000000",
    },
    "filologia": {
        "name": "📝 Filología",
        "url": "/libros/filologia/112000000",
    },
    "ingenieria": {
        "name": "🔧 Ingeniería",
        "url": "/libros/ingenieria/119000000",
    },
    "oposiciones": {
        "name": "📋 Oposiciones",
        "url": "/libros/oposiciones/126000000",
    },
    "literatura-otros-idiomas": {
        "name": "🌍 Literatura en otros idiomas",
        "url": "/libros/literatura-en-otros-idiomas/129000000",
    },
    "comics-manga-infantil": {
        "name": "💥 Cómics y manga infantil y juvenil",
        "url": "/libros/comics-y-manga-infantil-y-juvenil/134000000",
    },
    "libros-texto-formacion": {
        "name": "📖 Libros de Texto y Formación",
        "url": "/libros/libros-de-texto-y-formacion/135000000",
    },
    "guias-viaje": {
        "name": "✈️ Guías de viaje",
        "url": "/libros/guias-de-viaje/136000000",
    },
    "ocio-deporte": {
        "name": "🏃 Ocio y deporte",
        "url": "/libros/ocio-y-deporte/137000000",
    },
    "psicologia-pedagogia": {
        "name": "🧠 Psicología y Pedagogía",
        "url": "/libros/psicologia-y-pedagogia/125000000",
    },
    "salud-dietas": {
        "name": "🥗 Salud y Dietas",
        "url": "/libros/salud-y-dietas/138000000",
    },
}

# ── Active jobs state ──────────────────────────────────────────────────
active_jobs: dict = {}


def _check_url_exists(url: str) -> bool:
    """Check if a book URL is already stored in the database."""
    conn = db.get_connection()
    cursor = conn.cursor()
    db.execute_query(cursor, "SELECT 1 FROM books WHERE url = ? LIMIT 1", (url,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists


def _load_existing_urls() -> set:
    """Load all book URLs from DB into an in-memory set for O(1) duplicate checks."""
    conn = db.get_connection()
    cursor = conn.cursor()
    db.execute_query(cursor, "SELECT url FROM books")
    urls = set(row[0] for row in cursor.fetchall())
    conn.close()
    print(f"[Bulk] Loaded {len(urls)} existing URLs into cache")
    return urls


async def _scrape_one_book(book_link: dict, page_queue: asyncio.Queue,
                           cat_name: str, job: dict,
                           existing_urls: set, max_books: int | None):
    """
    Scrape one book using a page borrowed from the pool.
    Designed to run concurrently via asyncio.gather().
    """
    if job["status"] == "stopped":
        return
    if max_books and job["books_scraped"] >= max_books:
        return

    book_url = book_link["url"]
    if not book_url.startswith("http"):
        book_url = BASE_URL + book_url

    # Fast in-memory duplicate check
    if book_url in existing_urls:
        job["books_skipped"] += 1
        job["books_found"] += 1
        return

    # Claim the URL now to prevent parallel workers from double-scraping it
    existing_urls.add(book_url)

    page = await page_queue.get()
    try:
        if job["status"] == "stopped" or (max_books and job["books_scraped"] >= max_books):
            existing_urls.discard(book_url)
            return

        job["current_book"] = book_link.get("title", book_url)[:80]
        print(f"\n[Bulk] Scraping: {job['current_book']}")

        data = await scrape_book(page, query=book_link.get("title", ""), direct_url=book_url)
        if data:
            data["url"] = book_url
            data["search_query"] = book_link.get("title", "")
            data["category"] = cat_name
            save_to_db(data)
            job["books_scraped"] += 1
            print(f"  → Saved: {data.get('title', '?')}")
        else:
            job["books_failed"] += 1
            existing_urls.discard(book_url)
            print(f"  → Failed to scrape")

        delay = random.uniform(0.5, 1.5)
        print(f"  → Waiting {delay:.1f}s...")
        await asyncio.sleep(delay)

    except Exception as e:
        job["books_failed"] += 1
        existing_urls.discard(book_url)
        error_msg = f"{book_link.get('title', '?')}: {str(e)[:100]}"
        job["errors"].append(error_msg)
        print(f"  → Error: {e}")
    finally:
        await page_queue.put(page)

    job["books_found"] += 1


def get_all_books(page: int = 1, per_page: int = 20, search: str = ""):
    """Retrieve books from the DB with pagination and optional search."""
    conn = db.get_connection()
    cursor = conn.cursor()

    offset = (page - 1) * per_page

    if search:
        like = f"%{search}%"
        db.execute_query(cursor, 
            "SELECT COUNT(*) FROM books WHERE title LIKE ? OR author LIKE ? OR isbn LIKE ?",
            (like, like, like),
        )
        total = cursor.fetchone()[0]
        db.execute_query(cursor, 
            """SELECT id, title, author, editorial, isbn, price, url, image_url, timestamp, category,
                      categoria_1, categoria_2, categoria_3, categoria_4, categoria_5
               FROM books WHERE title LIKE ? OR author LIKE ? OR isbn LIKE ?
               ORDER BY id DESC LIMIT ? OFFSET ?""",
            (like, like, like, per_page, offset),
        )
    else:
        db.execute_query(cursor, "SELECT COUNT(*) FROM books")
        total = cursor.fetchone()[0]
        db.execute_query(cursor, 
            """SELECT id, title, author, editorial, isbn, price, url, image_url, timestamp, category,
                      categoria_1, categoria_2, categoria_3, categoria_4, categoria_5
               FROM books ORDER BY id DESC LIMIT ? OFFSET ?""",
            (per_page, offset),
        )

    rows = []
    for row in cursor.fetchall():
        d = dict(row)
        if 'timestamp' in d and isinstance(d['timestamp'], datetime):
            d['timestamp'] = d['timestamp'].isoformat()
        rows.append(d)
    
    conn.close()

    return {
        "books": rows,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    }


def get_books_count() -> int:
    """Return total number of books in the database."""
    conn = db.get_connection()
    cursor = conn.cursor()
    db.execute_query(cursor, "SELECT COUNT(*) FROM books")
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_last_scraped_page(category_key: str) -> int:
    """Get the last scraped page for a category (auto-resume)."""
    conn = db.get_connection()
    cursor = conn.cursor()
    db.execute_query(cursor, "SELECT last_page FROM scrape_progress WHERE category_key = ?", (category_key,))
    row = cursor.fetchone()
    
    if row:
        conn.close()
        return row[0]
    
    # Try to auto-detect progress from existing books
    cat_name = CATEGORIES.get(category_key, {}).get("name")
    if cat_name:
        db.execute_query(cursor, "SELECT COUNT(*) FROM books WHERE category = ?", (cat_name,))
        count_row = cursor.fetchone()
        count = count_row[0] if count_row else 0
        
        # Assume ~20 books per page. Back up 2 pages just to ensure no gaps
        guessed_page = max(1, (count // 20) - 2)
        
        if guessed_page > 1:
            db.execute_query(cursor, "INSERT INTO scrape_progress (category_key, last_page) VALUES (?, ?)", (category_key, guessed_page))
            conn.commit()
            conn.close()
            return guessed_page

    conn.close()
    return 1


def update_last_scraped_page(category_key: str, page: int):
    """Update the last scraped page for a category."""
    conn = db.get_connection()
    cursor = conn.cursor()
    db.execute_query(cursor, "SELECT category_key FROM scrape_progress WHERE category_key = ?", (category_key,))
    if cursor.fetchone():
        db.execute_query(cursor, "UPDATE scrape_progress SET last_page = ? WHERE category_key = ?", (page, category_key))
    else:
        db.execute_query(cursor, "INSERT INTO scrape_progress (category_key, last_page) VALUES (?, ?)", (category_key, page))
    conn.commit()
    conn.close()


async def _extract_book_links(page) -> list[dict]:
    """
    Extract all book links from the current category listing page.
    Returns list of {url, title} dicts.
    """
    links = await page.evaluate("""
        () => {
            const results = [];
            const seen = new Set();

            // Look for all links whose href contains /libro- or /ebook-
            document.querySelectorAll('a[href*="/libro-"]').forEach(a => {
                const href = a.getAttribute('href');
                if (!href || seen.has(href)) return;
                seen.add(href);

                // Try to get a title from the link text or nearby elements
                let title = a.innerText.trim();
                if (!title || title.length < 2) {
                    const img = a.querySelector('img');
                    if (img) title = img.alt || '';
                }
                title = title.split('\\n')[0].trim();

                if (href && title && title.length > 1) {
                    results.push({ url: href, title: title });
                }
            });

            return results;
        }
    """)
    return links or []


async def _extract_max_page(page) -> int | None:
    """
    Extract the maximum page number from the paginator on a category listing page.
    Looks for pagination links/buttons with numeric text (e.g. 1, 2, ... 71825).
    Returns the highest page number found, or None if no paginator detected.
    """
    max_page = await page.evaluate("""
        () => {
            let maxNum = 0;

            // Strategy 1: Look for pagination links with numeric text
            const paginationSelectors = [
                'nav[aria-label*="paginat"] a',
                'nav[aria-label*="Paginat"] a',
                '.pagination a',
                '.pager a',
                '[class*="pagination"] a',
                '[class*="Pagination"] a',
                '[class*="pager"] a',
                'ul.pagination li a',
                'a[href*="page="]',
            ];

            for (const sel of paginationSelectors) {
                document.querySelectorAll(sel).forEach(el => {
                    const text = el.innerText.trim();
                    const num = parseInt(text, 10);
                    if (!isNaN(num) && num > maxNum) {
                        maxNum = num;
                    }
                });
            }

            // Strategy 2: Look for "last page" buttons with page number in href
            document.querySelectorAll('a[href*="page="]').forEach(a => {
                const match = a.getAttribute('href').match(/page=(\\d+)/);
                if (match) {
                    const num = parseInt(match[1], 10);
                    if (num > maxNum) maxNum = num;
                }
            });

            // Strategy 3: Look for text patterns like "de 71825" or "/ 71825"
            const body = document.body.innerText;
            const patterns = [
                /de\\s+(\\d{2,})\\s*p[áa]gina/i,
                /p[áa]gina\\s+\\d+\\s+de\\s+(\\d+)/i,
                /(\\d{2,})\\s*p[áa]ginas/i,
            ];
            for (const pat of patterns) {
                const m = body.match(pat);
                if (m) {
                    const num = parseInt(m[1], 10);
                    if (num > maxNum) maxNum = num;
                }
            }

            return maxNum > 0 ? maxNum : null;
        }
    """)
    return max_page


async def discover_categories() -> list[dict]:
    """
    Navigate to casadellibro.com/libros and dynamically discover all categories
    from the "Todas las temáticas de libros" section.
    Returns list of {name, url, book_count} dicts.
    Updates CATEGORIES dict with any newly discovered categories.
    """
    discovered = []
    print("[Discover] Navigating to /libros to discover categories...")

    try:
        async with async_playwright() as p:
            launch_opts = {"headless": True, "args": CHROMIUM_ARGS}
            if PROXY_URL:
                launch_opts["proxy"] = {"server": PROXY_URL}
            browser = await p.chromium.launch(**launch_opts)
            page = await browser.new_page()
            await _setup_page(page)

            await page.goto(f"{BASE_URL}/libros", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # Extract all category links from the "Todas las temáticas" section
            raw = await page.evaluate("""
                () => {
                    const results = [];
                    const seen = new Set();

                    // Find all links under the "Todas las temáticas" section
                    // These are links matching /libros/CATEGORY_NAME/CODE pattern
                    document.querySelectorAll('a[href]').forEach(a => {
                        const href = a.getAttribute('href');
                        if (!href) return;

                        // Match pattern: /libros/category-name/NUMERIC_CODE
                        const match = href.match(/^\\/libros\\/([\\w-]+)\\/?(\\d{9})?$/);
                        if (!match) return;
                        if (seen.has(href)) return;
                        seen.add(href);

                        const text = a.innerText.trim();
                        if (!text || text.length < 2) return;

                        // Extract book count from parentheses: "Literatura (280346)"
                        const countMatch = text.match(/\\((\\d+)\\)/);
                        const bookCount = countMatch ? parseInt(countMatch[1], 10) : 0;
                        const name = text.replace(/\\s*\\(\\d+\\)/, '').trim();

                        results.push({
                            name: name,
                            url: href,
                            book_count: bookCount
                        });
                    });

                    return results;
                }
            """)

            await browser.close()

            if raw:
                discovered = raw
                print(f"[Discover] Found {len(discovered)} categories from the site")

                # Build a slug-to-key map from existing CATEGORIES for matching
                # e.g. "informatica" -> "informatica", "comics-y-manga" -> "comics"
                slug_to_keys: dict[str, list[str]] = {}
                for key, val in CATEGORIES.items():
                    # Extract slug from URL: /libros/informatica/116000000 -> "informatica"
                    parts = val["url"].strip("/").split("/")
                    if len(parts) >= 2:
                        slug = parts[1]  # first path segment after /libros/
                        slug_to_keys.setdefault(slug, []).append(key)

                new_count = 0
                updated_count = 0
                for cat in discovered:
                    disc_parts = cat["url"].strip("/").split("/")
                    disc_slug = disc_parts[1] if len(disc_parts) >= 2 else ""

                    if disc_slug in slug_to_keys:
                        # Category exists — update URL if the site has a different code
                        for existing_key in slug_to_keys[disc_slug]:
                            if CATEGORIES[existing_key]["url"] != cat["url"]:
                                old_url = CATEGORIES[existing_key]["url"]
                                CATEGORIES[existing_key]["url"] = cat["url"]
                                updated_count += 1
                                print(f"[Discover] URL update: {existing_key}: {old_url} → {cat['url']}")
                    else:
                        # Truly new category — add it
                        key = f"auto-{disc_slug}"
                        CATEGORIES[key] = {
                            "name": f"🔍 {cat['name']}",
                            "url": cat["url"],
                        }
                        slug_to_keys.setdefault(disc_slug, []).append(key)
                        new_count += 1
                        print(f"[Discover]   NEW: {cat['name']} → {cat['url']} ({cat['book_count']} libros)")

                print(f"[Discover] Result: {new_count} new, {updated_count} URLs updated, {len(discovered)} total from site")

            else:
                print("[Discover] No categories found on page")

    except Exception as e:
        print(f"[Discover] Error: {e}")

    return discovered




async def bulk_scrape(category_key: str, max_books: int | None = None):
    """
    Main bulk scraping coroutine.
    Crawls a category, collects book links, then scrapes each one.
    Updates active_jobs[job_id] with live progress.
    """
    job_id = str(uuid.uuid4())[:8]
    cat_keys = []
    job_category_name = ""

    if category_key == "all":
        # Prioridad: empezar por las categorias con mas titulos populares
        # Asi la BD tiene libros relevantes rapido antes de pasar a categorias nicho
        PRIORITY_FIRST = ["mas-vendidos", "recomendados", "novedades"]
        remaining = [k for k in CATEGORIES.keys() if k not in PRIORITY_FIRST]
        cat_keys = PRIORITY_FIRST + remaining
        job_category_name = "TODAS LAS CATEGORÍAS"
        # 0 = sin límite: scrapea absolutamente todas las páginas de cada categoría
        pages_cap = PAGES_PER_CATEGORY  # 0 = ilimitado
    else:
        if category_key not in CATEGORIES:
            return None
        cat_keys = [category_key]
        job_category_name = CATEGORIES[category_key]["name"]
        # Sin limite cuando se scrape una categoria individual
        pages_cap = PAGES_PER_CATEGORY  # 0 = ilimitado

    init_db()

    job = {
        "id": job_id,
        "category": job_category_name,
        "category_key": category_key,
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "books_found": 0,
        "books_scraped": 0,
        "books_skipped": 0,
        "books_failed": 0,
        "current_book": "",
        "current_page": 1,
        "total_pages": None,
        "errors": [],
        "max_books": max_books,
    }
    active_jobs[job_id] = job

    try:
        async with async_playwright() as p:
            launch_opts = {"headless": True, "args": CHROMIUM_ARGS}
            if PROXY_URL:
                launch_opts["proxy"] = {"server": PROXY_URL}
            browser = await p.chromium.launch(**launch_opts)

            # Page for browsing category listings
            list_page = await browser.new_page()
            await _setup_page(list_page)

            # Pool of pages for parallel book detail scraping
            page_queue: asyncio.Queue = asyncio.Queue()
            for _ in range(POOL_SIZE):
                dp = await browser.new_page()
                await _setup_page(dp)
                await page_queue.put(dp)

            # Load all existing URLs into memory once — avoids per-book DB queries
            existing_urls = _load_existing_urls()

            for current_cat_key in cat_keys:
                if job["status"] == "stopped":
                    break

                cat = CATEGORIES[current_cat_key]
                category_url = BASE_URL + cat["url"]
                if category_key == "all":
                    job["category"] = f"Todas ({cat['name']})"

                # ── Restore Page Progress ──
                start_page = get_last_scraped_page(current_cat_key)
                page_num = start_page
                print(f"\n[Bulk] Resuming {cat['name']} from page {page_num}")

                previous_page_urls = []
                max_page_num = None  # Will be detected from paginator

                while True:
                    if job["status"] == "stopped":
                        break

                    # Check if we've exceeded the detected max page
                    if max_page_num and page_num > max_page_num:
                        print(f"[Bulk] Reached max page {max_page_num} for {cat['name']}. Category complete.")
                        break

                    # Build paginated URL
                    sep = "&" if "?" in category_url else "?"
                    page_url = f"{category_url}{sep}page={page_num}" if page_num > 1 else category_url

                    print(f"\n[Bulk] Loading category page {page_num}{f'/{max_page_num}' if max_page_num else ''}: {page_url}")
                    job["current_page"] = page_num
                    job["current_book"] = f"Cargando página {page_num}{f'/{max_page_num}' if max_page_num else ''}..."

                    try:
                        await list_page.goto(page_url, timeout=60000, wait_until="domcontentloaded")
                        await list_page.wait_for_timeout(1500)
                    except Exception as e:
                        print(f"[Bulk] Error loading page {page_num}: {e}")
                        job["errors"].append(f"Page {page_num}: {str(e)[:100]}")
                        break

                    # Detect total pages from paginator on first load of this category
                    if max_page_num is None:
                        detected = await _extract_max_page(list_page)
                        if detected and detected > 1:
                            max_page_num = detected
                            job["total_pages"] = max_page_num
                            print(f"[Bulk] Detected {max_page_num} total pages for {cat['name']}")

                    links = await _extract_book_links(list_page)

                    if not links:
                        print(f"[Bulk] No books found on page {page_num}. Ending category.")
                        break

                    # Detect pagination limit: if the site returns exactly the same fallback list instead of an empty page
                    current_page_urls = [link["url"] for link in links]
                    if current_page_urls == previous_page_urls:
                        print(f"[Bulk] Page {page_num} returned the exact same books as the previous page! End of category reached.")
                        break
                    previous_page_urls = current_page_urls

                    print(f"[Bulk] Found {len(links)} book links on page {page_num} — scraping {POOL_SIZE} in parallel")

                    # ── Scrape all books on this page in parallel ──
                    tasks = [
                        _scrape_one_book(book_link, page_queue, cat["name"], job, existing_urls, max_books)
                        for book_link in links
                    ]
                    await asyncio.gather(*tasks)

                    if max_books and job["books_scraped"] >= max_books:
                        print(f"[Bulk] Reached max_books ({max_books}). Stopping.")
                        break

                    # Limite de paginas por categoria (modo 'all')
                    pages_done_this_run = page_num - start_page + 1
                    if pages_cap > 0 and pages_done_this_run >= pages_cap:
                        print(f"[Bulk] Limite de {pages_cap} pags/categoria alcanzado en {cat['name']}. Continuara en el proximo job.")
                        break

                    if job["status"] != "stopped":
                        page_num += 1
                        update_last_scraped_page(current_cat_key, page_num)

                    await asyncio.sleep(random.uniform(0.5, 1.0))

            await browser.close()

        # Mark job as completed
        if job["status"] != "stopped":
            job["status"] = "completed"
        job["finished_at"] = datetime.now().isoformat()

        print(f"\n[Bulk] Job {job_id} finished: "
              f"{job['books_scraped']} scraped, "
              f"{job['books_skipped']} skipped, "
              f"{job['books_failed']} failed")

    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"Fatal: {str(e)[:200]}")
        print(f"[Bulk] Fatal error in job {job_id}: {e}")

    return job_id


def stop_job(job_id: str) -> bool:
    """Stop a running scrape job."""
    if job_id in active_jobs and active_jobs[job_id]["status"] == "running":
        active_jobs[job_id]["status"] = "stopped"
        return True
    return False


def get_job_status(job_id: str) -> dict | None:
    """Get current status of a scrape job."""
    return active_jobs.get(job_id)


def get_categories() -> list[dict]:
    """Return list of available categories."""
    cats = [{"key": "all", "name": "📚 TODAS LAS CATEGORÍAS (Extensivo)", "url": ""}]
    cats.extend([
        {"key": k, "name": v["name"], "url": v["url"]}
        for k, v in CATEGORIES.items()
    ])
    return cats
