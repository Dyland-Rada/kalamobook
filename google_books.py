"""
Google Books API client — fallback gratis cuando Casa del Libro no tiene el libro.

Endpoint: GET https://www.googleapis.com/books/v1/volumes?q=isbn:<isbn>
Free tier: 1000 requests/day sin API key, 100k/day con key.
Setea GOOGLE_BOOKS_API_KEY (opcional) para subir el limite.
"""
import os
import asyncio
from typing import Optional, Dict, Any

import aiohttp

GOOGLE_BOOKS_API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "")
BASE_URL = "https://www.googleapis.com/books/v1/volumes"

# Mapeo ISO 639-1 → nombre humano para que el HTML rendereado sea legible
_LANG_MAP = {
    "es": "Castellano",
    "en": "Inglés",
    "ca": "Catalán",
    "gl": "Gallego",
    "eu": "Euskera",
    "pt": "Portugués",
    "fr": "Francés",
    "it": "Italiano",
    "de": "Alemán",
    "ja": "Japonés",
    "zh": "Chino",
    "ru": "Ruso",
    "ar": "Árabe",
    "la": "Latín",
}


def _normalize_isbn(s: str) -> str:
    return "".join(c for c in str(s or "") if c.isdigit() or c.upper() == "X")


async def fetch_by_isbn(
    session: aiohttp.ClientSession,
    isbn: str,
    *,
    timeout_s: float = 10.0,
) -> Optional[Dict[str, Any]]:
    """
    Busca un libro por ISBN en Google Books. Si encuentra multiples ediciones
    elige la que matchea el ISBN exacto, sino la primera.
    Devuelve None si no hay resultados o si la API falla.
    """
    isbn_clean = _normalize_isbn(isbn)
    if len(isbn_clean) not in (10, 13):
        return None

    params = {"q": f"isbn:{isbn_clean}", "maxResults": 5}
    if GOOGLE_BOOKS_API_KEY:
        params["key"] = GOOGLE_BOOKS_API_KEY

    try:
        async with session.get(
            BASE_URL, params=params,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None

    items = data.get("items") or []
    if not items:
        return None

    # Preferir el que matchea el ISBN exacto
    best = items[0]
    for item in items:
        ids = (item.get("volumeInfo") or {}).get("industryIdentifiers") or []
        for i in ids:
            if _normalize_isbn(i.get("identifier", "")) == isbn_clean:
                best = item
                break

    return _normalize(best.get("volumeInfo") or {}, isbn_clean, best.get("id"))


def _normalize(vi: Dict[str, Any], isbn: str, gb_id: Optional[str]) -> Dict[str, Any]:
    """Convierte la respuesta de Google Books al schema interno de la app."""
    authors = vi.get("authors") or []
    images = vi.get("imageLinks") or {}
    categories = vi.get("categories") or []
    lang_code = (vi.get("language") or "").lower()
    lang_human = _LANG_MAP.get(lang_code, lang_code)

    # Google Books a veces da dimensiones en `dimensions: {height, width, thickness}`
    dims = vi.get("dimensions") or {}

    # Imagen: preferir la mas grande disponible
    image_url = (
        images.get("extraLarge") or images.get("large") or
        images.get("medium") or images.get("thumbnail") or
        images.get("smallThumbnail") or ""
    )
    # Google Books sirve thumbnails con http, forzar https para que Odoo no rechace
    if image_url.startswith("http://"):
        image_url = "https://" + image_url[7:]

    return {
        "source": "google_books",
        "google_books_id": gb_id or "",
        "isbn": isbn,
        "title": (vi.get("title") or "").strip(),
        "subtitle": (vi.get("subtitle") or "").strip(),
        "author": ", ".join(authors),
        "editorial": (vi.get("publisher") or "").strip(),
        "release_date": (vi.get("publishedDate") or "").strip(),
        "description": (vi.get("description") or "").strip(),
        "pages": str(vi.get("pageCount") or "").strip() or "",
        "language": lang_human,
        "categories": categories,
        "tags": categories,
        "image_url": image_url,
        "average_rating": vi.get("averageRating"),
        "ratings_count": vi.get("ratingsCount"),
        "preview_link": (vi.get("previewLink") or "").strip(),
        "info_link": (vi.get("infoLink") or "").strip(),
        "maturity_rating": vi.get("maturityRating", ""),
        "height": dims.get("height", ""),
        "width": dims.get("width", ""),
        "thickness": dims.get("thickness", ""),
        # Campos que Google Books NO tiene (los deja vacios para no romper merge)
        "weight": "",
        "binding": "",
        "translator": "",
        "illustrator": "",
        "collection": "",
        "price": "",
        "edition_year": "",
        "edition_place": "",
        "reading_time": "",
        "origin": "",
        "url": (vi.get("canonicalVolumeLink") or vi.get("infoLink") or "").strip(),
    }


def merge_book_data(
    primary: Optional[dict],
    fallback: Optional[dict],
) -> Optional[dict]:
    """
    Combina dos fuentes de datos del mismo libro. Los campos de `primary`
    ganan si tienen valor; los huecos se rellenan con `fallback`.
    Usado para mezclar CDL (mejor para fichas tecnicas: peso/altura) con
    Google Books (mejor cobertura).
    """
    if not primary and not fallback:
        return None
    if not fallback:
        return primary
    if not primary:
        return fallback

    merged = dict(fallback)
    for k, v in primary.items():
        if _has_value(v):
            merged[k] = v

    sources = []
    if _has_value(primary.get("title")):
        sources.append(primary.get("source") or "primary")
    if _has_value(fallback.get("title")):
        sources.append(fallback.get("source") or "fallback")
    merged["source"] = "+".join(dict.fromkeys(sources)) or "merged"
    return merged


def _has_value(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        s = v.strip().lower()
        return s not in ("", "unknown", "unknown price", "unknown title", "no description", "0")
    if isinstance(v, (list, dict)):
        return len(v) > 0
    return True


async def fetch_by_isbn_with_session(isbn: str) -> Optional[Dict[str, Any]]:
    """Conveniencia para callers que no manejan session — crea una propia."""
    async with aiohttp.ClientSession() as sess:
        return await fetch_by_isbn(sess, isbn)
