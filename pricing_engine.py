"""
Motor de precios Kalamo (API-15) — Capa 1: suplemento por PVP bajo.

Regla (sobre el PVP crudo del proveedor = pvp_base):
  PVP < 2,90        -> NO publicar (apagar producto)
  2,90 - 4,99       -> +2,00
  5,00 - 6,00       -> +1,50
  6,01 - 7,50       -> +1,00
  > 7,50            -> sin suplemento
  sin precio (0/NULL) -> apagar producto

Idempotente: el precio web SIEMPRE se calcula desde pvp_base (snapshot del
PVP crudo), nunca sobre un precio ya suplementado. Re-ejecutar no duplica.
Solo apaga (active=False), NUNCA borra.

El -0,01 de marketplace (API-16) NO se aplica aqui: es capa de exportacion.
"""
import asyncio
import time
from collections import defaultdict
from datetime import datetime

import db
from odoo_client import OdooClient

UMBRAL_MIN = 2.90

price_job: dict = {"status": "idle"}


def get_status() -> dict:
    return dict(price_job)


def supplement(pvp: float) -> float:
    """Suplemento segun el tramo del PVP crudo."""
    if pvp <= 4.99:
        return 2.00
    if pvp <= 6.00:
        return 1.50
    if pvp <= 7.50:
        return 1.00
    return 0.00


def web_price(pvp) -> float | None:
    """Precio de venta web. None = no publicar (apagar)."""
    if pvp is None or float(pvp) < UMBRAL_MIN:
        return None
    p = float(pvp)
    return round(p + supplement(p), 2)


def _load_targets(limit=None):
    """Lee mirror: (odoo_id, pvp_base, list_price)."""
    conn = db.get_connection(); cur = conn.cursor()
    q = """
        SELECT odoo_id, pvp_base, list_price
        FROM odoo_books_mirror
        WHERE odoo_id IS NOT NULL
    """
    if limit:
        q += f" LIMIT {int(limit)}"
    cur.execute(q)
    rows = cur.fetchall()
    conn.close()
    return rows


async def run_price_update(dry_run: bool = True, limit: int | None = None) -> dict:
    global price_job
    price_job = {"status": "running", "dry_run": dry_run,
                 "started_at": datetime.now().isoformat(), "stage": "leyendo",
                 "total": 0, "precio_actualizado": 0, "apagados": 0,
                 "sin_cambio": 0, "calls": 0, "errors": []}
    job = price_job
    t0 = time.monotonic()
    try:
        rows = _load_targets(limit)
        job["total"] = len(rows)

        price_updates = defaultdict(list)   # web_price -> [odoo_id] (solo los que cambian)
        to_deactivate = []                  # odoo_id a apagar
        for odoo_id, pvp_base, list_price in rows:
            wp = web_price(pvp_base)
            if wp is None:
                to_deactivate.append(odoo_id)
            else:
                cur_lp = round(float(list_price), 2) if list_price is not None else None
                if cur_lp != wp:
                    price_updates[wp].append(odoo_id)
                else:
                    job["sin_cambio"] += 1

        job["a_actualizar"] = sum(len(v) for v in price_updates.values())
        job["a_apagar"] = len(to_deactivate)
        job["valores_precio_distintos"] = len(price_updates)

        if dry_run:
            job["stage"] = "dry_run_done"
            job["status"] = "completed"
            job["elapsed_s"] = round(time.monotonic() - t0, 1)
            return job

        async with OdooClient() as odoo:
            # 1) actualizar precios (una write por valor de precio)
            job["stage"] = "actualizando precios"
            for wp, ids in price_updates.items():
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    try:
                        await odoo.write("product.template", chunk,
                                         {"list_price": wp, "active": True})
                        job["precio_actualizado"] += len(chunk)
                        job["calls"] += 1
                    except Exception as e:
                        job["errors"].append(f"precio {wp}: {str(e)[:100]}")
            # 2) apagar (active=False) en lotes
            job["stage"] = "apagando"
            for i in range(0, len(to_deactivate), 500):
                chunk = to_deactivate[i:i + 500]
                try:
                    await odoo.write("product.template", chunk, {"active": False})
                    job["apagados"] += len(chunk)
                    job["calls"] += 1
                except Exception as e:
                    job["errors"].append(f"apagar @{i}: {str(e)[:100]}")

        job["stage"] = "done"
        job["status"] = "completed"
        job["elapsed_s"] = round(time.monotonic() - t0, 1)
        try:
            import audit_log
            audit_log.log_event("pricing", "mass_update",
                                f"Precios: {job['precio_actualizado']:,} actualizados, "
                                f"{job['apagados']:,} apagados, {job['sin_cambio']:,} sin cambio",
                                detalle={k: job.get(k) for k in
                                         ("total", "precio_actualizado", "apagados",
                                          "sin_cambio", "calls", "elapsed_s")})
        except Exception:
            pass
        return job
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e!r}"[:300])
        job["elapsed_s"] = round(time.monotonic() - t0, 1)
        return job
