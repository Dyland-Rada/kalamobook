"""
Bulk scraper for La Casa del Libro — crawl entire categories page by page.
Runs as a background task, reports progress, supports stop/resume.
"""

import asyncio
import db
import random
import re
from datetime import datetime
from playwright.async_api import async_playwright
from scraper import (
    BASE_URL, PROXY_URL, scrape_book, save_to_db, init_db,
    DB_NAME,
)
import sqlite3
import uuid
# ── Category catalogue ─────────────────────────────────────────────────
CATEGORIES = {
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
    "novela-contemporanea": {
        "name": "📖 Novela contemporánea",
        "url": "/libros/literatura/novela-contemporanea/121016000",
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
    "autoayuda": {
        "name": "🌱 Autoayuda y espiritualidad",
        "url": "/libros/autoayuda-y-espiritualidad/102000000",
    },
    "historia": {
        "name": "🏛️ Historia",
        "url": "/libros/historia/115000000",
    },
    "ciencias": {
        "name": "🔬 Ciencias y tecnología",
        "url": "/libros/ciencias/103000000",
    },
    "economia": {
        "name": "📊 Economía y Empresa",
        "url": "/libros/economia-y-empresa/110000000",
    },
    "cocina": {
        "name": "🍳 Cocina",
        "url": "/libros/cocina/106000000",
    },
    "infantil": {
        "name": "🧒 Infantil",
        "url": "/libros/infantil/120000000",
    },
    "narrativa-hispanoamericana": {
        "name": "🌎 Narrativa hispanoamericana",
        "url": "/libros/literatura/novela-contemporanea/narrativa-hispanoamericana/121016007",
    },
    "narrativa-espanola": {
        "name": "🇪🇸 Narrativa española",
        "url": "/libros/literatura/novela-contemporanea/narrativa-espanola/121016003",
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
            """SELECT id, title, author, editorial, isbn, price, url, image_url, timestamp, category, categoria_1, categoria_2, categoria_3
               FROM books WHERE title LIKE ? OR author LIKE ? OR isbn LIKE ?
               ORDER BY id DESC LIMIT ? OFFSET ?""",
            (like, like, like, per_page, offset),
        )
    else:
        db.execute_query(cursor, "SELECT COUNT(*) FROM books")
        total = cursor.fetchone()[0]
        db.execute_query(cursor, 
            """SELECT id, title, author, editorial, isbn, price, url, image_url, timestamp, category, categoria_1, categoria_2, categoria_3
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
        cat_keys = list(CATEGORIES.keys())
        job_category_name = "TODAS LAS CATEGORÍAS"
    else:
        if category_key not in CATEGORIES:
            return None
        cat_keys = [category_key]
        job_category_name = CATEGORIES[category_key]["name"]

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
        "errors": [],
        "max_books": max_books,
    }
    active_jobs[job_id] = job

    try:
        async with async_playwright() as p:
            launch_opts = {"headless": True}
            if PROXY_URL:
                launch_opts["proxy"] = {"server": PROXY_URL}
            browser = await p.chromium.launch(**launch_opts)

            # Page for browsing category listings
            list_page = await browser.new_page()
            await list_page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                "Accept-Language": "es-ES,es;q=0.9",
            })

            # Page for scraping individual books
            detail_page = await browser.new_page()
            await detail_page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                "Accept-Language": "es-ES,es;q=0.9",
            })

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

                while True:
                    if job["status"] == "stopped":
                        break

                    # Build paginated URL
                    sep = "&" if "?" in category_url else "?"
                    page_url = f"{category_url}{sep}page={page_num}" if page_num > 1 else category_url

                    print(f"\n[Bulk] Loading category page {page_num}: {page_url}")
                    job["current_page"] = page_num
                    job["current_book"] = f"Cargando página {page_num}..."

                    try:
                        await list_page.goto(page_url, timeout=60000, wait_until="domcontentloaded")
                        await list_page.wait_for_timeout(3000)
                    except Exception as e:
                        print(f"[Bulk] Error loading page {page_num}: {e}")
                        job["errors"].append(f"Page {page_num}: {str(e)[:100]}")
                        break

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

                    print(f"[Bulk] Found {len(links)} book links on page {page_num}")
                    
                    # ── Scrape each book on the current page ──
                    for i, book_link in enumerate(links):
                        if job["status"] == "stopped":
                            break
                        
                        book_url = book_link["url"]
                        if not book_url.startswith("http"):
                            book_url = BASE_URL + book_url

                        job["current_book"] = book_link.get("title", book_url)[:80]
                        print(f"\n[Bulk] P.{page_num} [{i+1}/{len(links)}] {job['current_book']}")

                        if _check_url_exists(book_url):
                            print(f"  → Already in DB, skipping")
                            job["books_skipped"] += 1
                        else:
                            # Scrape the book
                            try:
                                data = await scrape_book(detail_page, query=book_link.get("title", ""), direct_url=book_url)
                                if data:
                                    data["url"] = book_url
                                    data["search_query"] = book_link.get("title", "")
                                    data["category"] = cat["name"]
                                    save_to_db(data)
                                    job["books_scraped"] += 1
                                    print(f"  → Saved: {data.get('title', '?')}")
                                else:
                                    job["books_failed"] += 1
                                    print(f"  → Failed to scrape")
                            except Exception as e:
                                job["books_failed"] += 1
                                error_msg = f"{book_link.get('title', '?')}: {str(e)[:100]}"
                                job["errors"].append(error_msg)
                                print(f"  → Error: {e}")

                        job["books_found"] += 1
                        
                        if max_books and job["books_scraped"] >= max_books:
                            break

                        # Random delay between books
                        delay = random.uniform(3, 6)
                        print(f"  → Waiting {delay:.1f}s...")
                        await asyncio.sleep(delay)
                    
                    if max_books and job["books_scraped"] >= max_books:
                        print(f"[Bulk] Reached max_books ({max_books}). Stopping.")
                        break
                    
                    if job["status"] != "stopped":
                        page_num += 1
                        update_last_scraped_page(current_cat_key, page_num)
                    
                    await asyncio.sleep(random.uniform(1, 2))

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
