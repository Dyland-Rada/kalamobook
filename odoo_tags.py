"""
Clasificación de libros en Odoo con tags de estado (V2).

Tags y reglas:
- Completo:  imagen + descripcion + peso + alto + ancho
- Web:       imagen + descripcion (sin dimensiones completas)
- Foto:      SIN imagen PERO CON descripcion (le falta foto)
- Stock:     SIN descripcion (con o sin imagen) -> solo marketplace
- Bloqueado: MANUAL. Si un libro lo tiene, este job lo skippea.

Estados que se vuelcan:
  Web:       Completo + Web
  Markets:   Completo + Web + Foto + Stock

Fuente de datos: odoo_books_mirror (cdl_image_url, description, cdl_weight,
cdl_height, cdl_width). Escribe product.template.product_tag_ids en Odoo.

Idempotente: re-correr no causa cambios si ya está sincronizado.
"""
import asyncio
import os
import time
from datetime import datetime

import db
from odoo_client import OdooClient


TAG_NAMES = ["Completo", "Web", "Foto", "Stock", "Bloqueado"]
WRITE_CHUNK = int(os.environ.get("TAG_WRITE_CHUNK", "500"))

tag_job: dict | None = None


def get_status() -> dict:
    job = dict(tag_job) if tag_job else {"status": "idle"}
    if "errors" in job:
        job["errors"] = job["errors"][-15:]
    return job


def stop():
    global tag_job
    if tag_job and tag_job.get("status") == "running":
        tag_job["status"] = "stopped"
        return True
    return False


def _classify(img, desc, peso, alto, ancho) -> str:
    """Logica V2. Devuelve: 'Completo'|'Web'|'Foto'|'Stock'."""
    has_img = bool(img and str(img).strip())
    has_desc = bool(desc and str(desc).strip())
    has_dim = (bool(peso and str(peso).strip())
               and bool(alto and str(alto).strip())
               and bool(ancho and str(ancho).strip()))

    if has_img and has_desc and has_dim:
        return "Completo"
    if has_img and has_desc:
        return "Web"
    if not has_img and has_desc:
        return "Foto"
    return "Stock"


async def _resolve_tags(odoo: OdooClient) -> dict[str, int]:
    rows = await odoo.search_read(
        "product.tag", [["name", "in", TAG_NAMES]],
        ["id", "name"],
    )
    return {r["name"]: r["id"] for r in rows}


async def _get_bloqueado_ids(odoo: OdooClient,
                              bloqueado_tag_id: int) -> set[int]:
    """template_ids que ya tienen tag Bloqueado en Odoo."""
    ids = await odoo.execute_kw(
        "product.template", "search",
        [[["product_tag_ids", "in", [bloqueado_tag_id]]]],
    )
    return set(ids or [])


def _read_mirror_classified() -> tuple[dict[str, list[int]], int]:
    """
    Lee todo el mirror y devuelve ({tag_name: [odoo_id, ...]}, total).
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        db.execute_query(cur, """
            SELECT odoo_id, cdl_image_url, description,
                   cdl_weight, cdl_height, cdl_width
            FROM odoo_books_mirror
            WHERE odoo_id IS NOT NULL
        """)
        rows = cur.fetchall()
    finally:
        conn.close()

    groups: dict[str, list[int]] = {n: [] for n in TAG_NAMES if n != "Bloqueado"}
    for r in rows:
        tag = _classify(r[1], r[2], r[3], r[4], r[5])
        groups[tag].append(r[0])
    return groups, len(rows)


async def run_tag_classification(dry_run: bool = False) -> dict:
    """
    Clasifica todos los libros del mirror y aplica las tags en Odoo.

    dry_run=True: solo calcula la particion, NO escribe en Odoo.
    Respeta el tag Bloqueado: si un libro ya lo tiene, no se toca.
    """
    global tag_job
    tag_job = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "stage": "starting",
        "dry_run": dry_run,
        "total_mirror": 0,
        "bloqueados_skipped": 0,
        "groups_before_skip": {},
        "groups_after_skip": {},
        "written": 0,
        "errors": [],
        "elapsed_s": 0,
    }
    job = tag_job
    t_start = time.monotonic()

    try:
        async with OdooClient() as odoo:
            # 1. Resolver tags
            job["stage"] = "resolving_tags"
            name_to_id = await _resolve_tags(odoo)
            missing = [n for n in TAG_NAMES if n not in name_to_id]
            if missing:
                raise RuntimeError(f"Tags faltan en Odoo: {missing}")
            estado_ids = [name_to_id[n] for n in
                          ["Completo", "Web", "Foto", "Stock"]]
            bloq_id = name_to_id["Bloqueado"]
            print(f"[Tags] tag IDs: {name_to_id}")

            # 2. Cargar bloqueados
            job["stage"] = "loading_blocked"
            bloqueados = await _get_bloqueado_ids(odoo, bloq_id)
            print(f"[Tags] {len(bloqueados):,} libros bloqueados (se respetan)")

            # 3. Clasificar mirror
            job["stage"] = "classifying"
            groups, total = _read_mirror_classified()
            job["total_mirror"] = total
            job["groups_before_skip"] = {k: len(v) for k, v in groups.items()}

            # 4. Quitar los bloqueados
            skipped = 0
            for tag_name in list(groups.keys()):
                kept = [t for t in groups[tag_name] if t not in bloqueados]
                skipped += len(groups[tag_name]) - len(kept)
                groups[tag_name] = kept
            job["bloqueados_skipped"] = skipped
            job["groups_after_skip"] = {k: len(v) for k, v in groups.items()}
            print(f"[Tags] Particion final: {job['groups_after_skip']}")

            if dry_run:
                job["status"] = "completed"
                job["stage"] = "done (dry_run)"
                job["elapsed_s"] = round(time.monotonic() - t_start, 2)
                print(f"[Tags] DRY RUN — sin escribir en Odoo")
                return job

            # 5. Por cada grupo, write batch a Odoo
            job["stage"] = "writing"
            for tag_name, tmpl_ids in groups.items():
                if not tmpl_ids:
                    continue
                target_id = name_to_id[tag_name]
                # quitar las otras tags de estado, anadir la target
                ops = [(3, tid, 0) for tid in estado_ids if tid != target_id]
                ops.append((4, target_id, 0))
                for i in range(0, len(tmpl_ids), WRITE_CHUNK):
                    if job["status"] != "running":
                        break
                    chunk = tmpl_ids[i:i + WRITE_CHUNK]
                    try:
                        await odoo.write(
                            "product.template", chunk,
                            {"product_tag_ids": ops},
                        )
                        job["written"] += len(chunk)
                    except Exception as e:
                        err = f"{tag_name} chunk@{i}: {type(e).__name__}: {str(e)[:150]}"
                        job["errors"].append(err)
                        print(f"[Tags] {err}")
                    if job["written"] % 10000 < WRITE_CHUNK:
                        print(f"[Tags] {tag_name}: written={job['written']:,}")

            if job["status"] == "running":
                job["status"] = "completed"
            job["stage"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e!r}")
        print(f"[Tags] Fatal: {e!r}")

    job["elapsed_s"] = round(time.monotonic() - t_start, 2)
    print(f"[Tags] DONE written={job['written']:,} skipped={job.get('bloqueados_skipped',0):,} "
          f"en {job['elapsed_s']}s")
    return job


if __name__ == "__main__":
    import sys
    dry = "--dry" in sys.argv
    asyncio.run(run_tag_classification(dry_run=dry))