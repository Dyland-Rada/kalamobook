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
import os
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


# ── Capa 0: ajustar el PVP segun lo que nos descuenta el proveedor ──────
# Va ANTES que el suplemento de la Capa 1, sobre el PVP crudo.
#
# AZETA no descuenta lo mismo en todas sus lineas. Medido sobre su feed del
# 07/10/2026, de 1.035.248 libros con precio solo 497.705 llegan al 30%; los
# otros 537.543 se quedan en 27, 25, 20 o menos. En esos, vender al PVP del
# editor deja un margen por debajo del objetivo.
#
# La regla: subir el PVP hasta que la venta deje el margen que se busca.
#
#     coste_base = pvp / (1 + iva) * (1 - descuento)      <- lo que pagamos
#     queremos    (venta_base - coste_base) / venta_base = margen
#     => venta_base = coste_base / (1 - margen)
#     => pvp_nuevo  = pvp * (1 - descuento) / (1 - margen)
#
# El IVA se va en la division, asi que el factor no depende de el. Un libro
# al 20% de descuento necesita x1,1429; al 25%, x1,0714.
#
# Idempotente como el resto del motor: siempre se calcula desde el PVP crudo
# del proveedor, nunca sobre un PVP ya ajustado.
MARGEN_OBJETIVO = float(os.environ.get("KALAMO_MARGEN_OBJETIVO_PCT", "30"))


def pvp_por_margen(pvp, descuento, margen_pct: float | None = None):
    """
    PVP necesario para que la venta deje `margen_pct` de margen, sabiendo
    que el proveedor nos descuenta `descuento` por ciento.

    Devuelve el PVP crudo sin tocar cuando el descuento ya llega al objetivo
    —no se baja el precio nunca— y None cuando faltan datos.
    """
    if pvp is None or descuento is None:
        return None
    m = MARGEN_OBJETIVO if margen_pct is None else float(margen_pct)
    if not (0 <= m < 100):
        raise ValueError(f"margen fuera de rango: {m}")
    p, d = float(pvp), float(descuento)
    if p <= 0:
        return None
    if d >= m:
        return round(p, 2)
    return round(p * (1 - d / 100) / (1 - m / 100), 2)


def margen_real(pvp, descuento) -> float | None:
    """Margen que deja hoy ese libro, en por ciento. Es el propio descuento:
    comprando al (1-d) de la base y vendiendo a la base, el margen sobre
    venta ES d. Se expone aparte para que el calculo quede explicito donde
    se use y no haya que recordarlo."""
    if pvp is None or descuento is None:
        return None
    return round(float(descuento), 2)


# ── Capa 3: descuento de marketplace ────────────────────────────────────
# Se aplica SOBRE el precio ya suplementado por la Capa 1, y despues va el
# centimo. Orden completo: PVP proveedor -> Capa 1 -> Capa 3 -> -0,01.
#
# La Capa 2 (descuento de cesta web) NO va aqui: es solo de Shopify y de
# momento no la tenemos escrita en ningun sitio.
#
# OJO con el tramo de 33 a 34: la tabla del cliente salta de "0-33" a
# "34-40" y deja fuera 33,01-33,99, donde caen 2.844 libros del catalogo.
# Aqui se les da 0% -lo conservador, no bajar el precio- pero es una
# decision nuestra, no suya: hay que confirmarla.
TRAMOS_MARKETPLACE = [
    (33.99, 0.0),
    (40.00, 1.5),
    (50.00, 2.0),
    (80.00, 4.0),
    (float("inf"), 5.0),
]
CENTIMO = 0.01


def descuento_marketplace(precio: float) -> float:
    """Porcentaje de descuento que le toca a ese precio."""
    for tope, pct in TRAMOS_MARKETPLACE:
        if precio <= tope:
            return pct
    return 0.0


def precio_marketplace(precio_web) -> float | None:
    """
    Precio final de marketplace: Capa 3 sobre el precio web, menos el
    centimo. None si no hay precio del que partir.
    """
    if precio_web is None:
        return None
    p = float(precio_web)
    if p <= 0:
        return None
    pct = descuento_marketplace(p)
    return round(round(p * (1 - pct / 100), 2) - CENTIMO, 2)


async def reactivar_variantes(odoo, template_ids: list[int]) -> int:
    """
    Desarchiva las variantes de las plantillas que acabamos de reactivar.

    Odoo archiva las variantes en cascada al archivar la plantilla, pero al
    desarchivarla NO las devuelve: queda plantilla activa con variante
    archivada. Ese producto no se puede vender ni admite stock.quant
    ("product.product no encontrado" en el sync). Medido 2026-07-30:
    22.074 productos en ese estado tras varios ciclos de la regla API-15.
    """
    reactivadas = 0
    for i in range(0, len(template_ids), 500):
        chunk = template_ids[i:i + 500]
        try:
            rows = await odoo.search_read(
                "product.product",
                [["product_tmpl_id", "in", chunk], ["active", "=", False]],
                ["id"])
            ids = [r["id"] for r in rows]
            if ids:
                await odoo.write("product.product", ids, {"active": True})
                reactivadas += len(ids)
        except Exception as e:
            print(f"[Pricing] reactivar_variantes chunk@{i}: {e}")
    return reactivadas


def _load_targets(limit=None, solo_suplemento: bool = False):
    """
    Lee mirror: (odoo_id, pvp_base, list_price).

    solo_suplemento acota a los tramos donde la Capa 1 SUMA algo (2,90 a
    7,50). Existe porque la corrida completa es peligrosa: al reescribir
    list_price = pvp_base para todo, se lleva por delante cualquier precio
    que hoy este por encima del PVP. Medido el 08/09/2026: de 137.730
    productos por encima de 7,50 que cambiarian, 102.244 BAJARIAN de
    precio, y entre ellos uno de 172,00 EUR que se quedaba en 38,46 y otro
    de 115,00 que se quedaba en 14,42. Como list_price es tambien el precio
    de la web, eso se publica en Shopify y en los dos marketplaces.

    Con el filtro puesto no se archiva nada, ademas: web_price solo
    devuelve None por debajo de 2,90 o sin PVP, y ambos quedan fuera.
    """
    conn = db.get_connection(); cur = conn.cursor()
    q = """
        SELECT odoo_id, pvp_base, list_price
        FROM odoo_books_mirror
        WHERE odoo_id IS NOT NULL
    """
    if solo_suplemento:
        q += " AND pvp_base >= 2.90 AND pvp_base < 7.51"
    if limit:
        q += f" LIMIT {int(limit)}"
    cur.execute(q)
    rows = cur.fetchall()
    conn.close()
    return rows


async def run_price_update(dry_run: bool = True, limit: int | None = None,
                           solo_suplemento: bool = False,
                           solo_subidas: bool = False) -> dict:
    global price_job
    price_job = {"status": "running", "dry_run": dry_run,
                 "solo_suplemento": solo_suplemento,
                 "solo_subidas": solo_subidas,
                 "started_at": datetime.now().isoformat(), "stage": "leyendo",
                 "total": 0, "precio_actualizado": 0, "apagados": 0,
                 "sin_cambio": 0, "calls": 0, "bajarian": 0,
                 "omitidos_por_bajar": 0, "errors": []}
    job = price_job
    t0 = time.monotonic()
    try:
        rows = _load_targets(limit, solo_suplemento)
        job["total"] = len(rows)

        price_updates = defaultdict(list)   # web_price -> [odoo_id] (solo los que cambian)
        to_deactivate = []                  # odoo_id a apagar
        for odoo_id, pvp_base, list_price in rows:
            wp = web_price(pvp_base)
            if wp is None:
                to_deactivate.append(odoo_id)
            else:
                cur_lp = round(float(list_price), 2) if list_price is not None else None
                baja = cur_lp is not None and wp < cur_lp
                # solo_subidas protege los registros donde list_price y
                # pvp_base no guardan ninguna relacion, que son los que la
                # regla estropea. Medido el 08/09/2026 en el tramo del
                # suplemento: de 508 que bajarian, 116 lo hacian por mas de
                # 10 EUR, y el pvp_base 5,72 aparecia repetido en libros de
                # editoriales distintas con precios reales de 30, 27 y 25
                # EUR. Ahi el 5,72 es basura, no un PVP, y la regla no tiene
                # de donde partir. Se dejan quietos y se revisan a mano.
                if baja and solo_subidas:
                    job["omitidos_por_bajar"] += 1
                elif cur_lp != wp:
                    price_updates[wp].append(odoo_id)
                    # Una bajada de precio es la senal de alarma de esta
                    # corrida: significa que list_price estaba por encima
                    # del PVP y la regla se lo lleva por delante. En seco
                    # este contador es lo que hay que mirar antes de
                    # aplicar nada.
                    if baja:
                        job["bajarian"] += 1
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
            reactivados: list[int] = []
            for wp, ids in price_updates.items():
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    try:
                        await odoo.write("product.template", chunk,
                                         {"list_price": wp, "active": True})
                        job["precio_actualizado"] += len(chunk)
                        job["calls"] += 1
                        reactivados.extend(chunk)
                    except Exception as e:
                        job["errors"].append(f"precio {wp}: {str(e)[:100]}")
            # Desarchivar variantes de lo reactivado (Odoo no lo hace solo)
            job["stage"] = "reactivando variantes"
            job["variantes_reactivadas"] = await reactivar_variantes(
                odoo, reactivados)
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
