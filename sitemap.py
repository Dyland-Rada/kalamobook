"""
Sitemap-based book URL discovery for Casa del Libro.

Casa del Libro publishes a sitemap-index that points to ~40 themed
sub-sitemaps (.xml.gz). Each contains thousands of canonical book URLs
with image and ISBN metadata. Reading them is orders of magnitude faster
than crawling category listings page-by-page.

Public entry point: `iter_book_urls()` — async generator yielding
{url, isbn, image_url, title} dicts.
"""
import asyncio
import gzip
import xml.etree.ElementTree as ET
from typing import AsyncIterator

import aiohttp

# Casa del Libro ES base. Spanish servers hit this directly; non-ES
# clients get a 302 to .com.co. Sub-sitemap URLs inside the index will
# match whichever host actually served the response — that's fine, we
# just follow them as returned.
SITEMAP_INDEX_URL = "https://www.casadellibro.com/sitemap-index.xml"

# Theme-grouped sub-index — points to the 40 .xml.gz files with book URLs.
THEMES_INDEX_URL = "https://www.casadellibro.com/sitemap-cdl-libros-tematicas.xml"

NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "image": "http://www.google.com/schemas/sitemap-image/1.1",
}

# How many sub-sitemaps to fetch in parallel. Each is ~1MB gzipped → ~10MB XML.
SITEMAP_FETCH_CONCURRENCY = 4


async def _fetch_xml(session: aiohttp.ClientSession, url: str) -> str:
    """Fetch a sitemap (plain XML or gzip) and return decoded text."""
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=180)) as resp:
        resp.raise_for_status()
        data = await resp.read()
    if url.endswith(".gz"):
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="replace")


def _parse_book_entries(xml_text: str) -> list[dict]:
    """Parse a urlset XML and return [{url, isbn, image_url, title}, ...]."""
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[Sitemap] XML parse error: {e}")
        return out

    for url_el in root.findall("sm:url", NS):
        # First <loc> per <url> block is the canonical book page; the
        # second (if present) is the /opiniones-libro variant which we ignore.
        locs = url_el.findall("sm:loc", NS)
        if not locs or not locs[0].text:
            continue
        book_url = locs[0].text.strip()
        if "/libro-" not in book_url:
            continue  # skip non-book entries (categories, opinions, etc.)

        # URL pattern: /libro-SLUG/ISBN/PRODUCT_ID
        parts = [p for p in book_url.rstrip("/").split("/") if p]
        isbn = ""
        if len(parts) >= 2:
            candidate = parts[-2]
            if candidate.isdigit() and 10 <= len(candidate) <= 14:
                isbn = candidate

        image_el = url_el.find(".//image:loc", NS)
        title_el = url_el.find(".//image:title", NS)
        image_url = image_el.text.strip() if image_el is not None and image_el.text else ""
        title = ""
        if title_el is not None and title_el.text:
            # Format observed: "TITULO DEL LIBRO-9788412345678"
            title = title_el.text.split("-")[0].strip()

        out.append({
            "url": book_url,
            "isbn": isbn,
            "image_url": image_url,
            "title": title,
        })
    return out


async def fetch_themes_subsitemaps(session: aiohttp.ClientSession) -> list[str]:
    """Return the list of .xml.gz sub-sitemap URLs from the themes index."""
    xml = await _fetch_xml(session, THEMES_INDEX_URL)
    root = ET.fromstring(xml)
    urls = [el.text for el in root.findall(".//sm:sitemap/sm:loc", NS) if el.text]
    print(f"[Sitemap] Themes index has {len(urls)} sub-sitemaps")
    return urls


async def iter_book_urls(
    progress_cb=None,
) -> AsyncIterator[dict]:
    """
    Async generator that yields every book record from all theme sub-sitemaps.
    Deduplicates by URL across sitemaps.

    progress_cb: optional callable(message: str) for status updates.
    """
    seen: set[str] = set()
    timeout = aiohttp.ClientTimeout(total=180, connect=30)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept": "application/xml,text/xml,*/*",
    }

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        try:
            sub_sitemaps = await fetch_themes_subsitemaps(session)
        except Exception as e:
            print(f"[Sitemap] Failed to fetch themes index: {e}")
            return

        sem = asyncio.Semaphore(SITEMAP_FETCH_CONCURRENCY)

        async def _fetch_and_parse(idx: int, url: str) -> list[dict]:
            async with sem:
                try:
                    if progress_cb:
                        progress_cb(f"Sitemap {idx + 1}/{len(sub_sitemaps)}: {url.split('/')[-1]}")
                    xml = await _fetch_xml(session, url)
                    entries = _parse_book_entries(xml)
                    print(f"[Sitemap] {idx + 1}/{len(sub_sitemaps)} {url.split('/')[-1]}: "
                          f"{len(entries)} libros")
                    return entries
                except Exception as e:
                    print(f"[Sitemap] Error on {url}: {e}")
                    return []

        # Fan out fetches but yield as they complete to start scraping ASAP
        tasks = [asyncio.create_task(_fetch_and_parse(i, u))
                 for i, u in enumerate(sub_sitemaps)]

        for task in asyncio.as_completed(tasks):
            entries = await task
            for entry in entries:
                u = entry["url"]
                if u in seen:
                    continue
                seen.add(u)
                yield entry

    print(f"[Sitemap] Total unique book URLs: {len(seen)}")
