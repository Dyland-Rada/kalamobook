"""
Ingesta del fichero de disponibilidad de UDL.

POR QUE HACE FALTA UN MODULO APARTE. UDL es el unico de los dieciseis
proveedores que NO manda CEGALD. Mandan ESTADO -respuestas sueltas sobre
titulos concretos, de 1 a 40 al dia- y ENVIO -albaranes-, pero nunca el
documento de stock. Por eso llevaban meses con almacen dado de alta en
Odoo y cero libros: no habia nada que meter.

Lo que si mandan, cada ~15 dias, es un fichero de disponibilidad: texto
separado por tabuladores, en CP850, con cabecera y 12 columnas.

    EAN  ISBN  TITULO  AUTOR  EDITORIAL  COLECCION  IDIOMA
    PAGINAS  PRECIO  PVP  ENCUADERNACION  SINOPSIS

NO TRAE CANTIDADES. La regla de negocio -confirmada por UDL el 03/11/2026-
es que estar en el fichero significa estar disponible, asi que entra con
stock 1, el mismo semaforo que usan doce de los quince proveedores.

LO QUE SI TRAE, Y ES RARO EN ESTA CASA: el PVP y la base imponible en
columnas SEPARADAS. Medido sobre el fichero del 21/10/2026, la razon
PRECIO/PVP sale 1/1,04 clavado en 42.406 de 42.843 filas, y 1/1,21 en
otras 426 -lo que no son libros-. Eso convierte a UDL en la segunda
fuente, despues de AZETA, que no obliga a deducir si el precio lleva IVA:
lo dice. De ahi sale tambien el tipo de IVA de cada linea.

EL APAGADO VA A 15 DIAS, no a las 48 horas del resto, porque su cadencia
es quincenal: con el umbral normal sus libros se apagarian solos a los dos
dias de cada fichero. Ver CEGALD_FRESCURA_POR_PROVEEDOR en
sync_stock_sinli.
"""
import os
import re
import time
from datetime import datetime

import db

PROVEEDOR_EMAIL = "info@udllibros.com"
PROVEEDOR_ID = 11

# El fichero viene en CP850. Leerlo como Latin-1 -que es lo que hace quien
# llena sinli_precios- convierte la N con virgulilla en un simbolo de yen:
# 1.569 titulos de 19.171 llegan rotos por eso. Aqui se lee bien.
ENCODING = "cp850"

CABECERA_ESPERADA = ["EAN", "ISBN", "TITULO", "AUTOR", "EDITORIAL",
                     "COLECCION", "IDIOMA", "PAGINAS", "PRECIO", "PVP"]

_job = None


def get_status():
    return dict(_job) if _job else {"status": "idle"}


def _num(s):
    s = (s or "").strip().replace(".", "").replace(",", ".")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def _iva_de(precio, pvp):
    """
    El tipo de IVA sale de la razon entre las dos columnas, que es el dato
    que ningun otro proveedor nos da. Se redondea al tipo legal mas cercano
    en vez de devolver el calculo crudo: un 4,0001% no existe.
    """
    if not (precio and pvp) or precio <= 0:
        return None
    pct = (pvp / precio - 1) * 100
    for legal in (4.0, 10.0, 21.0):
        if abs(pct - legal) <= 0.6:
            return legal
    return None


def parsear(ruta):
    """Lee el fichero y devuelve las filas utilizables, sin tocar la BD."""
    with open(ruta, "rb") as f:
        crudo = f.read()
    texto = crudo.decode(ENCODING, errors="replace")
    lineas = [l.rstrip("\r") for l in texto.split("\n") if l.strip()]
    if not lineas:
        raise ValueError("Fichero vacio")

    cab = [c.strip().upper() for c in lineas[0].split("\t")]
    faltan = [c for c in CABECERA_ESPERADA if c not in cab]
    if faltan:
        raise ValueError(
            "La cabecera no es la esperada; faltan " + str(faltan) +
            ". Recibida: " + str(cab[:12]) + ". Si UDL ha cambiado el "
            "formato hay que revisar este parser antes de escribir nada.")
    idx = {c: cab.index(c) for c in CABECERA_ESPERADA}

    filas = []
    descartes = {"sin_isbn": 0, "sin_precio": 0, "duplicado": 0}
    vistos = set()
    for linea in lineas[1:]:
        campos = linea.split("\t")
        if len(campos) <= idx["PVP"]:
            descartes["sin_isbn"] += 1
            continue
        ean = campos[idx["EAN"]].strip()
        # Solo ISBN de libro. El fichero trae tambien expositores, bolsas y
        # material de promocion con EAN que no empiezan por 978/979.
        if not re.fullmatch(r"97[89]\d{10}", ean):
            descartes["sin_isbn"] += 1
            continue
        if ean in vistos:
            descartes["duplicado"] += 1
            continue
        pvp = _num(campos[idx["PVP"]])
        base = _num(campos[idx["PRECIO"]])
        if not pvp:
            descartes["sin_precio"] += 1
            continue
        vistos.add(ean)
        filas.append({
            "isbn": ean,
            "pvp": round(pvp, 2),
            "base": round(base, 2) if base else None,
            "iva": _iva_de(base, pvp),
            "titulo": campos[idx["TITULO"]].strip()[:250],
        })
    return {"filas": filas, "descartes": descartes, "lineas": len(lineas) - 1}


def importar(ruta, dry_run=True, archivo_nombre=None):
    """
    Mete el fichero en libros_proveedor con stock 1 y registra sus ISBN.

    Los que UDL tenia y no vienen en este fichero pasan a 0: el fichero es
    la foto completa de lo que tienen, no un incremental.
    """
    global _job
    t0 = time.monotonic()
    nombre = archivo_nombre or os.path.basename(ruta)
    _job = {"status": "running", "dry_run": dry_run, "archivo": nombre,
            "fase": "leyendo", "started_at": datetime.now().isoformat(),
            "errors": []}
    job = _job
    try:
        p = parsear(ruta)
        filas = p["filas"]
        job["lineas_fichero"] = p["lineas"]
        job["descartes"] = p["descartes"]
        job["con_isbn_y_precio"] = len(filas)
        job["reparto_iva"] = {}
        for f in filas:
            k = ("%.0f%%" % f["iva"]) if f["iva"] else "sin deducir"
            job["reparto_iva"][k] = job["reparto_iva"].get(k, 0) + 1

        if not filas:
            raise ValueError("Ni una fila utilizable: no se escribe nada.")

        conn = db.get_connection()
        cur = conn.cursor()
        try:
            db.execute_query(cur, "SELECT COUNT(*) FROM libros_proveedor "
                                  "WHERE proveedor_email = ? "
                                  "AND stock_disponible > 0",
                             (PROVEEDOR_EMAIL,))
            tenia = int(cur.fetchone()[0])
            job["tenia_con_stock"] = tenia
            # Salvaguarda de tamano: si el fichero trae mucho menos de lo
            # que ya habia, lo normal es que llegara truncado, y aplicarlo
            # apagaria el catalogo entero. Mismo criterio que el CEGALD.
            if tenia and len(filas) < tenia * 0.5:
                job["status"] = "abortado"
                job["motivo"] = (
                    "El fichero trae %s libros y UDL tenia %s con stock. "
                    "Menos de la mitad: parece truncado. No se ha escrito "
                    "nada." % (format(len(filas), ","), format(tenia, ",")))
                return job

            if dry_run:
                job["status"] = "completed"
                job["nota"] = "en seco: no se ha escrito nada"
                return job

            from psycopg2.extras import execute_values
            job["fase"] = "escribiendo stock"
            execute_values(cur, """
                INSERT INTO libros_proveedor
                    (isbn, proveedor_email, proveedor_id, stock_disponible,
                     precio_con_iva, precio_sin_iva, pct_iva,
                     stock_actualizado_en, actualizado_en)
                VALUES %s
                ON CONFLICT (isbn, proveedor_email) DO UPDATE SET
                    stock_disponible = EXCLUDED.stock_disponible,
                    precio_con_iva = EXCLUDED.precio_con_iva,
                    precio_sin_iva = EXCLUDED.precio_sin_iva,
                    pct_iva = COALESCE(EXCLUDED.pct_iva,
                                       libros_proveedor.pct_iva),
                    stock_actualizado_en = NOW(),
                    actualizado_en = CASE
                        WHEN libros_proveedor.stock_disponible
                             IS DISTINCT FROM EXCLUDED.stock_disponible
                          OR libros_proveedor.precio_con_iva
                             IS DISTINCT FROM EXCLUDED.precio_con_iva
                        THEN NOW() ELSE libros_proveedor.actualizado_en END
            """, [(f["isbn"], PROVEEDOR_EMAIL, PROVEEDOR_ID, 1,
                   f["pvp"], f["base"], f["iva"]) for f in filas],
                template="(%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())",
                page_size=5000)
            job["escritos"] = len(filas)

            job["fase"] = "apagando los que ya no estan"
            db.execute_query(cur, """
                UPDATE libros_proveedor
                SET stock_disponible = 0, stock_actualizado_en = NOW(),
                    actualizado_en = NOW()
                WHERE proveedor_email = ? AND stock_disponible > 0
                  AND isbn <> ALL(?)
            """, (PROVEEDOR_EMAIL, [f["isbn"] for f in filas]))
            job["apagados"] = cur.rowcount or 0

            job["fase"] = "registrando ISBN"
            execute_values(cur, """
                INSERT INTO cegald_isbns_v2
                    (proveedor_id, proveedor_email, archivo_nombre, isbn,
                     registrado_en)
                VALUES %s
            """, [(PROVEEDOR_ID, PROVEEDOR_EMAIL, nombre, f["isbn"])
                  for f in filas],
                template="(%s,%s,%s,%s,NOW())", page_size=5000)
            conn.commit()
            job["status"] = "completed"
        finally:
            conn.close()
        return job
    except Exception as e:
        job["status"] = "error"
        job["errors"].append((type(e).__name__ + ": " + str(e))[:400])
        return job
    finally:
        job["elapsed_s"] = round(time.monotonic() - t0, 1)
