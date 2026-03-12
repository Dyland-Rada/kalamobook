import pandas as pd
import asyncio
from playwright.async_api import async_playwright
import random
import os
import re
from datetime import datetime
from bs4 import BeautifulSoup
import db

# Configuration
DB_NAME = 'books.db'
INPUT_FILE = 'librosbuscar.xlsx'
OUTPUT_REPORT = 'reporte_libros.xlsx'
BASE_URL = 'https://www.casadellibro.com'  # Spanish version (EUR)
PROXY_URL = os.environ.get('PROXY_URL', '')  # e.g. http://user:pass@host:port


def init_db():
    """Initialize the SQLite database with extended fields."""
    conn = db.get_connection()
    cursor = conn.cursor()
    id_column = "id SERIAL PRIMARY KEY" if db.IS_POSTGRES else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    datetime_column = "timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP" if db.IS_POSTGRES else "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP"
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS books (
            {id_column},
            search_query TEXT,
            title TEXT,
            author TEXT,
            editorial TEXT,
            isbn TEXT,
            price TEXT,
            original_price TEXT,
            discount TEXT,
            description TEXT,
            translator TEXT,
            illustrator TEXT,
            language TEXT,
            pages TEXT,
            reading_time TEXT,
            binding TEXT,
            release_date TEXT,
            edition_year TEXT,
            edition_place TEXT,
            collection TEXT,
            height TEXT,
            width TEXT,
            weight TEXT,
            origin TEXT,
            url TEXT,
            image_url TEXT,
            category TEXT,
            {datetime_column}
        )
    ''')
    
    conn.commit()
    
    # Apply schema mutation for existing databases
    try:
        db.execute_query(cursor, 'ALTER TABLE books ADD COLUMN category TEXT')
        conn.commit()
    except Exception:
        # Both sqlite3.OperationalError and psycopg2.errors.DuplicateColumn can be raised
        conn.rollback()
    db.execute_query(cursor, 'CREATE INDEX IF NOT EXISTS idx_search_query ON books(search_query)')
    conn.commit()
    conn.close()


def check_in_db(search_query):
    """Check if a search query has already been processed."""
    conn = db.get_connection()
    cursor = conn.cursor()
    
    query = '''
        SELECT title, author, editorial, isbn, price, original_price, discount,
               description, translator, illustrator, language, pages, reading_time,
               binding, release_date, edition_year, edition_place, collection,
               height, width, weight, origin, url, image_url
        FROM books WHERE search_query = ?
    '''
    db.execute_query(cursor, query, (search_query,))
    result = cursor.fetchone()
    conn.close()
    return result


def save_to_db(data):
    """Save scraped data to the database."""
    conn = db.get_connection()
    cursor = conn.cursor()
    query = '''
        INSERT INTO books (
            search_query, title, author, editorial, isbn, price, original_price,
            discount, description, translator, illustrator, language, pages,
            reading_time, binding, release_date, edition_year, edition_place,
            collection, height, width, weight, origin, url, image_url, category
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    '''
    db.execute_query(cursor, query, (
        data.get('search_query', ''),
        data.get('title', ''),
        data.get('author', ''),
        data.get('editorial', ''),
        data.get('isbn', ''),
        data.get('price', ''),
        data.get('original_price', ''),
        data.get('discount', ''),
        data.get('description', ''),
        data.get('translator', ''),
        data.get('illustrator', ''),
        data.get('language', ''),
        data.get('pages', ''),
        data.get('reading_time', ''),
        data.get('binding', ''),
        data.get('release_date', ''),
        data.get('edition_year', ''),
        data.get('edition_place', ''),
        data.get('collection', ''),
        data.get('height', ''),
        data.get('width', ''),
        data.get('weight', ''),
        data.get('origin', ''),
        data.get('url', ''),
        data.get('image_url', ''),
        data.get('category', ''),
    ))
    conn.commit()
    conn.close()


async def get_book_data(query):
    """
    Standalone function to scrape a single book.
    Manages the browser lifecycle internally.
    """
    async with async_playwright() as p:
        launch_opts = {'headless': True}
        if PROXY_URL:
            launch_opts['proxy'] = {'server': PROXY_URL}
        browser = await p.chromium.launch(**launch_opts)
        page = await browser.new_page()
        await page.set_extra_http_headers({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Accept-Language': 'es-ES,es;q=0.9',
        })
        try:
            data = await scrape_book(page, query)
            return data
        finally:
            await browser.close()


async def extract_text(page, selectors, default=""):
    """Try multiple selectors and return the first non-empty result."""
    for selector in selectors:
        try:
            el = page.locator(selector).first
            if await el.count() > 0:
                text = await el.inner_text(timeout=3000)
                text = text.strip()
                if text:
                    return text
        except:
            continue
    return default


async def extract_ficha_tecnica(page):
    """
    Extract all key-value pairs from the 'Ficha tecnica' section.
    Uses JavaScript to scan the page text for known patterns.
    Returns a dict with all found fields.
    """
    ficha = {}

    try:
        result = await page.evaluate('''
            () => {
                const data = {};

                // Strategy 1: definition-list dt/dd pattern
                document.querySelectorAll('dt').forEach(dt => {
                    const key = dt.innerText.trim().replace(/:$/, '').trim();
                    if (!key || key.length > 60) return;
                    const dd = dt.nextElementSibling;
                    if (dd && dd.tagName === 'DD') {
                        data[key] = dd.innerText.trim();
                    }
                });

                // Strategy 2: generic two-column rows inside any ficha/spec section
                const sections = document.querySelectorAll(
                    'section, div[class*="spec"], div[class*="ficha"], div[class*="technical"], div[class*="detail"]'
                );
                sections.forEach(section => {
                    const rows = section.querySelectorAll('li, tr, [class*="row"], [class*="item"]');
                    rows.forEach(row => {
                        const children = Array.from(row.querySelectorAll('span, div, td, p, strong, b, [class*="label"], [class*="value"]'));
                        if (children.length >= 2) {
                            const key = children[0].innerText.trim().replace(/:$/, '').trim();
                            const val = children[1].innerText.trim();
                            if (key && val && key.length < 60 && !data[key]) {
                                data[key] = val;
                            }
                        }
                    });
                });

                // Strategy 3: Regex scan of all page text for known Spanish field names
                const allText = document.body.innerText;
                const patterns = [
                    ['ISBN', /ISBN:\\s*([0-9X\\-]{10,17})/i],
                    ['Paginas', /N[\\u00fa\\u00fc]mero de p[\\u00e1a]ginas:\\s*(\\d+)/i],
                    ['Editorial', /Editorial:\\s*([^\\n]+)/i],
                    ['Idioma', /Idioma:\\s*([^\\n]+)/i],
                    ['Encuadernacion', /Encuadernaci[\\u00f3o]n:\\s*([^\\n]+)/i],
                    ['Fecha de lanzamiento', /Fecha de lanzamiento:\\s*([^\\n]+)/i],
                    ['Coleccion', /Colecci[\\u00f3o]n:\\s*([^\\n]+)/i],
                    ['Peso', /Peso:\\s*([^\\n]+)/i],
                    ['Alto', /Alto:\\s*([^\\n]+)/i],
                    ['Ancho', /Ancho:\\s*([^\\n]+)/i],
                    ['Traductor', /Traductor:\\s*([^\\n]+)/i],
                    ['Ilustrador', /Ilustrador:\\s*([^\\n]+)/i],
                    ['Ano de edicion', /A[\\u00f1n]o de edici[\\u00f3o]n:\\s*([^\\n]+)/i],
                    ['Plaza de edicion', /Plaza de edici[\\u00f3o]n:\\s*([^\\n]+)/i],
                    ['Tiempo de lectura', /Tiempo de lectura:\\s*([^\\n]+)/i],
                    ['Origen', /Origen:\\s*([^\\n]+)/i],
                ];

                patterns.forEach(([field, regex]) => {
                    if (!data[field]) {
                        const match = allText.match(regex);
                        if (match && match[1]) {
                            data[field] = match[1].trim().split('\\n')[0].trim();
                        }
                    }
                });

                return data;
            }
        ''')

        if result:
            ficha = result
            print(f"  Ficha tecnica fields found: {list(ficha.keys())}")
    except Exception as e:
        print(f"  Error extracting ficha tecnica: {e}")

    return ficha


async def scrape_book(page, query, direct_url=None):
    """Scrape book details from La Casa del Libro using Playwright."""
    print(f"\n{'='*55}")
    print(f"Scraping for: {query or direct_url}")

    try:
        book_url = ""
        if direct_url:
            book_url = direct_url
            print(f"Using direct URL: {book_url}")
        else:
            # Navigate DIRECTLY to the search results page — bypasses all cookie/popup overlays
            import urllib.parse
            search_url = f"{BASE_URL}/?query={urllib.parse.quote(query)}"
            print(f"Navigating to search URL: {search_url}")
            try:
                await page.goto(search_url, timeout=60000, wait_until='domcontentloaded')
                await page.wait_for_timeout(3000)  # let JS results load
            except Exception as e:
                print(f"Error navigating to search URL: {e}")
                return None

            # Find matching product link
            print("Looking for matching link...")
            target_link = None

            # 1. ISBN match in href
            clean_query = query.replace('-', '').replace(' ', '')
            if clean_query.isdigit():
                try:
                    isbn_links = page.locator(f'a[href*="{clean_query}"]')
                    if await isbn_links.count() > 0:
                        print(f"Found match by ISBN in href: {clean_query}")
                        target_link = isbn_links.first
                except:
                    pass

            # 2. Text content match
            if not target_link:
                try:
                    text_links = page.locator(f'a:has-text("{query}")')
                    count = await text_links.count()

                    if count == 0 and len(query.split()) > 2:
                        reduced_query = " ".join(query.split()[:2])
                        print(f"Trying reduced query: '{reduced_query}'")
                        text_links = page.locator(f'a:has-text("{reduced_query}")')
                        count = await text_links.count()

                    print(f"Found {count} text-matching links")
                    for i in range(count):
                        link = text_links.nth(i)
                        href = await link.get_attribute('href')
                        if href and ('/libro-' in href or '/ebook-' in href):
                            print(f"Valid book link: {href}")
                            target_link = link
                            break
                except Exception as e:
                    print(f"Error finding text links: {e}")

            # 3. Fuzzy token match on .compact-product
            if not target_link:
                print("Fallback: fuzzy match on .compact-product...")
                try:
                    products = page.locator('.compact-product')
                    count = await products.count()
                    for i in range(min(20, count)):
                        prod = products.nth(i)
                        try:
                            text = await prod.inner_text(timeout=1000)
                            text_lower = text.lower()
                            query_tokens = query.lower().replace('-', ' ').split()
                            matches = sum(1 for t in query_tokens if t in text_lower)
                            if len(query_tokens) > 0 and matches / len(query_tokens) >= 0.75:
                                target_link = prod.locator('a').first
                                print(f"Fuzzy match ({matches}/{len(query_tokens)}) at product {i}")
                                break
                        except:
                            continue
                except Exception as e:
                    print(f"Fallback search error: {e}")

            if not target_link:
                print(f"No matching link found for: {query}")
                return None

            # Navigate to book detail page
            book_url_relative = await target_link.get_attribute('href')
            if not book_url_relative:
                print("Could not extract book URL")
                return None

            book_url = BASE_URL + book_url_relative if not book_url_relative.startswith('http') else book_url_relative
            
        print(f"Navigating to: {book_url}")

        # Intercept the price API response (compraproductoget) which loads async
        price_from_api = {"value": None}

        async def handle_response(response):
            try:
                if 'compraproducto' in response.url or 'precio' in response.url.lower():
                    body = await response.json()
                    # Try common price keys in the API response
                    p = (body.get('precio') or body.get('price') or
                         body.get('Price') or body.get('pvp') or
                         (body.get('data') or {}).get('precio'))
                    if p and price_from_api["value"] is None:
                        try:
                            num = float(str(p).replace(',', '.'))
                            price_from_api["value"] = f"$ {num:,.0f}".replace(',', '.')
                        except:
                            price_from_api["value"] = str(p)
            except:
                pass

        page.on("response", handle_response)

        await page.goto(book_url, timeout=60000)
        try:
            await page.wait_for_load_state('networkidle', timeout=15000)
        except:
            await page.wait_for_load_state('domcontentloaded')
        await page.wait_for_timeout(3000)  # Extra wait for async price API

        # Remove cookie overlays via JS so they don't block any subsequent interactions
        try:
            await page.evaluate("""
                ['#onetrust-consent-sdk', '.onetrust-pc-dark-filter'].forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => el.remove());
                });
            """)
        except:
            pass

        # ── COVER IMAGE ───────────────────────────────────────────────
        image_url = ""
        try:
            image_url = await page.evaluate("""
                () => {
                    // 1. JSON-LD image
                    for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
                        try {
                            const d = JSON.parse(s.textContent);
                            if (d.image) return typeof d.image === 'string' ? d.image : (d.image.url || d.image[0] || '');
                        } catch(e) {}
                    }
                    // 2. Open Graph meta
                    const og = document.querySelector('meta[property="og:image"]');
                    if (og) return og.content;
                    // 3. Main product cover img
                    const imgs = [
                        document.querySelector('.book-cover img'),
                        document.querySelector('[class*="cover"] img'),
                        document.querySelector('[class*="product"] img'),
                        document.querySelector('img[class*="cover"]'),
                        document.querySelector('img[itemprop="image"]'),
                    ];
                    for (const img of imgs) {
                        if (img && img.src && !img.src.includes('data:')) return img.src;
                    }
                    return '';
                }
            """)
        except Exception as e:
            print(f"  Could not extract image: {e}")
        print(f"  Image: {image_url[:60] if image_url else 'None'}")

        # ── TITLE ─────────────────────────────────────────────────────
        title = await extract_text(page, [
            'h1',
            '.product-title h1',
            '[class*="title"] h1',
        ], "Unknown Title")
        print(f"  Title: {title[:70]}")

        # ── AUTHOR ────────────────────────────────────────────────────
        # Use JS to find the author: look for itemprop or structured data first,
        # then try the meta tag og:author or any link in the product header area.
        author = "Unknown Author"
        try:
            author = await page.evaluate("""
                () => {
                    // 1. Try structured data (JSON-LD)
                    const scripts = document.querySelectorAll('script[type="application/ld+json"]');
                    for (const s of scripts) {
                        try {
                            const d = JSON.parse(s.textContent);
                            if (d.author) {
                                if (typeof d.author === 'string') return d.author;
                                if (d.author.name) return d.author.name;
                                if (Array.isArray(d.author) && d.author[0]) return d.author[0].name || d.author[0];
                            }
                        } catch(e) {}
                    }
                    // 2. Try meta tags
                    const metaAuthor = document.querySelector('meta[name="author"], meta[property="og:author"], meta[property="book:author"]');
                    if (metaAuthor) return metaAuthor.content.trim();
                    // 3. Try itemprop
                    const itemprop = document.querySelector('[itemprop="author"]');
                    if (itemprop) return itemprop.innerText.trim();
                    return '';
                }
            """)
            if not author:
                author = await extract_text(page, [
                    '[itemprop="author"]',
                    '.authorNameHeader',
                    'h1 + a',
                ], "Unknown Author")
        except:
            author = "Unknown Author"
        print(f"  Author: {author[:60]}")

        # ── EDITORIAL ─────────────────────────────────────────────────
        editorial = await extract_text(page, [
            'a[href*="/editorial/"]',
            'span[class*="editorial"]',
            '[itemprop="publisher"]',
            '[class*="publisher"]',
        ], "")
        print(f"  Editorial: {editorial[:60]}")

        # ── PRICE ─────────────────────────────────────────────────────
        # Priority 1: price captured from the async API response interceptor
        # Priority 2: JSON-LD structured data, meta tags, DOM scan
        price = "Unknown Price"
        if price_from_api["value"]:
            price = price_from_api["value"]
            print(f"  Price (from API): {price[:40]}")
        else:
            try:
                price = await page.evaluate("""
                    () => {
                        // 1. JSON-LD offers price (site uses capital 'Price')
                        for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
                            try {
                                let parsed = JSON.parse(s.textContent);
                                if (!Array.isArray(parsed)) parsed = [parsed];
                                for (const d of parsed) {
                                    let offersRaw = d.offers;
                                    if (!offersRaw && d.workExample && d.workExample.length > 0) {
                                        offersRaw = d.workExample[0].offers;
                                    }
                                    if (offersRaw) {
                                        const o = Array.isArray(offersRaw) ? offersRaw[0] : offersRaw;
                                        const rawPrice = o.Price || o.price;
                                        const currency = o.priceCurrency || 'COP';
                                        if (rawPrice) {
                                            const num = parseFloat(rawPrice);
                                            if (!isNaN(num)) return '$ ' + num.toLocaleString('es-CO') + ' ' + currency;
                                            return String(rawPrice);
                                        }
                                    }
                                }
                            } catch(e) {}
                        }
                        // 2. Meta tags
                        const metaPrice = document.querySelector('meta[property="og:price:amount"], meta[name="price"]');
                        if (metaPrice) return metaPrice.content;
                        // 3. DOM scan for price-looking text
                        const all = document.querySelectorAll('*');
                        for (const el of all) {
                            if (el.children.length === 0 && el.innerText) {
                                const t = el.innerText.trim();
                                const startsWithDollar = t.startsWith('$') && t.length > 1;
                                const startWithCOP = t.startsWith('COP') && t.length > 3;
                                if (startsWithDollar || startWithCOP) {
                                    const rest = t.replace(/[$COP .,]/g, '');
                                    if (rest.length > 0 && rest.length < 12 && /^[0-9]+$/.test(rest)) {
                                        return t;
                                    }
                                }
                            }
                        }
                        return '';
                    }
                """)
                if not price:
                    price = "Unknown Price"
            except Exception as e:
                price = "Unknown Price"
        print(f"  Price: {price[:40]}")

        # ── ORIGINAL PRICE & DISCOUNT ──────────────────────────────────
        original_price = await extract_text(page, [
            '[class*="original-price"]',
            '[class*="old-price"]',
            'del span',
            's span',
            '[class*="crossed"]',
        ], "")

        discount = await extract_text(page, [
            '[class*="discount"]',
            '[class*="badge"][class*="off"]',
            '[class*="percent"]',
            '[class*="rebaja"]',
        ], "")

        # ── SYNOPSIS / DESCRIPTION ─────────────────────────────────────
        # The synopsis is truncated — click 'Ver más' to expand it first
        try:
            ver_mas = page.locator('label.like-a-link')
            if await ver_mas.count() > 0:
                await ver_mas.first.click(force=True)
                await page.wait_for_timeout(800)
                print("  Clicked 'Ver más' to expand synopsis")
        except Exception as e:
            print(f"  Could not click 'Ver más': {e}")

        description = await extract_text(page, [
            'div.resumen',               # Main synopsis div (sibling of h2.resumen)
            'h2.resumen ~ div',          # Sibling div after the synopsis heading
            '[class*="resumen"] p',
            '[class*="resume"] p',
            '[class*="resume"]',
            '[class*="description"] p',
            '[class*="description"]',
            '[itemprop="description"]',
        ], "No Description")
        description = description[:1500]
        print(f"  Description: {description[:80]}...")

        # ── ORIGIN ────────────────────────────────────────────────────
        origin = await extract_text(page, [
            '[class*="origin"]',
            '[class*="procedencia"]',
        ], "")
        if not origin:
            try:
                page_text = await page.locator('body').inner_text(timeout=3000)
                m = re.search(r'Origen[:\s]+([^\n]+)', page_text)
                if m:
                    origin = m.group(1).strip()
            except:
                pass
        print(f"  Origin: {origin}")

        # ── FICHA TECNICA (ALL FIELDS) ─────────────────────────────────
        print("  Extracting ficha tecnica...")
        ficha = await extract_ficha_tecnica(page)

        def get_ficha(*keys):
            """Search ficha dict for any of the given key substrings."""
            for k in keys:
                for ficha_key, val in ficha.items():
                    if k.lower() in ficha_key.lower():
                        v = val.strip()
                        # Remove trailing "Ver mas" or junk
                        v = v.split('\n')[0].strip()
                        if v:
                            return v
            return ""

        isbn         = get_ficha('isbn', 'ISBN')
        translator   = get_ficha('traductor', 'Traductor')
        illustrator  = get_ficha('ilustrador', 'Ilustrador')
        language     = get_ficha('idioma', 'Idioma')
        pages        = get_ficha('paginas', 'páginas', 'Paginas', 'Número de p')
        reading_time = get_ficha('lectura', 'Tiempo de')
        binding      = get_ficha('encuadernaci', 'Encuadernaci')
        release_date = get_ficha('lanzamiento', 'Fecha de')
        edition_year = get_ficha('ano de edici', 'Año de edici')
        edition_place= get_ficha('plaza', 'Plaza de')
        collection   = get_ficha('colecci', 'Colección', 'Coleccion')
        height       = get_ficha('alto', 'Alto')
        width        = get_ficha('ancho', 'Ancho')
        weight       = get_ficha('peso', 'Peso')

        # Fill editorial from ficha if still missing
        if not editorial:
            editorial = get_ficha('editorial', 'Editorial')

        print(f"  ISBN: {isbn} | Pages: {pages} | Binding: {binding}")
        print(f"  Language: {language} | Release: {release_date}")
        print(f"  Translator: {translator} | Illustrator: {illustrator}")
        print(f"  Collection: {collection} | H:{height} W:{width} Weight:{weight}")

        return {
            'search_query':  query,
            'title':         title.strip(),
            'author':        author.strip(),
            'editorial':     editorial.strip(),
            'isbn':          isbn.strip(),
            'price':         price.strip(),
            'original_price':original_price.strip(),
            'discount':      discount.strip(),
            'description':   description.strip(),
            'translator':    translator.strip(),
            'illustrator':   illustrator.strip(),
            'language':      language.strip(),
            'pages':         pages.strip(),
            'reading_time':  reading_time.strip(),
            'binding':       binding.strip(),
            'release_date':  release_date.strip(),
            'edition_year':  edition_year.strip(),
            'edition_place': edition_place.strip(),
            'collection':    collection.strip(),
            'height':        height.strip(),
            'width':         width.strip(),
            'weight':        weight.strip(),
            'origin':        origin.strip(),
            'url':           book_url,
            'image_url':     image_url,
        }

    except Exception as e:
        print(f"Exception scraping {query}: {e}")
        return None


async def process_row(row, page):
    query = str(row['busqueda']).strip()
    if not query or query.lower() == 'nan':
        return None

    existing = check_in_db(query)

    if existing:
        print(f"Found in DB: {query}")
        keys = [
            'title', 'author', 'editorial', 'isbn', 'price', 'original_price',
            'discount', 'description', 'translator', 'illustrator', 'language',
            'pages', 'reading_time', 'binding', 'release_date', 'edition_year',
            'edition_place', 'collection', 'height', 'width', 'weight', 'origin', 'url', 'image_url'
        ]
        result = {'busqueda': query, 'found_in_db': True}
        for i, k in enumerate(keys):
            result[k] = existing[i] if i < len(existing) else ''
        return result
    else:
        data = await scrape_book(page, query)
        if data:
            save_to_db(data)
            data['busqueda'] = query
            data['found_in_db'] = False
            return data
        else:
            print(f"Failed to scrape: {query}")
            return {'busqueda': query, 'status': 'Not Found'}


async def main_async():
    init_db()

    if not os.path.exists(INPUT_FILE):
        print(f"Input file {INPUT_FILE} not found!")
        return

    df = pd.read_excel(INPUT_FILE)

    if 'busqueda' not in df.columns:
        print("Column 'busqueda' not found in Excel file.")
        return

    results = []

    async with async_playwright() as p:
        launch_opts = {'headless': True}
        if PROXY_URL:
            launch_opts['proxy'] = {'server': PROXY_URL}
        browser = await p.chromium.launch(**launch_opts)
        page = await browser.new_page()
        await page.set_extra_http_headers({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Accept-Language': 'es-ES,es;q=0.9',
        })

        for _, row in df.iterrows():
            result = await process_row(row, page)
            if result:
                results.append(result)
            await asyncio.sleep(random.uniform(2, 4))

        await browser.close()

    if results:
        report_df = pd.DataFrame(results)
        # Nice column order for the Excel report
        col_order = [
            'busqueda', 'title', 'author', 'editorial', 'isbn', 'price',
            'original_price', 'discount', 'language', 'pages', 'reading_time',
            'binding', 'release_date', 'edition_year', 'edition_place',
            'collection', 'height', 'width', 'weight', 'origin',
            'translator', 'illustrator', 'description', 'url', 'found_in_db', 'status'
        ]
        existing_cols = [c for c in col_order if c in report_df.columns]
        remaining = [c for c in report_df.columns if c not in existing_cols]
        report_df = report_df[existing_cols + remaining]
        report_df.to_excel(OUTPUT_REPORT, index=False)
        print(f"\nReport generated: {OUTPUT_REPORT} ({len(results)} records)")
    else:
        print("No results to report.")


def main():
    asyncio.run(main_async())


if __name__ == '__main__':
    main()
