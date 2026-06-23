"""
Carga del catalogo completo de AZETA al mirror.

AZETA expone su catalogo entero (~1M libros) en un endpoint HTTP que devuelve
un ZIP con un CSV (encoding latin-1, separador `|`, 22 columnas).

Cobertura medida del CSV:
  Titulo: 100%, Ean: 100%, Editorial: 100%, Precio S/IVA: 100%
  Autor: 97.7%, Idioma: 98.2%, Peso: 96.5%, Coleccion: 95.9%
  PVP: 92.4%, Tema: 73.5%, Paginas: 73.6%, Fecha edicion: 70.9%
  Ancho: 67.1%, Alto: 67.0%, Portada URL: 66.9%
  Encuadernacion: 60.7%, Sinopsis: 43.9% (promedio 847 chars)

Reuso las columnas cdl_* del mirror para los campos descriptivos (con
inferred_source='azeta_catalog'), y agrego azeta_price_eur / azeta_price_no_iva /
azeta_iva / azeta_codigo / azeta_fetched_at especificas.

NO push a Odoo desde aqui (eso es la Fase 2). Solo cargo al mirror.
"""
import asyncio
import io
import os
import time
import zipfile
from datetime import datetime
from typing import Iterator

import aiohttp

import db

CATALOG_URL = "https://www.azetadistribuciones.es/servicios_web/csv_parcial.php"
AZETA_USER = os.environ.get("AZETA_USER", "120153")
AZETA_PASS = os.environ.get("AZETA_PASS", "jalta4b")

CSV_COLUMNS = [
    "Titulo", "Autor", "Idioma", "Ean", "Codigo", "Editorial",
    "PrecioSIVA", "PVP", "iva", "Tema", "Coleccion", "Encuadernacion",
    "Alto", "Ancho", "Fecha_edicion", "Paginas", "Peso", "portada",
    "Sinopsis", "Fecha_servicio", "Ud_Venta", "Perm_Devolucion",
]

# Estado del job en memoria
catalog_job: dict | None = None


def get_catalog_status() -> dict:
    job = dict(catalog_job) if catalog_job else {"status": "idle"}
    job["azeta_books_in_mirror"] = _count_azeta_in_mirror()
    job["azeta_last_fetched"] = _last_azeta_fetched()
    if "errors" in job:
        job["errors"] = job["errors"][-10:]
    return job


def stop_catalog_sync():
    global catalog_job
    if catalog_job and catalog_job.get("status") == "running":
        catalog_job["status"] = "stopped"
        return True
    return False


def _count_azeta_in_mirror() -> int:
    """Cuantos libros del mirror tienen azeta_fetched_at NOT NULL."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur,
            "SELECT COUNT(*) FROM odoo_books_mirror WHERE azeta_fetched_at IS NOT NULL")
        return cur.fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


def _last_azeta_fetched() -> str | None:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur,
            "SELECT MAX(azeta_fetched_at) FROM odoo_books_mirror")
        row = cur.fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass
    finally:
        conn.close()
    return None


def _normalize_int(v: str) -> int | None:
    """Parsea str a int. Devuelve None si vacio o invalido."""
    if not v:
        return None
    s = v.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _normalize_float(v: str) -> float | None:
    if not v:
        return None
    s = v.strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _format_date_yyyymmdd(v: str) -> str | None:
    """Convierte '20150301' a '01/03/2015' (formato CDL para compat)."""
    if not v or len(v.strip()) != 8 or not v.strip().isdigit():
        return None
    s = v.strip()
    return f"{s[6:8]}/{s[4:6]}/{s[:4]}"


def _format_dim(v: str) -> str | None:
    """Convierte '230' (mm) a '23.0 cm' (formato CDL)."""
    n = _normalize_int(v)
    if n is None or n <= 0:
        return None
    return f"{n / 10:.1f} cm"


def _format_weight(v: str) -> str | None:
    """Convierte '557' (gramos) a '557.0 gr'."""
    n = _normalize_int(v)
    if n is None or n <= 0:
        return None
    return f"{n}.0 gr"


def _split_categories(tema: str) -> list[str]:
    """Tema viene como 'CAT1#CAT2#CAT3'. Devuelve lista limpia."""
    if not tema:
        return []
    parts = [p.strip() for p in tema.split("#") if p.strip()]
    # Dedup manteniendo orden, max 5 (compat con scraper)
    seen = set()
    out = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
        if len(out) >= 5:
            break
    return out


def _parse_row(parts: list[str]) -> dict | None:
    """Convierte una fila del CSV a dict listo para UPSERT."""
    if len(parts) < 22:
        return None
    raw = dict(zip(CSV_COLUMNS, parts))

    isbn = (raw.get("Ean") or "").strip()
    if not isbn or not isbn.isdigit() or len(isbn) not in (10, 13):
        return None

    return {
        "isbn": isbn,
        "title": (raw.get("Titulo") or "").strip()[:500],
        "author": (raw.get("Autor") or "").strip()[:200],
        "language": (raw.get("Idioma") or "").strip()[:20],
        "codigo": (raw.get("Codigo") or "").strip()[:50],
        "editorial": (raw.get("Editorial") or "").strip()[:200],
        "price_no_iva": _normalize_float(raw.get("PrecioSIVA") or ""),
        "price_eur": _normalize_float(raw.get("PVP") or ""),
        "iva": _normalize_int(raw.get("iva") or ""),
        "categories": _split_categories(raw.get("Tema") or ""),
        "collection": (raw.get("Coleccion") or "").strip()[:200],
        "binding": (raw.get("Encuadernacion") or "").strip()[:50],
        "height": _format_dim(raw.get("Alto") or ""),
        "width": _format_dim(raw.get("Ancho") or ""),
        "release_date": _format_date_yyyymmdd(raw.get("Fecha_edicion") or ""),
        "pages": (raw.get("Paginas") or "").strip(),
        "weight": _format_weight(raw.get("Peso") or ""),
        "image_url": (raw.get("portada") or "").strip()[:500],
        "description": (raw.get("Sinopsis") or "").strip()[:5000],
    }


async def download_catalog_zip() -> bytes:
    """Descarga el ZIP del catalogo AZETA. Devuelve bytes."""
    timeout = aiohttp.ClientTimeout(total=300)
    params = {"user": AZETA_USER, "password": AZETA_PASS}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(CATALOG_URL, params=params) as resp:
            if resp.status != 200:
                raise RuntimeError(f"AZETA catalog HTTP {resp.status}")
            return await resp.read()


def iter_csv_from_zip(zip_bytes: bytes) -> Iterator[dict]:
    """Generator que yield un dict por fila valida del CSV."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".csv"):
                continue
            with zf.open(name) as f:
                # CSV es grande (664MB), iteramos por lineas decodificadas
                # Latin-1 jamas falla decodificando (todos los bytes son validos)
                raw = f.read()
            text = raw.decode("latin-1")
            lines = text.splitlines()
            if not lines:
                continue
            # Skip header
            for ln in lines[1:]:
                parts = ln.split("|")
                row = _parse_row(parts)
                if row:
                    yield row
            return  # solo procesamos el primer CSV
        # Si llegamos aqui, no habia CSV en el ZIP
        raise RuntimeError("No se encontro ningun .csv dentro del ZIP")


def upsert_to_mirror_batch(rows: list[dict]) -> dict:
    """UPSERT batch al mirror. Solo actualiza libros que YA estan en
    odoo_books_mirror (cruce por barcode = isbn).
    Devuelve {updated, skipped_not_in_mirror}.
    """
    if not rows:
        return {"updated": 0, "skipped_not_in_mirror": 0}

    conn = db.get_connection()
    cur = conn.cursor()
    updated = 0
    skipped = 0

    try:
        for r in rows:
            cats = r.get("categories") or []
            cats_str = " > ".join(cats) if cats else None

            # UPDATE solo si existe la fila en mirror (no creamos libros nuevos)
            sql = """
                UPDATE odoo_books_mirror
                SET cdl_author        = COALESCE(NULLIF(cdl_author, ''),        ?),
                    cdl_editorial     = COALESCE(NULLIF(cdl_editorial, ''),     ?),
                    cdl_image_url     = COALESCE(NULLIF(cdl_image_url, ''),     ?),
                    cdl_weight        = COALESCE(NULLIF(cdl_weight, ''),        ?),
                    cdl_height        = COALESCE(NULLIF(cdl_height, ''),        ?),
                    cdl_width         = COALESCE(NULLIF(cdl_width, ''),         ?),
                    cdl_binding       = COALESCE(NULLIF(cdl_binding, ''),       ?),
                    cdl_collection    = COALESCE(NULLIF(cdl_collection, ''),    ?),
                    cdl_pages         = COALESCE(NULLIF(cdl_pages, ''),         ?),
                    cdl_release_date  = COALESCE(NULLIF(cdl_release_date, ''),  ?),
                    cdl_language      = COALESCE(NULLIF(cdl_language, ''),      ?),
                    description       = COALESCE(NULLIF(description, ''),       ?),
                    inferred_categories = COALESCE(NULLIF(inferred_categories, ''), ?),
                    inferred_source   = CASE
                        WHEN (inferred_categories IS NULL OR inferred_categories = '')
                             AND ? IS NOT NULL THEN 'azeta_catalog'
                        ELSE inferred_source
                    END,
                    azeta_price_eur    = ?,
                    azeta_price_no_iva = ?,
                    azeta_iva          = ?,
                    azeta_codigo       = COALESCE(NULLIF(azeta_codigo, ''), ?),
                    azeta_fetched_at   = CURRENT_TIMESTAMP
                WHERE barcode = ?
            """
            params = (
                r.get("author"), r.get("editorial"), r.get("image_url"),
                r.get("weight"), r.get("height"), r.get("width"),
                r.get("binding"), r.get("collection"), r.get("pages"),
                r.get("release_date"), r.get("language"),
                r.get("description"),
                cats_str, cats_str,  # ON ... IS NOT NULL en inferred_source
                r.get("price_eur"), r.get("price_no_iva"), r.get("iva"),
                r.get("codigo"),
                r["isbn"],
            )
            db.execute_query(cur, sql, params)
            # En psycopg2 + Postgres, cur.rowcount es el numero de filas afectadas
            if cur.rowcount and cur.rowcount > 0:
                updated += 1
            else:
                skipped += 1
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        raise
    finally:
        conn.close()

    return {"updated": updated, "skipped_not_in_mirror": skipped}


async def run_catalog_sync(batch_size: int = 500) -> dict:
    """
    Job completo:
      1. Descarga ZIP
      2. Parsea CSV
      3. UPSERT al mirror en batches
    """
    global catalog_job
    catalog_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "stage": "starting",
        "downloaded_bytes": 0,
        "rows_parsed": 0,
        "rows_invalid": 0,
        "updated": 0,
        "skipped_not_in_mirror": 0,
        "elapsed_download_s": 0,
        "elapsed_parse_s": 0,
        "elapsed_upsert_s": 0,
        "errors": [],
    }
    job = catalog_job
    t_start = time.monotonic()

    try:
        # 1. Descarga ZIP
        job["stage"] = "downloading"
        print("[AZETACat] Descargando catalogo ZIP...")
        t0 = time.monotonic()
        zip_bytes = await download_catalog_zip()
        job["elapsed_download_s"] = round(time.monotonic() - t0, 2)
        job["downloaded_bytes"] = len(zip_bytes)
        print(f"[AZETACat] Descargado: {len(zip_bytes):,} bytes en {job['elapsed_download_s']}s")

        # 2. Parse + batch UPSERT
        job["stage"] = "processing"
        batch: list[dict] = []
        rows_parsed = 0
        t_parse_start = time.monotonic()
        t_upsert_total = 0.0

        for row in iter_csv_from_zip(zip_bytes):
            if job["status"] != "running":
                print("[AZETACat] Detenido por usuario")
                break
            rows_parsed += 1
            batch.append(row)
            if len(batch) >= batch_size:
                t_u = time.monotonic()
                try:
                    res = upsert_to_mirror_batch(batch)
                    job["updated"] += res["updated"]
                    job["skipped_not_in_mirror"] += res["skipped_not_in_mirror"]
                except Exception as e:
                    job["errors"].append(f"batch @ {rows_parsed}: {type(e).__name__}: {str(e)[:120]}")
                t_upsert_total += time.monotonic() - t_u
                batch = []
                if rows_parsed % 10000 == 0:
                    print(f"[AZETACat] {rows_parsed:,} filas procesadas "
                          f"(upd:{job['updated']} skip:{job['skipped_not_in_mirror']})")

        # Flush ultimo batch
        if batch and job["status"] == "running":
            t_u = time.monotonic()
            try:
                res = upsert_to_mirror_batch(batch)
                job["updated"] += res["updated"]
                job["skipped_not_in_mirror"] += res["skipped_not_in_mirror"]
            except Exception as e:
                job["errors"].append(f"final batch: {type(e).__name__}: {str(e)[:120]}")
            t_upsert_total += time.monotonic() - t_u

        job["rows_parsed"] = rows_parsed
        job["elapsed_parse_s"] = round(time.monotonic() - t_parse_start - t_upsert_total, 2)
        job["elapsed_upsert_s"] = round(t_upsert_total, 2)
        job["elapsed_total_s"] = round(time.monotonic() - t_start, 2)
        job["stage"] = "done"

        if job["status"] == "running":
            job["status"] = "completed"
        print(f"[AZETACat] DONE: parsed={rows_parsed:,} upd={job['updated']:,} "
              f"skip={job['skipped_not_in_mirror']:,} en {job['elapsed_total_s']}s")
    except Exception as e:
        job["status"] = "error"
        err = f"{type(e).__name__}: {e!r}"
        job["errors"].append(err[:300])
        print(f"[AZETACat] Fatal: {err}")

    return job