"""
Espejo local de product.template desde Odoo a Postgres.

Pulla en lotes via JSON-RPC e inserta/upserta a odoo_books_mirror.
Job en background, idempotente (re-ejecutar es seguro — los registros
existentes se actualizan, los nuevos se insertan).

Default: solo libros con barcode y SIN description_sale (~727k).
Pasa only_pending=False para espejar TODOS (~1M).

Campos copiados: id, barcode, name, description, description_sale,
list_price, categ_id, categ_name.
"""
import asyncio
import json
import os
from datetime import datetime
from typing import Any

import db
from odoo_client import OdooClient, OdooError

MIRROR_BATCH_SIZE = int(os.environ.get("MIRROR_BATCH_SIZE", "1000"))
# Campos siempre presentes en product.template (Odoo core)
MIRROR_FIELDS_CORE = [
    "id", "barcode", "name", "description",
    "description_sale", "list_price", "categ_id",
]
# Campos opcionales que dependen de modulos (website_sale para public_categ_ids).
# Se prueban al arranque del job y si Odoo dice "Invalid field" se quitan.
MIRROR_FIELDS_OPTIONAL = ["public_categ_ids"]

# ── Sharding para multi-worker (Server A + Server B sobre la misma BD) ──
# Cada worker procesa solo libros con `odoo_id % SHARD_COUNT = SHARD_INDEX`.
# Sin coordinacion ni locks — las particiones son disjuntas por matematica.
# Default 0/1 = un solo worker procesa todo (compat backward).
#   Server B: WORKER_SHARD_INDEX=0  WORKER_SHARD_COUNT=2
#   Server A: WORKER_SHARD_INDEX=1  WORKER_SHARD_COUNT=2
WORKER_SHARD_INDEX = int(os.environ.get("WORKER_SHARD_INDEX", "0"))
WORKER_SHARD_COUNT = max(1, int(os.environ.get("WORKER_SHARD_COUNT", "1")))


def _shard_clause(col: str = "odoo_id") -> str:
    """SQL fragment para filtrar este worker. Si COUNT=1, retorna '1=1'.
    Usa MOD() en Postgres porque '%' literal colisiona con el placeholder
    de psycopg2 (%s) y revienta cualquier execute con params."""
    if WORKER_SHARD_COUNT <= 1:
        return "1=1"
    if db.IS_POSTGRES:
        return f"MOD({col}, {WORKER_SHARD_COUNT}) = {WORKER_SHARD_INDEX}"
    return f"({col} % {WORKER_SHARD_COUNT}) = {WORKER_SHARD_INDEX}"

mirror_job: dict | None = None


# ── Job state ──────────────────────────────────────────────────────────
def _job_init(only_pending: bool, batch_size: int) -> dict:
    global mirror_job
    mirror_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "only_pending": only_pending,
        "batch_size": batch_size,
        "total_target": 0,
        "offset": 0,
        "mirrored": 0,
        "errors": [],
    }
    return mirror_job


def stop_mirror_job() -> bool:
    """Pide al job que pare en el proximo loop. Idempotente."""
    global mirror_job
    if mirror_job and mirror_job.get("status") == "running":
        mirror_job["status"] = "stopped"
        return True
    return False


def get_mirror_status() -> dict:
    """Snapshot del estado del job para la UI / poller."""
    job = dict(mirror_job) if mirror_job else {"status": "idle"}
    job["mirror_count_local"] = count_mirror_rows()
    job["public_categories_cached"] = count_public_categories()
    job["inferred_categorized"] = count_inferred_rows()
    job["gbooks_fetched_count"] = _count_with("gbooks_fetched_at IS NOT NULL")
    job["cdl_fetched_count"] = _count_with("cdl_fetched_at IS NOT NULL")
    job["with_supplier_count"] = _count_with("supplier_count IS NOT NULL AND supplier_count > 0")
    if "errors" in job:
        job["errors"] = job["errors"][-5:]
    return job


def count_mirror_rows() -> int:
    """Cuantos libros tenemos espejados localmente."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, "SELECT COUNT(*) FROM odoo_books_mirror")
        return cur.fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


def _count_with(where_clause: str) -> int:
    """Helper para conteos con WHERE arbitrario sobre odoo_books_mirror."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"SELECT COUNT(*) FROM odoo_books_mirror WHERE {where_clause}")
        return cur.fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


# ── Upsert ─────────────────────────────────────────────────────────────
def _load_categ_name_cache() -> dict[int, str]:
    """Lee odoo_public_categories y arma {id: complete_name} para enriquecer
    el mirror con nombres legibles cuando ya hicimos sync de categorias."""
    cache: dict[int, str] = {}
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        db.execute_query(cur, "SELECT categ_id, COALESCE(complete_name, name) FROM odoo_public_categories")
        for row in cur.fetchall():
            cache[row[0]] = row[1]
        conn.close()
    except Exception:
        pass
    return cache


def _upsert_batch(rows: list[dict], categ_cache: dict[int, str] | None = None) -> int:
    """Inserta/actualiza un lote de filas. Devuelve cuantas se procesaron."""
    if not rows:
        return 0
    if categ_cache is None:
        categ_cache = {}

    conn = db.get_connection()
    cur = conn.cursor()
    inserted = 0
    for r in rows:
        odoo_id = r.get("id")
        if not odoo_id:
            continue

        # categ_id (interno contable) en Odoo viene como [id, "name"] o False
        categ = r.get("categ_id")
        categ_id, categ_name = None, None
        if isinstance(categ, list) and len(categ) >= 2:
            categ_id = categ[0]
            categ_name = categ[1]

        # public_categ_ids (tienda/web) viene como [3, 7, 12] o False
        public_ids = r.get("public_categ_ids")
        if not isinstance(public_ids, list):
            public_ids = []
        public_ids_json = json.dumps(public_ids) if public_ids else None
        public_names = None
        if public_ids and categ_cache:
            names = [categ_cache.get(i) for i in public_ids]
            names = [n for n in names if n]
            if names:
                public_names = " | ".join(names)

        barcode = r.get("barcode") or None
        name = r.get("name") or None
        # Odoo devuelve False para campos vacios — normalizar a None
        desc = r.get("description") if r.get("description") else None
        desc_sale = r.get("description_sale") if r.get("description_sale") else None
        list_price = r.get("list_price")
        if list_price in (False, None):
            list_price = None

        try:
            if db.IS_POSTGRES:
                db.execute_query(cur, """
                    INSERT INTO odoo_books_mirror
                        (odoo_id, barcode, name, description, description_sale,
                         list_price, categ_id, categ_name,
                         public_categ_ids, public_categ_names, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT (odoo_id) DO UPDATE SET
                        barcode = EXCLUDED.barcode,
                        name = EXCLUDED.name,
                        description = EXCLUDED.description,
                        description_sale = EXCLUDED.description_sale,
                        list_price = EXCLUDED.list_price,
                        categ_id = EXCLUDED.categ_id,
                        categ_name = EXCLUDED.categ_name,
                        public_categ_ids = EXCLUDED.public_categ_ids,
                        public_categ_names = EXCLUDED.public_categ_names,
                        synced_at = CURRENT_TIMESTAMP
                """, (odoo_id, barcode, name, desc, desc_sale,
                      list_price, categ_id, categ_name,
                      public_ids_json, public_names))
            else:
                db.execute_query(cur, """
                    INSERT OR REPLACE INTO odoo_books_mirror
                        (odoo_id, barcode, name, description, description_sale,
                         list_price, categ_id, categ_name,
                         public_categ_ids, public_categ_names, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (odoo_id, barcode, name, desc, desc_sale,
                      list_price, categ_id, categ_name,
                      public_ids_json, public_names))
            inserted += 1
        except Exception:
            # Un row malo no debe abortar el lote — Odoo a veces devuelve
            # registros con tipos raros.
            continue

    conn.commit()
    conn.close()
    return inserted


# ── Main job loop ──────────────────────────────────────────────────────
async def run_mirror_job(only_pending: bool = True,
                         batch_size: int = MIRROR_BATCH_SIZE):
    """
    Loop principal: pulla en lotes via search_read, upserta, repite hasta
    agotar. Si Odoo da error en un lote, espera 30s y reintenta.
    """
    global mirror_job
    if mirror_job and mirror_job.get("status") == "running":
        raise RuntimeError("Mirror job already running")

    job = _job_init(only_pending, batch_size)
    domain = [["barcode", "!=", False]]
    if only_pending:
        domain.append(["description_sale", "=", False])

    print(f"[Mirror] Arrancado — only_pending={only_pending}, batch={batch_size}")
    categ_cache = _load_categ_name_cache()
    if categ_cache:
        print(f"[Mirror] Cache de categorias publicas cargado: {len(categ_cache)} entries")
    else:
        print("[Mirror] Sin cache de categorias — corre /api/v1/odoo/mirror/sync-categories despues")

    try:
        async with OdooClient() as odoo:
            # Detectar que campos opcionales soporta esta instancia de Odoo.
            # Sin website_sale no existe public_categ_ids — y el job
            # entero falla en cada fetch si lo pedimos.
            mirror_fields = await _detect_supported_fields(odoo)
            print(f"[Mirror] Campos a pullar: {mirror_fields}")
            try:
                total = await odoo.search_count("product.template", domain)
                job["total_target"] = total
                print(f"[Mirror] Target: {total} libros")
            except Exception as e:
                err = f"{type(e).__name__}: {e!r}"
                job["errors"].append(f"count: {err[:200]}")
                print(f"[Mirror] No pude contar target: {err}")

            offset = 0
            target = job["total_target"]
            while job["status"] == "running":
                if target and offset >= target:
                    break
                try:
                    rows = await odoo.search_read(
                        "product.template", domain, mirror_fields,
                        offset=offset, limit=batch_size, order="id",
                    )
                except Exception as e:
                    err = f"{type(e).__name__}: {e!r}"
                    job["errors"].append(f"fetch@{offset}: {err[:200]}")
                    print(f"[Mirror] Fetch error @ {offset}: {err}")
                    await asyncio.sleep(30)
                    continue

                if not rows:
                    print(f"[Mirror] Odoo devolvio 0 filas @ offset {offset} — fin")
                    break

                inserted = _upsert_batch(rows, categ_cache)
                job["mirrored"] += inserted
                offset += len(rows)
                job["offset"] = offset
                pct = (offset / target * 100) if target else 0
                print(f"[Mirror] {offset}/{target} ({pct:.1f}%) — "
                      f"total espejados: {job['mirrored']}")

            if job["status"] == "running":
                job["status"] = "completed"
                print(f"[Mirror] COMPLETED: {job['mirrored']} libros espejados")
            else:
                print(f"[Mirror] STOPPED: {job['mirrored']} libros espejados")
    except Exception as e:
        job["status"] = "error"
        err = f"{type(e).__name__}: {e!r}"
        job["errors"].append(f"fatal: {err[:200]}")
        print(f"[Mirror] Fatal: {err}")


# ── Sync de categorias de tienda (product.public.category) ─────────────
async def _detect_supported_fields(odoo: OdooClient) -> list[str]:
    """
    Devuelve la lista de campos a usar en search_read sobre product.template,
    saltando los opcionales (public_categ_ids) que no existen en instancias
    sin website_sale instalado.

    Hace un fields_get una sola vez al inicio del job para evitar errores
    repetidos en cada batch.
    """
    fields = list(MIRROR_FIELDS_CORE)
    try:
        info = await odoo.execute_kw(
            "product.template", "fields_get",
            [],
            {"attributes": ["type"]},
        )
        available = set(info.keys()) if isinstance(info, dict) else set()
        for opt in MIRROR_FIELDS_OPTIONAL:
            if opt in available:
                fields.append(opt)
            else:
                print(f"[Mirror] Campo opcional '{opt}' no existe en Odoo (saltando)")
    except Exception as e:
        print(f"[Mirror] fields_get fallo ({type(e).__name__}: {e!r}) — uso solo core")
    return fields


async def sync_public_categories() -> dict:
    """
    Pulla TODAS las product.public.category de Odoo y las cachea en
    odoo_public_categories. Tras esto, el mirror puede resolver
    public_categ_ids -> nombres legibles.

    Tambien hace backfill: si ya hay filas en odoo_books_mirror con
    public_categ_ids guardado, recalcula public_categ_names usando el cache
    recien actualizado.
    """
    print("[CategSync] Pullando product.public.category...")
    async with OdooClient() as odoo:
        try:
            rows = await odoo.search_read(
                "product.public.category", [],
                ["id", "name", "parent_id", "complete_name"],
                order="id",
            )
        except Exception as e:
            err = f"{type(e).__name__}: {e!r}"
            print(f"[CategSync] Error pullando: {err}")
            return {"status": "error", "error": err}

    if not rows:
        print("[CategSync] Odoo devolvio 0 categorias publicas")
        return {"status": "ok", "fetched": 0, "backfilled": 0}

    # Upsert al cache
    conn = db.get_connection()
    cur = conn.cursor()
    n_upserted = 0
    for r in rows:
        cat_id = r.get("id")
        if not cat_id:
            continue
        name = r.get("name") or ""
        complete_name = r.get("complete_name") or name
        parent = r.get("parent_id")
        parent_id = None
        if isinstance(parent, list) and len(parent) >= 1:
            parent_id = parent[0]
        try:
            if db.IS_POSTGRES:
                db.execute_query(cur, """
                    INSERT INTO odoo_public_categories
                        (categ_id, name, parent_id, complete_name, synced_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT (categ_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        parent_id = EXCLUDED.parent_id,
                        complete_name = EXCLUDED.complete_name,
                        synced_at = CURRENT_TIMESTAMP
                """, (cat_id, name, parent_id, complete_name))
            else:
                db.execute_query(cur, """
                    INSERT OR REPLACE INTO odoo_public_categories
                        (categ_id, name, parent_id, complete_name, synced_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (cat_id, name, parent_id, complete_name))
            n_upserted += 1
        except Exception:
            continue
    conn.commit()
    print(f"[CategSync] Cacheadas {n_upserted} categorias publicas")

    # Backfill: recalcular public_categ_names en filas existentes del mirror
    cache = _load_categ_name_cache()
    db.execute_query(cur, """
        SELECT odoo_id, public_categ_ids
        FROM odoo_books_mirror
        WHERE public_categ_ids IS NOT NULL
          AND (public_categ_names IS NULL OR public_categ_names = '')
    """)
    rows_to_update = cur.fetchall()
    backfilled = 0
    for row in rows_to_update:
        odoo_id = row[0]
        ids_json = row[1]
        try:
            ids = json.loads(ids_json) if ids_json else []
        except Exception:
            continue
        names = [cache.get(i) for i in ids if i in cache]
        names = [n for n in names if n]
        if not names:
            continue
        full = " | ".join(names)
        try:
            db.execute_query(cur, """
                UPDATE odoo_books_mirror
                SET public_categ_names = ?
                WHERE odoo_id = ?
            """, (full, odoo_id))
            backfilled += 1
        except Exception:
            continue
    conn.commit()
    conn.close()
    print(f"[CategSync] Backfilled {backfilled} mirror rows con nombres legibles")

    return {
        "status": "ok",
        "fetched_categories": n_upserted,
        "backfilled_mirror_rows": backfilled,
    }


# ── Sync de proveedores desde Odoo (product.supplierinfo) ─────────────
# La Kalamo Import Tool cargó ~200k libros con vendor asignado. Mi mirror
# nunca pulleó ese campo. Este job lo trae sin tocar nada en Odoo (solo lee).
suppliers_sync_job: dict | None = None


def get_suppliers_sync_status() -> dict:
    job = dict(suppliers_sync_job) if suppliers_sync_job else {"status": "idle"}
    job["with_supplier_count"] = _count_with(
        "supplier_count IS NOT NULL AND supplier_count > 0"
    )
    if "errors" in job:
        job["errors"] = job["errors"][-5:]
    return job


def stop_suppliers_sync():
    global suppliers_sync_job
    if suppliers_sync_job and suppliers_sync_job.get("status") == "running":
        suppliers_sync_job["status"] = "stopped"
        return True
    return False


async def sync_suppliers_from_odoo(batch_size: int = 2000) -> dict:
    """
    Lee product.supplierinfo + res.partner de Odoo y espeja vendors al
    mirror. Solo LECTURA en Odoo, no escribe nada alli.

    Steps:
      1. Pull paginado de product.supplierinfo
      2. Pull de res.partner para los partner_ids distintos
      3. UPDATE bulk al mirror con supplier_names + supplier_partner_ids
    """
    global suppliers_sync_job
    suppliers_sync_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "supplierinfo_rows": 0,
        "distinct_partners": 0,
        "products_updated": 0,
        "errors": [],
    }
    job = suppliers_sync_job

    try:
        # ── Paso 1: trae product.supplierinfo paginado ──
        from collections import defaultdict
        # {product_tmpl_id: [(partner_id, partner_code, price), ...]}
        si_by_tmpl: dict[int, list[tuple]] = defaultdict(list)
        partner_ids: set[int] = set()

        async with OdooClient() as odoo:
            print("[SuppSync] Pullando product.supplierinfo...")
            offset = 0
            while job["status"] == "running":
                rows = await odoo.search_read(
                    "product.supplierinfo",
                    [],
                    ["id", "product_tmpl_id", "partner_id", "product_code", "price"],
                    offset=offset, limit=batch_size, order="id",
                )
                if not rows:
                    break
                for r in rows:
                    tmpl = r.get("product_tmpl_id")
                    partner = r.get("partner_id")
                    if isinstance(tmpl, list) and tmpl:
                        tmpl_id = tmpl[0]
                    else:
                        continue
                    partner_id = None
                    if isinstance(partner, list) and partner:
                        partner_id = partner[0]
                    if partner_id:
                        partner_ids.add(partner_id)
                    si_by_tmpl[tmpl_id].append((
                        partner_id,
                        r.get("product_code") or "",
                        r.get("price") or 0,
                    ))
                job["supplierinfo_rows"] += len(rows)
                offset += len(rows)
                print(f"[SuppSync] supplierinfo: {job['supplierinfo_rows']} filas...")

            # ── Paso 2: trae res.partner por sus IDs ──
            print(f"[SuppSync] Pullando {len(partner_ids)} res.partner...")
            partner_names: dict[int, str] = {}
            if partner_ids:
                ids_list = list(partner_ids)
                # Chunk para no mandar lista gigante en un solo XML-RPC
                for i in range(0, len(ids_list), 500):
                    chunk = ids_list[i:i+500]
                    rows = await odoo.search_read(
                        "res.partner",
                        [["id", "in", chunk]],
                        ["id", "name"],
                    )
                    for r in rows:
                        partner_names[r["id"]] = (r.get("name") or "").strip()
            job["distinct_partners"] = len(partner_names)

        # ── Paso 3: UPDATE bulk al mirror ──
        print(f"[SuppSync] Aplicando UPDATE a mirror "
              f"({len(si_by_tmpl)} product_tmpl con vendor)...")
        conn = db.get_connection()
        cur = conn.cursor()
        try:
            batch = []
            for tmpl_id, suppliers in si_by_tmpl.items():
                names_seen = []
                ids_seen = []
                for partner_id, _code, _price in suppliers:
                    if not partner_id:
                        continue
                    name = partner_names.get(partner_id)
                    if name and name not in names_seen:
                        names_seen.append(name)
                    if partner_id not in ids_seen:
                        ids_seen.append(partner_id)
                if not ids_seen:
                    continue
                names_str = " | ".join(names_seen)
                ids_json = json.dumps(ids_seen)
                batch.append((ids_json, names_str, len(ids_seen), tmpl_id))

                if len(batch) >= 500:
                    _suppliers_apply_batch(cur, batch)
                    conn.commit()
                    job["products_updated"] += len(batch)
                    batch = []

            if batch:
                _suppliers_apply_batch(cur, batch)
                conn.commit()
                job["products_updated"] += len(batch)
        finally:
            conn.close()

        if job["status"] == "running":
            job["status"] = "completed"
        print(f"[SuppSync] DONE — {job['products_updated']} productos actualizados "
              f"con vendor de Odoo")
    except Exception as e:
        job["status"] = "error"
        err = f"{type(e).__name__}: {e!r}"
        job["errors"].append(err[:200])
        print(f"[SuppSync] Fatal: {err}")

    return job


def _suppliers_apply_batch(cur, batch):
    """UPDATE batch de (ids_json, names_str, count, tmpl_id)."""
    for ids_json, names_str, count, tmpl_id in batch:
        try:
            db.execute_query(cur, """
                UPDATE odoo_books_mirror
                SET supplier_partner_ids = ?,
                    supplier_names = ?,
                    supplier_count = ?,
                    suppliers_synced_at = CURRENT_TIMESTAMP
                WHERE odoo_id = ?
            """, (ids_json, names_str, count, tmpl_id))
        except Exception:
            continue


# ── Inferencia de categorias desde fuentes scrapeadas ─────────────────
infer_job: dict | None = None


def get_infer_status() -> dict:
    job = dict(infer_job) if infer_job else {"status": "idle"}
    job["inferred_count"] = count_inferred_rows()
    if "errors" in job:
        job["errors"] = job["errors"][-5:]
    return job


def count_inferred_rows() -> int:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT COUNT(*) FROM odoo_books_mirror
            WHERE inferred_categories IS NOT NULL
              AND inferred_categories <> ''
        """)
        return cur.fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


def infer_categories_from_scraped() -> dict:
    """
    Llena odoo_books_mirror.inferred_categories con las categorias scrapeadas
    de CDL (tabla books) y/o las del catalogo de distribuidores
    (distributor_books), usando ISBN como join.

    Estrategia: prefer books (scrapeo CDL fresco) > distributor_books (catalogo).
    Categorias se concatenan como "cat1 > cat2 > cat3" (max 5 niveles).
    """
    global infer_job
    infer_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "from_books": 0,
        "from_distributors": 0,
        "no_match": 0,
        "errors": [],
    }

    conn = db.get_connection()
    cur = conn.cursor()

    # Path 0: enrichment_queue.scraped_data (categorias del enricher en JSON)
    # Es la fuente mas autoritativa: data fresca de CDL via el flujo del
    # enricher, que es lo que lleva dias procesando libros.
    infer_job["from_enrichment_queue"] = 0
    print("[Infer] Cruzando con enrichment_queue.scraped_data (enricher)...")
    try:
        if db.IS_POSTGRES:
            cur.execute("""
                UPDATE odoo_books_mirror m
                SET inferred_categories = TRIM(BOTH ' > ' FROM CONCAT_WS(' > ',
                    NULLIF(eq.scraped_data::jsonb->>'categoria_1', ''),
                    NULLIF(eq.scraped_data::jsonb->>'categoria_2', ''),
                    NULLIF(eq.scraped_data::jsonb->>'categoria_3', ''),
                    NULLIF(eq.scraped_data::jsonb->>'categoria_4', ''),
                    NULLIF(eq.scraped_data::jsonb->>'categoria_5', ''))),
                    inferred_source = 'enrichment_queue'
                FROM enrichment_queue eq
                WHERE m.odoo_id = eq.odoo_id
                  AND eq.scraped_data IS NOT NULL
                  AND eq.scraped_data <> ''
                  AND (eq.scraped_data::jsonb->>'categoria_1') IS NOT NULL
                  AND (eq.scraped_data::jsonb->>'categoria_1') <> ''
            """)
            infer_job["from_enrichment_queue"] = cur.rowcount or 0
            conn.commit()
        else:
            cur.execute("""
                SELECT odoo_id, scraped_data FROM enrichment_queue
                WHERE scraped_data IS NOT NULL AND scraped_data <> ''
            """)
            n = 0
            for row in cur.fetchall():
                odoo_id = row[0]
                try:
                    data = json.loads(row[1])
                except Exception:
                    continue
                cats = [data.get(f"categoria_{i}") for i in range(1, 6)]
                cats = [c for c in cats if c and str(c).strip()]
                if not cats:
                    continue
                cur.execute("""
                    UPDATE odoo_books_mirror
                    SET inferred_categories = ?, inferred_source = 'enrichment_queue'
                    WHERE odoo_id = ?
                """, (" > ".join(cats), odoo_id))
                n += 1
            infer_job["from_enrichment_queue"] = n
            conn.commit()
        print(f"[Infer] {infer_job['from_enrichment_queue']} libros desde enricher (JSON)")
    except Exception as e:
        err = f"enrichment_queue: {type(e).__name__}: {e!r}"
        infer_job["errors"].append(err)
        print(f"[Infer] Error: {err}")

    # Path 1: books table (CDL scraped del bulk scraper)
    # Solo donde aun no inferimos (para no pisar al enricher).
    print("[Infer] Cruzando con books (scrapeo CDL bulk)...")
    try:
        if db.IS_POSTGRES:
            cur.execute("""
                UPDATE odoo_books_mirror m
                SET inferred_categories = TRIM(BOTH ' > ' FROM CONCAT_WS(' > ',
                    NULLIF(b.categoria_1, ''),
                    NULLIF(b.categoria_2, ''),
                    NULLIF(b.categoria_3, ''),
                    NULLIF(b.categoria_4, ''),
                    NULLIF(b.categoria_5, ''))),
                    inferred_source = 'cdl_scrape'
                FROM books b
                WHERE m.barcode = b.isbn
                  AND m.barcode IS NOT NULL
                  AND (b.categoria_1 IS NOT NULL AND b.categoria_1 <> '')
                  AND (m.inferred_categories IS NULL OR m.inferred_categories = '')
            """)
            infer_job["from_books"] = cur.rowcount or 0
            conn.commit()
        else:
            # SQLite no soporta UPDATE...FROM, hacemos lookup row-a-row.
            # Solo si la fila no tiene aun inferred_categories.
            cur.execute("""
                SELECT m.odoo_id, m.barcode,
                       b.categoria_1, b.categoria_2, b.categoria_3,
                       b.categoria_4, b.categoria_5
                FROM odoo_books_mirror m
                INNER JOIN books b ON m.barcode = b.isbn
                WHERE b.categoria_1 IS NOT NULL AND b.categoria_1 <> ''
                  AND (m.inferred_categories IS NULL OR m.inferred_categories = '')
            """)
            n = 0
            for row in cur.fetchall():
                odoo_id = row[0]
                cats = [c for c in row[2:7] if c and str(c).strip()]
                if not cats:
                    continue
                full = " > ".join(cats)
                cur.execute("""
                    UPDATE odoo_books_mirror
                    SET inferred_categories = ?, inferred_source = 'cdl_scrape'
                    WHERE odoo_id = ?
                """, (full, odoo_id))
                n += 1
            infer_job["from_books"] = n
            conn.commit()
        print(f"[Infer] {infer_job['from_books']} libros categorizados desde books")
    except Exception as e:
        err = f"books: {type(e).__name__}: {e!r}"
        infer_job["errors"].append(err)
        print(f"[Infer] Error: {err}")

    # Path 2: distributor_books (XLSX catalogo) — solo para los que aun no tienen
    print("[Infer] Cruzando con distributor_books (XLSX)...")
    try:
        if db.IS_POSTGRES:
            cur.execute("""
                UPDATE odoo_books_mirror m
                SET inferred_categories = TRIM(BOTH ' > ' FROM CONCAT_WS(' > ',
                    NULLIF(d.categoria_1, ''),
                    NULLIF(d.categoria_2, ''),
                    NULLIF(d.categoria_3, ''),
                    NULLIF(d.categoria_4, ''),
                    NULLIF(d.categoria_5, ''))),
                    inferred_source = 'distributor:' || COALESCE(d.fuente, 'unknown')
                FROM distributor_books d
                WHERE m.barcode = d.isbn
                  AND m.barcode IS NOT NULL
                  AND (m.inferred_categories IS NULL OR m.inferred_categories = '')
                  AND (d.categoria_1 IS NOT NULL AND d.categoria_1 <> '')
            """)
            infer_job["from_distributors"] = cur.rowcount or 0
            conn.commit()
        else:
            cur.execute("""
                SELECT m.odoo_id, m.barcode, d.fuente,
                       d.categoria_1, d.categoria_2, d.categoria_3,
                       d.categoria_4, d.categoria_5
                FROM odoo_books_mirror m
                INNER JOIN distributor_books d ON m.barcode = d.isbn
                WHERE (m.inferred_categories IS NULL OR m.inferred_categories = '')
                  AND d.categoria_1 IS NOT NULL AND d.categoria_1 <> ''
            """)
            n = 0
            for row in cur.fetchall():
                odoo_id = row[0]
                fuente = row[2] or "unknown"
                cats = [c for c in row[3:8] if c and str(c).strip()]
                if not cats:
                    continue
                full = " > ".join(cats)
                cur.execute("""
                    UPDATE odoo_books_mirror
                    SET inferred_categories = ?, inferred_source = ?
                    WHERE odoo_id = ?
                """, (full, f"distributor:{fuente}", odoo_id))
                n += 1
            infer_job["from_distributors"] = n
            conn.commit()
        print(f"[Infer] {infer_job['from_distributors']} libros categorizados desde distribuidores")
    except Exception as e:
        err = f"distributors: {type(e).__name__}: {e!r}"
        infer_job["errors"].append(err)
        print(f"[Infer] Error: {err}")

    # Conteo final de sin-match
    try:
        cur.execute("""
            SELECT COUNT(*) FROM odoo_books_mirror
            WHERE inferred_categories IS NULL OR inferred_categories = ''
        """)
        infer_job["no_match"] = cur.fetchone()[0]
    except Exception:
        pass

    conn.close()
    infer_job["status"] = "completed"
    print(f"[Infer] DONE — books:{infer_job['from_books']} "
          f"distribuidores:{infer_job['from_distributors']} "
          f"sin_match:{infer_job['no_match']}")
    return infer_job


def count_public_categories() -> int:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, "SELECT COUNT(*) FROM odoo_public_categories")
        return cur.fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


# ── Push de categorias a Odoo (product.category) ──────────────────────
push_categ_job: dict | None = None
assign_categ_job: dict | None = None


def get_push_categ_status() -> dict:
    job = dict(push_categ_job) if push_categ_job else {"status": "idle"}
    job["cached_paths"] = _count_pushed_categories()
    if "errors" in job:
        job["errors"] = job["errors"][-10:]
    return job


def get_assign_categ_status() -> dict:
    job = dict(assign_categ_job) if assign_categ_job else {"status": "idle"}
    if "errors" in job:
        job["errors"] = job["errors"][-10:]
    return job


def _count_pushed_categories() -> int:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, "SELECT COUNT(*) FROM odoo_product_categories_cache")
        return cur.fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


def _lookup_cached_path(full_path: str) -> int | None:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur,
            "SELECT odoo_categ_id FROM odoo_product_categories_cache WHERE full_path = ?",
            (full_path,))
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        conn.close()


def _save_cached_path(full_path: str, categ_id: int, name: str, parent_path: str | None):
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        if db.IS_POSTGRES:
            db.execute_query(cur, """
                INSERT INTO odoo_product_categories_cache
                    (full_path, odoo_categ_id, name, parent_path, created_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (full_path) DO UPDATE SET
                    odoo_categ_id = EXCLUDED.odoo_categ_id,
                    name = EXCLUDED.name,
                    parent_path = EXCLUDED.parent_path
            """, (full_path, categ_id, name, parent_path))
        else:
            db.execute_query(cur, """
                INSERT OR REPLACE INTO odoo_product_categories_cache
                    (full_path, odoo_categ_id, name, parent_path, created_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (full_path, categ_id, name, parent_path))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def _get_distinct_inferred_paths() -> list[str]:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT DISTINCT inferred_categories
            FROM odoo_books_mirror
            WHERE inferred_categories IS NOT NULL
              AND inferred_categories <> ''
        """)
        return [r[0] for r in cur.fetchall() if r[0]]
    finally:
        conn.close()


async def _ensure_category(odoo: OdooClient, name: str,
                            parent_id: int | bool) -> int:
    """
    Devuelve el id de la product.category con (name, parent_id). Si no
    existe en Odoo la crea. Si parent_id es None, se trata como root (False).
    """
    p = parent_id if isinstance(parent_id, int) else False
    # Buscar primero
    domain = [["name", "=", name]]
    if p:
        domain.append(["parent_id", "=", p])
    else:
        domain.append(["parent_id", "=", False])
    existing = await odoo.search_read(
        "product.category", domain, ["id"], limit=1,
    )
    if existing:
        return existing[0]["id"]
    # Crear
    create_vals = {"name": name}
    if p:
        create_vals["parent_id"] = p
    new_id = await odoo.execute_kw(
        "product.category", "create", [create_vals]
    )
    return new_id


async def push_categories_to_odoo() -> dict:
    """
    Crea/encuentra en Odoo todas las product.category necesarias para
    cubrir las inferred_categories del mirror. Cachea cada path -> id en
    odoo_product_categories_cache para idempotencia.
    """
    global push_categ_job
    push_categ_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "distinct_paths": 0,
        "created_or_found": 0,
        "from_cache": 0,
        "errors": [],
    }

    try:
        paths = _get_distinct_inferred_paths()
        push_categ_job["distinct_paths"] = len(paths)
        print(f"[PushCat] {len(paths)} paths unicos a procesar")

        if not paths:
            push_categ_job["status"] = "completed"
            return push_categ_job

        # Memo en memoria — clave: full_path, valor: odoo_categ_id
        path_to_id: dict[str, int] = {}

        async with OdooClient() as odoo:
            for path in paths:
                if push_categ_job["status"] != "running":
                    break
                parts = [p.strip() for p in path.split(" > ") if p.strip()]
                if not parts:
                    continue
                parent_id: int | bool = False
                for i, part in enumerate(parts):
                    full = " > ".join(parts[: i + 1])
                    if full in path_to_id:
                        parent_id = path_to_id[full]
                        continue
                    cached = _lookup_cached_path(full)
                    if cached:
                        path_to_id[full] = cached
                        push_categ_job["from_cache"] += 1
                        parent_id = cached
                        continue
                    try:
                        cid = await _ensure_category(odoo, part, parent_id)
                    except Exception as e:
                        err = f"path '{full}': {type(e).__name__}: {e!r}"
                        push_categ_job["errors"].append(err[:200])
                        print(f"[PushCat] {err}")
                        break
                    path_to_id[full] = cid
                    parent_path = " > ".join(parts[:i]) if i > 0 else None
                    _save_cached_path(full, cid, part, parent_path)
                    push_categ_job["created_or_found"] += 1
                    parent_id = cid

        push_categ_job["status"] = "completed"
        print(f"[PushCat] DONE: {push_categ_job['created_or_found']} creadas/encontradas, "
              f"{push_categ_job['from_cache']} desde cache local")
    except Exception as e:
        push_categ_job["status"] = "error"
        err = f"{type(e).__name__}: {e!r}"
        push_categ_job["errors"].append(err[:200])
        print(f"[PushCat] Fatal: {err}")

    return push_categ_job


def stop_push_categories():
    global push_categ_job
    if push_categ_job and push_categ_job.get("status") == "running":
        push_categ_job["status"] = "stopped"
        return True
    return False


async def assign_books_to_odoo_categories(batch_size: int = 100) -> dict:
    """
    Para cada libro en odoo_books_mirror con inferred_categories, asigna su
    product.template.categ_id al ID de la categoria leaf correspondiente en
    Odoo. Usa el cache odoo_product_categories_cache para resolver paths.
    """
    global assign_categ_job
    assign_categ_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "to_assign": 0,
        "assigned": 0,
        "skipped_no_cache": 0,
        "errors": [],
    }

    # Leer todos los libros con categoria inferida y traer su path
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT odoo_id, inferred_categories
            FROM odoo_books_mirror
            WHERE inferred_categories IS NOT NULL
              AND inferred_categories <> ''
            ORDER BY odoo_id
        """)
        rows = cur.fetchall()
    finally:
        conn.close()

    assign_categ_job["to_assign"] = len(rows)
    print(f"[AssignCat] {len(rows)} libros a asignar")

    # Pre-cargar TODO el cache de paths a memoria (rapido, max ~50k entries)
    conn = db.get_connection()
    cur = conn.cursor()
    path_to_id: dict[str, int] = {}
    try:
        db.execute_query(cur,
            "SELECT full_path, odoo_categ_id FROM odoo_product_categories_cache")
        for row in cur.fetchall():
            path_to_id[row[0]] = row[1]
    finally:
        conn.close()
    print(f"[AssignCat] Cache local con {len(path_to_id)} paths")

    if not path_to_id:
        assign_categ_job["status"] = "error"
        err = "Cache vacio. Corre push_categories_to_odoo() primero."
        assign_categ_job["errors"].append(err)
        print(f"[AssignCat] {err}")
        return assign_categ_job

    # Agrupar libros por leaf categ_id para batchear writes
    by_cat: dict[int, list[int]] = {}
    for odoo_id, path in rows:
        cat_id = path_to_id.get(path)
        if not cat_id:
            assign_categ_job["skipped_no_cache"] += 1
            continue
        by_cat.setdefault(cat_id, []).append(odoo_id)

    print(f"[AssignCat] Agrupados en {len(by_cat)} categorias distintas")

    try:
        async with OdooClient() as odoo:
            for cat_id, odoo_ids in by_cat.items():
                if assign_categ_job["status"] != "running":
                    break
                # Batchear writes
                for i in range(0, len(odoo_ids), batch_size):
                    chunk = odoo_ids[i:i + batch_size]
                    try:
                        await odoo.write("product.template", chunk, {"categ_id": cat_id})
                        assign_categ_job["assigned"] += len(chunk)
                    except Exception as e:
                        err = f"cat {cat_id}: {type(e).__name__}: {e!r}"
                        assign_categ_job["errors"].append(err[:200])
                        print(f"[AssignCat] {err}")
                if assign_categ_job["assigned"] % 1000 == 0:
                    print(f"[AssignCat] {assign_categ_job['assigned']}/{assign_categ_job['to_assign']}")

        if assign_categ_job["status"] == "running":
            assign_categ_job["status"] = "completed"
        print(f"[AssignCat] DONE: asignados {assign_categ_job['assigned']}, "
              f"skipped {assign_categ_job['skipped_no_cache']}")
    except Exception as e:
        assign_categ_job["status"] = "error"
        err = f"{type(e).__name__}: {e!r}"
        assign_categ_job["errors"].append(err[:200])
        print(f"[AssignCat] Fatal: {err}")

    return assign_categ_job


def stop_assign_categories():
    global assign_categ_job
    if assign_categ_job and assign_categ_job.get("status") == "running":
        assign_categ_job["status"] = "stopped"
        return True
    return False


# ── Bulk fill desde Google Books (rapido, sin Playwright) ─────────────
gbooks_fill_job: dict | None = None


def get_gbooks_fill_status() -> dict:
    job = dict(gbooks_fill_job) if gbooks_fill_job else {"status": "idle"}
    if "errors" in job:
        job["errors"] = job["errors"][-10:]
    return job


def stop_gbooks_fill():
    global gbooks_fill_job
    if gbooks_fill_job and gbooks_fill_job.get("status") == "running":
        gbooks_fill_job["status"] = "stopped"
        return True
    return False


def _gbooks_needs_fill_count() -> int:
    """Cuantos libros necesitan datos de Google Books (en este shard)."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT COUNT(*) FROM odoo_books_mirror
            WHERE barcode IS NOT NULL
              AND barcode <> ''
              AND gbooks_fetched_at IS NULL
              AND {_shard_clause()}
        """)
        return cur.fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


def _gbooks_fetch_targets(limit: int = 1000) -> list[tuple[int, str]]:
    """Lista (odoo_id, barcode) para libros sin gbooks_fetched_at en este shard."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT odoo_id, barcode FROM odoo_books_mirror
            WHERE barcode IS NOT NULL
              AND barcode <> ''
              AND gbooks_fetched_at IS NULL
              AND {_shard_clause()}
            ORDER BY odoo_id
            LIMIT ?
        """, (limit,))
        return [(r[0], r[1]) for r in cur.fetchall()]
    finally:
        conn.close()


def _gbooks_save(odoo_id: int, gb_data: dict | None) -> bool:
    """
    Persiste lo que Google Books devolvio. Marca fetched_at incluso si no hubo
    match (None) — asi no se re-pregunta el mismo ISBN cada corrida.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        if gb_data is None:
            # No match — solo marca fetched para no reintentarlo
            db.execute_query(cur, """
                UPDATE odoo_books_mirror
                SET gbooks_fetched_at = CURRENT_TIMESTAMP
                WHERE odoo_id = ?
            """, (odoo_id,))
            conn.commit()
            return False

        # Categorias: si gb tiene, llenamos solo si esta vacio
        cats = gb_data.get("categories") or []
        cats_str = " > ".join([c for c in cats if c]) if cats else None
        # Description: solo si no hay
        desc = gb_data.get("description") or None
        # Otros
        publisher = gb_data.get("editorial") or None
        language = gb_data.get("language") or None
        try:
            pages = int(gb_data.get("pages") or 0) or None
        except (ValueError, TypeError):
            pages = None
        thumb = gb_data.get("image_url") or None

        # UPDATE condicional: rellenamos solo huecos (COALESCE)
        if db.IS_POSTGRES:
            db.execute_query(cur, """
                UPDATE odoo_books_mirror
                SET description = COALESCE(NULLIF(description, ''), ?),
                    inferred_categories = COALESCE(NULLIF(inferred_categories, ''), ?),
                    inferred_source = CASE
                        WHEN inferred_categories IS NULL OR inferred_categories = ''
                            THEN 'google_books'
                        ELSE inferred_source
                    END,
                    gbooks_publisher = ?,
                    gbooks_language = ?,
                    gbooks_pages = ?,
                    gbooks_thumbnail = ?,
                    gbooks_fetched_at = CURRENT_TIMESTAMP
                WHERE odoo_id = ?
            """, (desc, cats_str, publisher, language, pages, thumb, odoo_id))
        else:
            # SQLite: COALESCE tambien soportado
            db.execute_query(cur, """
                UPDATE odoo_books_mirror
                SET description = COALESCE(NULLIF(description, ''), ?),
                    inferred_categories = COALESCE(NULLIF(inferred_categories, ''), ?),
                    inferred_source = CASE
                        WHEN inferred_categories IS NULL OR inferred_categories = ''
                            THEN 'google_books'
                        ELSE inferred_source
                    END,
                    gbooks_publisher = ?,
                    gbooks_language = ?,
                    gbooks_pages = ?,
                    gbooks_thumbnail = ?,
                    gbooks_fetched_at = CURRENT_TIMESTAMP
                WHERE odoo_id = ?
            """, (desc, cats_str, publisher, language, pages, thumb, odoo_id))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


async def fill_from_google_books(concurrency: int = 15,
                                  chunk_size: int = 1000) -> dict:
    """
    Itera todos los libros del mirror sin gbooks_fetched_at y los enriquece
    con Google Books API (categorias, description, idioma, editorial,
    paginas, thumbnail). Async puro, sin Playwright — 100-1000x mas rapido
    que CDL.

    Trabaja en chunks de 1000 books con semaphore de N requests en
    paralelo. Marca fetched_at en cada uno (incluso si no hay match) para
    no reintentar.

    Estimado:
      - Sin API key:  ~1000 libros/dia (free tier limit Google)
      - Con API key:  ~100k libros/dia (subir GOOGLE_BOOKS_API_KEY)
    """
    import aiohttp
    import google_books

    global gbooks_fill_job
    gbooks_fill_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "concurrency": concurrency,
        "target": _gbooks_needs_fill_count(),
        "processed": 0,
        "matched": 0,
        "no_match": 0,
        "errors": [],
    }
    job = gbooks_fill_job
    print(f"[GBFill] Target: {job['target']} libros sin datos de Google Books")

    if job["target"] == 0:
        job["status"] = "completed"
        return job

    sem = asyncio.Semaphore(concurrency)

    async def _process_one(session, odoo_id, isbn):
        if job["status"] != "running":
            return
        async with sem:
            try:
                data = await google_books.fetch_by_isbn(session, isbn, timeout_s=10.0)
            except google_books.GoogleBooksRateLimitError as e:
                # Cuota Google agotada/rate limit - NO marcar fetched
                # asi el libro queda disponible para reintento mas tarde
                err = f"RATE LIMIT: {str(e)[:200]}"
                job["errors"].append(err)
                job["rate_limit_hits"] = job.get("rate_limit_hits", 0) + 1
                # Stop el job globalmente para no quemar mas libros
                if job["rate_limit_hits"] >= 3:
                    job["status"] = "rate_limited"
                    print(f"[GBFill] STOPPED — rate limit detectado (>=3 hits). "
                          f"Cuota Google agotada. Espera al reset (~24h) o pide aumento.")
                return
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:80]}"
                job["errors"].append(f"isbn {isbn}: {err}")
                # Error generico - marcamos fetched para no reintentarlo
                _gbooks_save(odoo_id, None)
                job["processed"] += 1
                job["no_match"] += 1
                return
            ok = _gbooks_save(odoo_id, data)
            job["processed"] += 1
            if ok:
                job["matched"] += 1
            else:
                job["no_match"] += 1

    try:
        async with aiohttp.ClientSession() as session:
            while job["status"] == "running":
                targets = _gbooks_fetch_targets(limit=chunk_size)
                if not targets:
                    print("[GBFill] Sin mas targets — fin")
                    break

                tasks = [_process_one(session, oid, isbn) for oid, isbn in targets]
                await asyncio.gather(*tasks, return_exceptions=True)

                pct = (job["processed"] / job["target"] * 100) if job["target"] else 0
                print(f"[GBFill] {job['processed']}/{job['target']} "
                      f"({pct:.1f}%) — match:{job['matched']} no:{job['no_match']}")

        if job["status"] == "running":
            job["status"] = "completed"
        print(f"[GBFill] DONE — processed:{job['processed']} "
              f"matched:{job['matched']} no_match:{job['no_match']}")
    except Exception as e:
        job["status"] = "error"
        err = f"{type(e).__name__}: {e!r}"
        job["errors"].append(err[:200])
        print(f"[GBFill] Fatal: {err}")

    return job


def reset_recent_gbooks_fetched(hours_back: int = 24) -> int:
    """
    Resetea gbooks_fetched_at = NULL para libros marcados como fetched
    en las ultimas N horas que NO tienen data de Google Books real.
    Esos son los "falsos no-match" producidos cuando Google estaba en
    rate limit pero igual marcabamos como procesado.

    Util tras un rate limit: corres esto y los libros estan listos para
    reintentar despues de que se renueve la cuota.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        if db.IS_POSTGRES:
            cur.execute(
                f"""
                UPDATE odoo_books_mirror
                SET gbooks_fetched_at = NULL
                WHERE gbooks_fetched_at IS NOT NULL
                  AND gbooks_fetched_at >= NOW() - INTERVAL '{int(hours_back)} hours'
                  AND (gbooks_publisher IS NULL OR gbooks_publisher = '')
                  AND (gbooks_language IS NULL OR gbooks_language = '')
                  AND gbooks_pages IS NULL
                  AND (gbooks_thumbnail IS NULL OR gbooks_thumbnail = '')
                """
            )
        else:
            cur.execute(
                f"""
                UPDATE odoo_books_mirror
                SET gbooks_fetched_at = NULL
                WHERE gbooks_fetched_at IS NOT NULL
                  AND datetime(gbooks_fetched_at) >= datetime('now', '-{int(hours_back)} hours')
                  AND (gbooks_publisher IS NULL OR gbooks_publisher = '')
                  AND (gbooks_language IS NULL OR gbooks_language = '')
                  AND gbooks_pages IS NULL
                  AND (gbooks_thumbnail IS NULL OR gbooks_thumbnail = '')
                """
            )
        n = cur.rowcount or 0
        conn.commit()
        print(f"[Reset] Reseteados {n} libros con gbooks_fetched_at falso (ultimas {hours_back}h)")
        return n
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[Reset] Error: {type(e).__name__}: {e!r}")
        return 0
    finally:
        conn.close()


# ── Bulk fill desde CDL (Casa del Libro) — usa proxies + Playwright ──
cdl_fill_job: dict | None = None


def get_cdl_fill_status() -> dict:
    job = dict(cdl_fill_job) if cdl_fill_job else {"status": "idle"}
    if "errors" in job:
        job["errors"] = job["errors"][-10:]
    return job


def stop_cdl_fill():
    global cdl_fill_job
    if cdl_fill_job and cdl_fill_job.get("status") == "running":
        cdl_fill_job["status"] = "stopped"
        return True
    return False


def _cdl_needs_fill_count() -> int:
    """Cuantos libros del sitemap aun no tienen categoria inferida (en este shard)."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT COUNT(*) FROM odoo_books_mirror m
            INNER JOIN cdl_isbn_index ci ON m.barcode = ci.isbn
            WHERE m.barcode IS NOT NULL
              AND m.barcode <> ''
              AND (m.inferred_categories IS NULL OR m.inferred_categories = '')
              AND m.cdl_fetched_at IS NULL
              AND {_shard_clause('m.odoo_id')}
        """)
        return cur.fetchone()[0]
    except Exception as e:
        print(f"[CDLFill] _cdl_needs_fill_count FAILED: {type(e).__name__}: {e}")
        try: conn.rollback()
        except Exception: pass
        return 0
    finally:
        conn.close()


def _cdl_fetch_targets(limit: int = 500) -> list[tuple[int, str, str]]:
    """Lista (odoo_id, isbn, url) para libros del sitemap sin categoria (en este shard)."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT m.odoo_id, m.barcode, ci.url
            FROM odoo_books_mirror m
            INNER JOIN cdl_isbn_index ci ON m.barcode = ci.isbn
            WHERE m.barcode IS NOT NULL
              AND m.barcode <> ''
              AND (m.inferred_categories IS NULL OR m.inferred_categories = '')
              AND m.cdl_fetched_at IS NULL
              AND {_shard_clause('m.odoo_id')}
            ORDER BY m.odoo_id
            LIMIT ?
        """, (limit,))
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]
    finally:
        conn.close()


def _cdl_save_one(odoo_id: int, isbn: str, data: dict | None):
    """
    Guarda lo scrapeado en books table Y espejea TODOS los campos al mirror.
    Cada UPDATE usa COALESCE(NULLIF(col,''), nuevo) para no pisar lo que
    ya estaba. Marca cdl_fetched_at siempre (aunque no haya match).
    """
    from scraper import save_to_db

    if data and data.get("title") and data.get("title") != "Unknown Title":
        try:
            data["search_query"] = isbn
            save_to_db(data)
        except Exception:
            pass

    # Helpers: convertir vacios/Unknown a None para que COALESCE los ignore
    def _v(key):
        v = (data or {}).get(key)
        if v is None:
            return None
        s = str(v).strip()
        if s == "" or s.lower() in ("unknown", "unknown title", "unknown author",
                                     "unknown price", "no description"):
            return None
        return s

    # Categorias del scrape -> string jerarquico
    cats_str = None
    if data:
        cats = [data.get(f"categoria_{i}") for i in range(1, 6)]
        cats = [str(c).strip() for c in cats if c and str(c).strip()]
        if cats:
            cats_str = " > ".join(cats)

    sql = """
        UPDATE odoo_books_mirror
        SET inferred_categories = COALESCE(NULLIF(inferred_categories, ''), ?),
            inferred_source = CASE
                WHEN (inferred_categories IS NULL OR inferred_categories = '')
                     AND ? IS NOT NULL THEN 'cdl_bulk'
                ELSE inferred_source
            END,
            description       = COALESCE(NULLIF(description, ''),       ?),
            cdl_author        = COALESCE(NULLIF(cdl_author, ''),        ?),
            cdl_editorial     = COALESCE(NULLIF(cdl_editorial, ''),     ?),
            cdl_image_url     = COALESCE(NULLIF(cdl_image_url, ''),     ?),
            cdl_weight        = COALESCE(NULLIF(cdl_weight, ''),        ?),
            cdl_height        = COALESCE(NULLIF(cdl_height, ''),        ?),
            cdl_width         = COALESCE(NULLIF(cdl_width, ''),         ?),
            cdl_binding       = COALESCE(NULLIF(cdl_binding, ''),       ?),
            cdl_translator    = COALESCE(NULLIF(cdl_translator, ''),    ?),
            cdl_illustrator   = COALESCE(NULLIF(cdl_illustrator, ''),   ?),
            cdl_collection    = COALESCE(NULLIF(cdl_collection, ''),    ?),
            cdl_pages         = COALESCE(NULLIF(cdl_pages, ''),         ?),
            cdl_release_date  = COALESCE(NULLIF(cdl_release_date, ''),  ?),
            cdl_url           = COALESCE(NULLIF(cdl_url, ''),           ?),
            cdl_price         = COALESCE(NULLIF(cdl_price, ''),         ?),
            cdl_language      = COALESCE(NULLIF(cdl_language, ''),      ?),
            cdl_fetched_at    = CURRENT_TIMESTAMP
        WHERE odoo_id = ?
    """
    params = (
        cats_str, cats_str,
        _v("description"),
        _v("author"),
        _v("editorial"),
        _v("image_url"),
        _v("weight"),
        _v("height"),
        _v("width"),
        _v("binding"),
        _v("translator"),
        _v("illustrator"),
        _v("collection"),
        _v("pages"),
        _v("release_date"),
        _v("url"),
        _v("price"),
        _v("language"),
        odoo_id,
    )

    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, sql, params)
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[CDLSave] FAILED odoo_id={odoo_id} isbn={isbn}: {e}")
    finally:
        conn.close()


async def fill_from_cdl_mirror(chunk_size: int = 500) -> dict:
    """
    Scrape CDL para libros del mirror que estan en cdl_isbn_index (tienen
    direct URL, fast path). Guarda a books table + actualiza mirror con
    categorias y description. Usa launch_browser_pool (PROXY_POOL).

    Diferencia con el enricher:
      - No empuja a Odoo (mas rapido, solo enriquece local)
      - Trabaja sobre el mirror entero, no solo description_sale=False
      - Marca cdl_fetched_at para no retrabajar
    """
    from playwright.async_api import async_playwright
    from scraper import launch_browser_pool, close_browser_pool, scrape_book, PROXY_POOL

    global cdl_fill_job
    cdl_fill_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "target": _cdl_needs_fill_count(),
        "processed": 0,
        "matched": 0,
        "no_match": 0,
        "transient_skipped": 0,
        "errors": [],
    }
    job = cdl_fill_job
    print(f"[CDLFill] Target: {job['target']} libros en cdl_isbn_index sin procesar")

    if job["target"] == 0:
        job["status"] = "completed"
        return job

    n_proxies = max(1, len(PROXY_POOL))
    pool_size = max(n_proxies * 2, 12)  # 2 paginas por proxy minimo, max 24

    try:
        async with async_playwright() as p:
            browsers, page_queue = await launch_browser_pool(p, pool_size)
            print(f"[CDLFill] Browser pool con {pool_size} paginas")

            sem = asyncio.Semaphore(pool_size)
            # Errores que indican "proxy/red rota" — no marcar fetched, reintentar
            TRANSIENT_PATTERNS = (
                "ERR_TUNNEL_CONNECTION_FAILED",
                "ERR_PROXY_CONNECTION_FAILED",
                "ERR_CONNECTION_RESET",
                "ERR_CONNECTION_CLOSED",
                "ERR_CONNECTION_REFUSED",
                "ERR_TIMED_OUT",
                "ERR_EMPTY_RESPONSE",
                "Timeout",
            )

            import proxy_health
            async def _process_one(odoo_id, isbn, url):
                if job["status"] != "running":
                    return
                async with sem:
                    page = await page_queue.get()
                    proxy_spec = getattr(page, '_proxy_spec', None)
                    try:
                        data = await scrape_book(page, query=isbn, direct_url=url)
                        if data and data.get("title") and data.get("title") != "Unknown Title":
                            _cdl_save_one(odoo_id, isbn, data)
                            job["matched"] += 1
                            proxy_health.mark_success(proxy_spec)
                        else:
                            # CDL respondio pero sin libro. Si esto pasa muchas veces
                            # seguidas en una proxy, es soft-block (mark_no_match
                            # acumula y marca dead tras SOFT_BLOCK_THRESHOLD).
                            _cdl_save_one(odoo_id, isbn, None)
                            job["no_match"] += 1
                            proxy_health.mark_no_match(proxy_spec)
                    except Exception as e:
                        err_str = str(e)
                        err_short = f"{type(e).__name__}: {err_str[:80]}"
                        job["errors"].append(f"isbn {isbn}: {err_short}")
                        is_transient = any(p in err_str for p in TRANSIENT_PATTERNS)
                        if is_transient:
                            job["transient_skipped"] = job.get("transient_skipped", 0) + 1
                            proxy_health.mark_failed(proxy_spec, err_short)
                        else:
                            _cdl_save_one(odoo_id, isbn, None)
                            job["no_match"] += 1
                    finally:
                        if proxy_health.is_alive(proxy_spec):
                            await page_queue.put(page)
                        job["processed"] += 1

            while job["status"] == "running":
                targets = _cdl_fetch_targets(limit=chunk_size)
                if not targets:
                    print("[CDLFill] Sin mas targets — fin")
                    break

                # Si el queue se vacio (todas las proxies murieron y las pages
                # no se devolvieron al queue), relanzar el pool. launch_browser_pool
                # filtra automaticamente las muertas y cae a IP directa si no
                # quedan vivas. Asi el job NO se cuelga.
                if page_queue.qsize() == 0:
                    print(f"[CDLFill] Pool vacio — relanzando "
                          f"(probablemente todas las proxies estan dead)")
                    await close_browser_pool(browsers)
                    browsers, page_queue = await launch_browser_pool(p, pool_size)

                tasks = [_process_one(oid, isbn, url) for oid, isbn, url in targets]
                await asyncio.gather(*tasks, return_exceptions=True)

                pct = (job["processed"] / job["target"] * 100) if job["target"] else 0
                print(f"[CDLFill] {job['processed']}/{job['target']} "
                      f"({pct:.1f}%) match:{job['matched']} no:{job['no_match']}")

            await close_browser_pool(browsers)

        if job["status"] == "running":
            job["status"] = "completed"
        print(f"[CDLFill] DONE — processed:{job['processed']} matched:{job['matched']}")
    except Exception as e:
        job["status"] = "error"
        err = f"{type(e).__name__}: {e!r}"
        job["errors"].append(err[:200])
        print(f"[CDLFill] Fatal: {err}")

    return job


# ── CSV streaming export ───────────────────────────────────────────────
def _truncate(v, n):
    if v is None:
        return ""
    s = str(v)
    return s[:n] + ("..." if len(s) > n else "")


def export_csv_streaming(only_with_categories: bool = False):
    """
    Export COMPLETO: join odoo_books_mirror con books (scrapeo CDL) y
    distributor_books (XLSX) para sacar el dato mas rico posible por
    libro. Coalesce: mirror > books > distribuidores > GoogleBooks.
    Streamea para no cargar a memoria.
    """
    import csv
    import io

    where_clause = ""
    if only_with_categories:
        where_clause = ("WHERE m.inferred_categories IS NOT NULL "
                        "AND m.inferred_categories <> ''")

    conn = db.get_connection()
    cur = conn.cursor()
    # IMPORTANTE: COALESCE en Postgres exige que TODOS los argumentos sean
    # del mismo tipo. m.list_price y d.price son NUMERIC, b.price es TEXT,
    # m.gbooks_pages es INTEGER. Sin CAST a TEXT el query lanza
    # "UNION types numeric and text cannot be matched" antes del primer
    # yield -> CSV de 0 bytes y sin error visible.
    try:
        db.execute_query(cur, f"""
            SELECT
                m.odoo_id,
                m.barcode,
                COALESCE(NULLIF(m.name, ''), b.title, d.title) AS titulo,
                COALESCE(NULLIF(m.cdl_author, ''), NULLIF(b.author, ''),
                         NULLIF(d.author, '')) AS autor,
                COALESCE(NULLIF(m.cdl_editorial, ''), NULLIF(b.editorial, ''),
                         NULLIF(d.editorial, ''), NULLIF(m.gbooks_publisher, '')) AS editorial,
                COALESCE(NULLIF(m.cdl_price, ''), CAST(m.list_price AS TEXT),
                         NULLIF(b.price, ''), CAST(d.price AS TEXT)) AS precio,
                COALESCE(NULLIF(m.cdl_language, ''), NULLIF(b.language, ''),
                         NULLIF(d.language, ''), NULLIF(m.gbooks_language, '')) AS idioma,
                COALESCE(m.inferred_categories, '') AS categorias,
                COALESCE(m.inferred_source, '') AS fuente_categoria,
                COALESCE(NULLIF(m.description, ''), NULLIF(b.description, ''),
                         NULLIF(d.description, '')) AS descripcion,
                COALESCE(NULLIF(m.cdl_pages, ''), NULLIF(b.pages, ''),
                         NULLIF(d.pages, ''), CAST(m.gbooks_pages AS TEXT)) AS paginas,
                COALESCE(NULLIF(m.cdl_binding, ''), NULLIF(b.binding, ''),
                         NULLIF(d.binding, '')) AS encuadernacion,
                COALESCE(NULLIF(m.cdl_translator, ''), NULLIF(b.translator, ''),
                         NULLIF(d.translator, '')) AS traductor,
                COALESCE(NULLIF(m.cdl_illustrator, ''), NULLIF(b.illustrator, ''),
                         NULLIF(d.illustrator, '')) AS ilustrador,
                COALESCE(NULLIF(m.cdl_weight, ''), NULLIF(b.weight, ''),
                         NULLIF(d.weight, '')) AS peso,
                COALESCE(NULLIF(m.cdl_height, ''), NULLIF(b.height, ''),
                         NULLIF(d.height, '')) AS alto,
                COALESCE(NULLIF(m.cdl_width, ''), NULLIF(b.width, ''),
                         NULLIF(d.width, '')) AS ancho,
                COALESCE(NULLIF(m.cdl_image_url, ''), NULLIF(b.image_url, ''),
                         NULLIF(d.image_url, ''), NULLIF(m.gbooks_thumbnail, '')) AS imagen,
                COALESCE(NULLIF(m.cdl_release_date, ''), NULLIF(b.release_date, ''),
                         NULLIF(d.release_date, '')) AS fecha_publicacion,
                COALESCE(NULLIF(m.cdl_collection, ''), NULLIF(b.collection, ''),
                         NULLIF(d.collection, '')) AS coleccion,
                COALESCE(NULLIF(m.cdl_url, ''), NULLIF(b.url, ''),
                         NULLIF(d.url, '')) AS url_cdl,
                d.fuente AS distribuidor,
                COALESCE(NULLIF(m.supplier_names, ''), '') AS proveedor_odoo,
                m.supplier_count AS proveedores_odoo_count,
                m.categ_id AS odoo_categ_id,
                m.categ_name AS odoo_categ_name,
                m.synced_at
            FROM odoo_books_mirror m
            LEFT JOIN books b ON m.barcode = b.isbn
            LEFT JOIN distributor_books d ON m.barcode = d.isbn
            {where_clause}
            ORDER BY m.odoo_id
        """)
    except Exception as e:
        # Log y rollback para no dejar la conexion en estado abortado
        err = f"{type(e).__name__}: {e!r}"
        print(f"[Export] Query FALLO: {err}")
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        # Emitir CSV minimo con headers y una fila de error para que el
        # navegador descargue algo en vez de 0 bytes silenciosos
        import csv as _csv
        import io as _io
        _buf = _io.StringIO()
        _w = _csv.writer(_buf)
        _w.writerow(["ERROR"])
        _w.writerow([err])
        yield _buf.getvalue()
        return

    headers = [
        "odoo_id", "isbn", "titulo", "autor", "editorial", "precio",
        "idioma", "categorias", "fuente_categoria",
        "descripcion", "paginas", "encuadernacion",
        "traductor", "ilustrador",
        "peso", "alto", "ancho", "imagen",
        "fecha_publicacion", "coleccion", "url_cdl",
        "distribuidor", "proveedor_odoo", "proveedores_odoo_count",
        "odoo_categ_id", "odoo_categ_name", "sincronizado",
    ]
    # Excel-friendly:
    #  - BOM UTF-8 al inicio (﻿) -> Excel detecta encoding, evita Ã©
    #  - delimiter ';' -> Excel locale ES auto-detecta columnas; con ',' las
    #    apila todas en la columna A porque ',' es separador decimal en ES
    #  - QUOTE_ALL -> cualquier ; , " o salto de linea dentro de un campo
    #    queda escapado y no rompe la fila
    #  - lineterminator '\r\n' -> formato Windows que Excel prefiere
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';', quoting=csv.QUOTE_ALL,
                   lineterminator='\r\n')
    w.writerow(headers)
    first = buf.getvalue()
    buf.seek(0); buf.truncate()
    # BOM UTF-8 antes de la primera linea
    yield '﻿' + first

    desc_idx = headers.index("descripcion")
    try:
        while True:
            rows = cur.fetchmany(1000)
            if not rows:
                break
            for r in rows:
                row_list = list(r)
                # Truncar descripcion para evitar celdas > 32k char (limite Excel)
                row_list[desc_idx] = _truncate(row_list[desc_idx], 2000)
                # Limpiar saltos de linea raros DENTRO de las celdas (mantiene
                # los normales \n que csv.QUOTE_ALL escapa solo)
                for i, v in enumerate(row_list):
                    if isinstance(v, str) and '\x00' in v:
                        row_list[i] = v.replace('\x00', '')
                w.writerow(row_list)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    finally:
        conn.close()


# ── CDL Search Fill (via busqueda directa por ISBN, sin cdl_isbn_index) ─
# Diferencia clave con fill_from_cdl_mirror:
#   - mirror: solo libros en cdl_isbn_index (sitemap, ~539k coverage)
#   - search: TODOS los libros del mirror sin cdl_fetched_at, busca por ISBN
#     en CDL via /?query=<isbn>. Mas lento (2 navs por libro) pero cobertura
#     completa del mirror.
cdl_search_fill_job: dict | None = None


def get_cdl_search_fill_status() -> dict:
    job = dict(cdl_search_fill_job) if cdl_search_fill_job else {"status": "idle"}
    if "errors" in job:
        job["errors"] = job["errors"][-10:]
    return job


def stop_cdl_search_fill():
    global cdl_search_fill_job
    if cdl_search_fill_job and cdl_search_fill_job.get("status") == "running":
        cdl_search_fill_job["status"] = "stopped"
        return True
    return False


# Condicion canonica: libro incompleto = sin categoria.
# (la descripcion no basta — sin categoria el libro no es usable para el
# catalogo, asi que aunque tenga descripcion lo re-scrapamos.)
# Lo usan tanto el contador como el fetcher para que la barra coincida.
_CDL_SEARCH_INCOMPLETE_WHERE = """
    barcode IS NOT NULL
    AND barcode <> ''
    AND (inferred_categories IS NULL OR inferred_categories = '')
    AND cdl_fetched_at IS NULL
"""


def _cdl_search_needs_fill_count() -> int:
    """Cuantos libros del mirror estan incompletos (sin categoria) en este shard."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT COUNT(*) FROM odoo_books_mirror
            WHERE {_CDL_SEARCH_INCOMPLETE_WHERE}
              AND {_shard_clause()}
        """)
        return cur.fetchone()[0]
    except Exception as e:
        print(f"[CDLSearch] _cdl_search_needs_fill_count FAILED: {type(e).__name__}: {e}")
        try: conn.rollback()
        except Exception: pass
        return 0
    finally:
        conn.close()


def _cdl_search_fetch_targets(limit: int = 200) -> list[tuple[int, str]]:
    """Lista (odoo_id, isbn) de libros del mirror sin categoria (en este shard).
    Prioriza:
      1. Libros que SI estan en cdl_isbn_index — CDL los vende. Maxima
         probabilidad de match. (Si el sitemap fast-path tambien los esta
         procesando hay duplicacion temporal benigna: el primero que llega
         marca cdl_fetched_at y el otro lo salta en el siguiente chunk.)
      2. Dentro de eso, prefijo 978-84 (Espana) > 978-958/9 (Colombia) > resto
      3. Al final, libros no-en-sitemap (mayoritariamente descatalogados;
         probable 0-5% match pero vale la pena por los pocos que rescata)
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT m.odoo_id, m.barcode
            FROM odoo_books_mirror m
            LEFT JOIN cdl_isbn_index ci ON m.barcode = ci.isbn
            WHERE m.barcode IS NOT NULL
              AND m.barcode <> ''
              AND (m.inferred_categories IS NULL OR m.inferred_categories = '')
              AND m.cdl_fetched_at IS NULL
              AND {_shard_clause('m.odoo_id')}
            ORDER BY
              CASE WHEN ci.isbn IS NOT NULL THEN 0 ELSE 1 END,
              CASE
                WHEN SUBSTR(m.barcode, 1, 5) = '97884' THEN 0
                WHEN SUBSTR(m.barcode, 1, 6) = '978958'
                  OR SUBSTR(m.barcode, 1, 6) = '978959' THEN 1
                ELSE 2
              END,
              m.odoo_id
            LIMIT ?
        """, (limit,))
        return [(r[0], r[1]) for r in cur.fetchall()]
    finally:
        conn.close()


async def fill_from_cdl_search(chunk_size: int = 200) -> dict:
    """
    Scrape CDL para CUALQUIER libro del mirror via busqueda directa por ISBN.
    No requiere cdl_isbn_index. Cubre los libros que el otro job (sitemap)
    no puede tocar. Mas lento por libro pero cobertura total.
    """
    from playwright.async_api import async_playwright
    from scraper import launch_browser_pool, close_browser_pool, scrape_book, PROXY_POOL

    global cdl_search_fill_job
    cdl_search_fill_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "target": _cdl_search_needs_fill_count(),
        "processed": 0,
        "matched": 0,
        "no_match": 0,
        "transient_skipped": 0,
        "errors": [],
        "mode": "search",
    }
    job = cdl_search_fill_job
    print(f"[CDLSearch] Target: {job['target']} libros del mirror sin cdl_fetched_at")

    if job["target"] == 0:
        job["status"] = "completed"
        return job

    n_proxies = max(1, len(PROXY_POOL))
    pool_size = max(n_proxies * 2, 8)

    try:
        async with async_playwright() as p:
            browsers, page_queue = await launch_browser_pool(p, pool_size)
            print(f"[CDLSearch] Browser pool con {pool_size} paginas")

            sem = asyncio.Semaphore(pool_size)
            TRANSIENT_PATTERNS = (
                "ERR_TUNNEL_CONNECTION_FAILED",
                "ERR_PROXY_CONNECTION_FAILED",
                "ERR_CONNECTION_RESET",
                "ERR_CONNECTION_CLOSED",
                "ERR_CONNECTION_REFUSED",
                "ERR_TIMED_OUT",
                "ERR_EMPTY_RESPONSE",
                "Timeout",
            )

            import proxy_health
            async def _process_one(odoo_id, isbn):
                if job["status"] != "running":
                    return
                async with sem:
                    page = await page_queue.get()
                    proxy_spec = getattr(page, '_proxy_spec', None)
                    try:
                        data = await scrape_book(page, query=isbn, direct_url=None)
                        if data and data.get("title") and data.get("title") != "Unknown Title":
                            _cdl_save_one(odoo_id, isbn, data)
                            job["matched"] += 1
                            proxy_health.mark_success(proxy_spec)
                        else:
                            _cdl_save_one(odoo_id, isbn, None)
                            job["no_match"] += 1
                            proxy_health.mark_no_match(proxy_spec)
                    except Exception as e:
                        err_str = str(e)
                        err_short = f"{type(e).__name__}: {err_str[:80]}"
                        job["errors"].append(f"isbn {isbn}: {err_short}")
                        is_transient = any(p in err_str for p in TRANSIENT_PATTERNS)
                        if is_transient:
                            job["transient_skipped"] = job.get("transient_skipped", 0) + 1
                            proxy_health.mark_failed(proxy_spec, err_short)
                        else:
                            _cdl_save_one(odoo_id, isbn, None)
                            job["no_match"] += 1
                    finally:
                        if proxy_health.is_alive(proxy_spec):
                            await page_queue.put(page)
                        job["processed"] += 1

            while job["status"] == "running":
                targets = _cdl_search_fetch_targets(limit=chunk_size)
                if not targets:
                    print("[CDLSearch] Sin mas targets — fin")
                    break

                # Auto-recovery: si todas las pages se fueron con sus proxies
                # muertas, el queue esta vacio. Relanzar el pool — caera a IP
                # directa porque no hay proxies vivas. Sin esto el job se cuelga.
                if page_queue.qsize() == 0:
                    print(f"[CDLSearch] Pool vacio — relanzando "
                          f"(probablemente todas las proxies estan dead)")
                    await close_browser_pool(browsers)
                    browsers, page_queue = await launch_browser_pool(p, pool_size)

                tasks = [_process_one(oid, isbn) for oid, isbn in targets]
                await asyncio.gather(*tasks, return_exceptions=True)

                pct = (job["processed"] / job["target"] * 100) if job["target"] else 0
                print(f"[CDLSearch] {job['processed']}/{job['target']} "
                      f"({pct:.1f}%) match:{job['matched']} no:{job['no_match']}")

            await close_browser_pool(browsers)

        if job["status"] == "running":
            job["status"] = "completed"
        print(f"[CDLSearch] DONE — processed:{job['processed']} matched:{job['matched']}")
    except Exception as e:
        job["status"] = "error"
        err = f"{type(e).__name__}: {e!r}"
        job["errors"].append(err[:200])
        print(f"[CDLSearch] Fatal: {err}")

    return job


# ── CDL HTTP Fill (aiohttp + BeautifulSoup — sin browser, 60-80x mas rapido) ─
# Reemplaza a fill_from_cdl_mirror y fill_from_cdl_search para volumen masivo.
# Mantiene TODOS los campos (peso/alto/ancho/encuadernacion/traductor) porque
# el HTML estatico de CDL ya los trae con el patron data-campo='Label'.
# Validado en local: 2000 libros/min con concurrency 20.
cdl_http_fill_job: dict | None = None


def get_cdl_http_fill_status() -> dict:
    job = dict(cdl_http_fill_job) if cdl_http_fill_job else {"status": "idle"}
    if "errors" in job:
        job["errors"] = job["errors"][-10:]
    return job


def stop_cdl_http_fill():
    global cdl_http_fill_job
    if cdl_http_fill_job and cdl_http_fill_job.get("status") == "running":
        cdl_http_fill_job["status"] = "stopped"
        return True
    return False


def _cdl_http_fetch_targets(limit: int = 500) -> list[tuple[int, str, str]]:
    """Targets para HTTP fill. Devuelve (odoo_id, isbn, url_or_empty).
    Prioriza libros del sitemap (URL conocida -> 1 request) > resto."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, f"""
            SELECT m.odoo_id, m.barcode, COALESCE(ci.url, '')
            FROM odoo_books_mirror m
            LEFT JOIN cdl_isbn_index ci ON m.barcode = ci.isbn
            WHERE m.barcode IS NOT NULL
              AND m.barcode <> ''
              AND (m.inferred_categories IS NULL OR m.inferred_categories = '')
              AND m.cdl_fetched_at IS NULL
              AND {_shard_clause('m.odoo_id')}
            ORDER BY
              CASE WHEN ci.isbn IS NOT NULL THEN 0 ELSE 1 END,
              CASE
                WHEN SUBSTR(m.barcode, 1, 5) = '97884' THEN 0
                WHEN SUBSTR(m.barcode, 1, 6) IN ('978958', '978959') THEN 1
                ELSE 2
              END,
              m.odoo_id
            LIMIT ?
        """, (limit,))
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]
    finally:
        conn.close()


async def fill_from_cdl_http(
    concurrency: int = 20, chunk_size: int = 500,
) -> dict:
    """
    Bulk scrape CDL via aiohttp + BeautifulSoup. Sin browser.
    Speedup ~60-80x vs Playwright manteniendo TODOS los campos.

    Auto-throttle: si CDL devuelve 429/403 (CDLBlocked), espera 60s y baja
    la concurrency a la mitad. Si pasa 3 veces, detiene el job.
    """
    import aiohttp
    import cdl_http_client as cdl_http

    global cdl_http_fill_job
    cdl_http_fill_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "processed": 0,
        "matched": 0,
        "no_match": 0,
        "transient_skipped": 0,
        "blocked_hits": 0,
        "concurrency": concurrency,
        "errors": [],
    }
    job = cdl_http_fill_job

    # Conta inicial — solo para mostrar progreso
    job["target"] = _cdl_search_needs_fill_count()
    print(f"[CDLHttp] Target: {job['target']} libros sin categoria")

    if job["target"] == 0:
        job["status"] = "completed"
        return job

    sem = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency + 10, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=30)

    async def _process_one(session, odoo_id: int, isbn: str, url: str):
        if job["status"] != "running":
            return
        async with sem:
            try:
                data = await cdl_http.fetch_book(
                    session, isbn=isbn,
                    direct_url=url if url else "",
                )
                if data and data.get("title"):
                    _cdl_save_one(odoo_id, isbn, data)
                    job["matched"] += 1
                else:
                    _cdl_save_one(odoo_id, isbn, None)
                    job["no_match"] += 1
            except cdl_http.CDLBlocked as e:
                # Rate limit / WAF — no marcar fetched, contar como reintento
                job["blocked_hits"] = job.get("blocked_hits", 0) + 1
                job["transient_skipped"] = job.get("transient_skipped", 0) + 1
                job["errors"].append(f"isbn {isbn}: BLOCKED {str(e)[:80]}")
                # Si pasa 3 veces, parar
                if job["blocked_hits"] >= 3:
                    job["status"] = "rate_limited"
                    print(f"[CDLHttp] DETENIDO por rate-limit (3+ hits)")
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:80]}"
                job["errors"].append(f"isbn {isbn}: {err}")
                # Errores generales: marcamos como no-match para no reintentar para siempre
                _cdl_save_one(odoo_id, isbn, None)
                job["no_match"] += 1
            finally:
                job["processed"] += 1

    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            while job["status"] == "running":
                targets = _cdl_http_fetch_targets(limit=chunk_size)
                if not targets:
                    print("[CDLHttp] Sin mas targets — fin")
                    break

                tasks = [_process_one(session, oid, isbn, url) for oid, isbn, url in targets]
                await asyncio.gather(*tasks, return_exceptions=True)

                pct = (job["processed"] / job["target"] * 100) if job["target"] else 0
                print(f"[CDLHttp] {job['processed']}/{job['target']} ({pct:.1f}%) "
                      f"match:{job['matched']} no:{job['no_match']} blocked:{job['blocked_hits']}")

        if job["status"] == "running":
            job["status"] = "completed"
        print(f"[CDLHttp] DONE — processed:{job['processed']} matched:{job['matched']}")
    except Exception as e:
        job["status"] = "error"
        err = f"{type(e).__name__}: {e!r}"
        job["errors"].append(err[:200])
        print(f"[CDLHttp] Fatal: {err}")

    return job
