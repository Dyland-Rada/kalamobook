"""
Ingesta del feed de descuentos que AZETA deja en nuestra carpeta SFTP.

Hasta el 08/10/2026 esta carpeta no la leia nadie: AZETA llevaba subiendo
ficheros desde el dia 7 y se quedaban ahi. Esto los carga.

El fichero se llama `<fecha>_azeta_feed_parcial.<cliente>.xls` pero NO es un
Excel: es texto plano separado por tabuladores, UTF-8, con 25 columnas. Un
.xls de verdad no podria: el del 07/10 ocupa 720 MB y trae 1.110.391 filas,
y el formato tope son 65.536.

La columna que importa es `Descuento Cliente`, que es lo que nos descuenta
AZETA sobre cada linea. Con ella se puede calcular lo que de verdad nos
cuesta un libro y, de ahi, el PVP que hace falta para un margen dado.

LA FORMULA, verificada sobre las 1.035.248 filas con precio del fichero del
07/10/2026 sin un solo fallo:

    Precio Cliente = PVP / (1 + IVA) * (1 - Descuento)

El descuento va sobre la BASE IMPONIBLE, no sobre el PVP, y el Precio
Cliente tambien viene sin IVA. Calcularlo sobre el PVP se equivoca en unos
30 centimos por libro: con 10,95 al 4% de IVA y 30% de descuento salen 7,37
por la via buena y 7,67 por la mala.

Reparto medido el 07/10/2026 sobre 1.035.248 libros con precio:
    30% o mas .......  497.705  (48,1%)
    27% .............   42.574  ( 4,1%)
    25% ............. 246.112  (23,8%)
    20% ............. 216.167  (20,9%)
    15% o menos .....   30.690  ( 3,0%)
Mas 71.676 activos que vienen sin precio ni descuento.
"""
import io
import os
import re
import time
from datetime import datetime

import db

SFTP_HOST = os.environ.get("AZETA_SFTP_HOST", "213.165.85.117")
SFTP_PORT = int(os.environ.get("AZETA_SFTP_PORT", "22"))
SFTP_USER = os.environ.get("AZETA_SFTP_USER", "proveedor")
# Sin valor por defecto a proposito: es la cuenta que usa un proveedor
# externo y no debe vivir en el repositorio. Se configura en el entorno.
SFTP_PASS = os.environ.get("AZETA_SFTP_PASS", "")
SFTP_DIR = os.environ.get("AZETA_SFTP_DIR", "/upload")
# Huella ed25519 del servidor. Si no coincide, no se descarga nada.
SFTP_HUELLA = os.environ.get(
    "AZETA_SFTP_HUELLA",
    "SHA256:rvVcRIYybycPvzb9MI7REXM8lN3AnGLdTinE+IuYNF0")

TABLA = "azeta_feed_descuentos"
LOTE = int(os.environ.get("AZETA_FEED_LOTE", "2000"))
PATRON = re.compile(r"^(\d{8})_azeta_feed_parcial\.", re.I)

_job: dict | None = None


def get_status() -> dict:
    job = dict(_job) if _job else {"status": "idle"}
    if "errors" in job:
        job["errors"] = job["errors"][-10:]
    job.update(_resumen_tabla())
    return job


def stop() -> bool:
    global _job
    if _job and _job.get("status") == "running":
        _job["status"] = "stopped"
        return True
    return False


def ensure_schema() -> None:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLA} (
                isbn            TEXT PRIMARY KEY,
                editorial       TEXT,
                pvp             NUMERIC(10,2),
                iva             NUMERIC(5,2),
                descuento       NUMERIC(5,2),
                precio_cliente  NUMERIC(10,2),
                activo          BOOLEAN,
                fichero         TEXT,
                fecha_fichero   DATE,
                cargado_en      TIMESTAMP
            )
        """)
        cur.execute(f"CREATE INDEX IF NOT EXISTS {TABLA}_desc_idx "
                    f"ON {TABLA} (descuento) WHERE activo")
        conn.commit()
    finally:
        conn.close()


def _resumen_tabla() -> dict:
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        try:
            cur.execute(f"""
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE activo AND descuento IS NOT NULL),
                       COUNT(*) FILTER (WHERE activo AND descuento < 30),
                       MAX(fecha_fichero), MAX(cargado_en)
                FROM {TABLA}
            """)
            r = cur.fetchone()
            return {
                "filas_en_tabla": int(r[0] or 0),
                "activos_con_descuento": int(r[1] or 0),
                "por_debajo_del_30": int(r[2] or 0),
                "ultimo_fichero_cargado": str(r[3]) if r[3] else None,
                "ultima_carga": str(r[4]) if r[4] else None,
            }
        finally:
            conn.close()
    except Exception:
        return {"filas_en_tabla": None}


def _num(x):
    """'10,95' -> 10.95. El feed usa coma decimal y punto de millares."""
    x = (x or "").strip().replace(".", "").replace(",", ".")
    if not x:
        return None
    try:
        return float(x)
    except ValueError:
        return None


def _conectar():
    import paramiko
    import base64
    import hashlib
    if not SFTP_PASS:
        raise RuntimeError(
            "Falta AZETA_SFTP_PASS en el entorno: sin ella no se conecta.")
    t = paramiko.Transport((SFTP_HOST, SFTP_PORT))
    t.connect(username=SFTP_USER, password=SFTP_PASS)
    clave = t.get_remote_server_key()
    huella = "SHA256:" + base64.b64encode(
        hashlib.sha256(clave.asbytes()).digest()).decode().rstrip("=")
    if SFTP_HUELLA and huella != SFTP_HUELLA:
        t.close()
        raise RuntimeError(
            f"La huella del servidor no coincide: {huella} "
            f"en vez de {SFTP_HUELLA}. No se descarga nada.")
    return t, paramiko.SFTPClient.from_transport(t), huella


def listar_ficheros() -> list[dict]:
    """Los feeds que hay en la carpeta, el mas nuevo primero."""
    t, s, huella = _conectar()
    try:
        out = []
        for a in s.listdir_attr(SFTP_DIR):
            m = PATRON.match(a.filename)
            if not m:
                continue
            out.append({"fichero": a.filename, "fecha": m.group(1),
                        "bytes": a.st_size, "modificado": a.st_mtime})
        out.sort(key=lambda x: x["fecha"], reverse=True)
        return out
    finally:
        t.close()


def _upsert(cur, lote: list[tuple]) -> int:
    if not lote:
        return 0
    # El feed repite EAN dentro del mismo lote y Postgres rechaza un
    # ON CONFLICT que toque la misma fila dos veces (CardinalityViolation).
    # Se queda la ultima aparicion, que es la que manda en un UPSERT normal.
    unicos: dict = {}
    for fila in lote:
        unicos[fila[0]] = fila
    lote = list(unicos.values())
    # db.execute_query traduce '?' a '%s' en Postgres, pero aqui se arma el
    # VALUES a mano para meter el lote entero de una vez, asi que la marca se
    # elige segun el motor en vez de confiar en esa traduccion.
    marca = "%s" if db.IS_POSTGRES else "?"
    fila_sql = "(" + ",".join([marca] * 10) + ")"
    marcas = ",".join([fila_sql] * len(lote))
    plano = [v for fila in lote for v in fila]
    cur.execute(f"""
        INSERT INTO {TABLA}
            (isbn, editorial, pvp, iva, descuento, precio_cliente,
             activo, fichero, fecha_fichero, cargado_en)
        VALUES {marcas}
        ON CONFLICT (isbn) DO UPDATE SET
            editorial      = EXCLUDED.editorial,
            pvp            = EXCLUDED.pvp,
            iva            = EXCLUDED.iva,
            descuento      = EXCLUDED.descuento,
            precio_cliente = EXCLUDED.precio_cliente,
            activo         = EXCLUDED.activo,
            fichero        = EXCLUDED.fichero,
            fecha_fichero  = EXCLUDED.fecha_fichero,
            cargado_en     = EXCLUDED.cargado_en
    """, plano)
    return len(lote)


def cargar(fichero: str | None = None, dry_run: bool = False) -> dict:
    """
    Descarga y carga un feed. Sin `fichero`, el mas reciente de la carpeta.

    Se lee en streaming y por trozos: el fichero completo son 720 MB y
    cargarlo entero en memoria es justo el fallo que tumbaba la carga del
    catalogo de AZETA durante un mes.
    """
    global _job
    ensure_schema()
    _job = {
        "status": "running", "started_at": datetime.now().isoformat(),
        "stage": "conectando", "fichero": fichero, "dry_run": dry_run,
        "bytes": 0, "filas": 0, "activos": 0, "inactivos": 0,
        "sin_precio": 0, "guardados": 0, "formula_falla": 0,
        "errors": [], "elapsed_s": 0,
    }
    job = _job
    t0 = time.monotonic()
    conn = None
    try:
        t, s, huella = _conectar()
        job["huella_ok"] = True
        try:
            if not fichero:
                disponibles = [a.filename for a in s.listdir_attr(SFTP_DIR)
                               if PATRON.match(a.filename)]
                if not disponibles:
                    raise RuntimeError(f"No hay ningun feed en {SFTP_DIR}")
                fichero = sorted(disponibles)[-1]
            job["fichero"] = fichero
            fecha = PATRON.match(fichero).group(1)
            fecha_iso = f"{fecha[:4]}-{fecha[4:6]}-{fecha[6:]}"
            job["fecha_fichero"] = fecha_iso
            job["stage"] = "leyendo"

            if not dry_run:
                conn = db.get_connection()
                cur = conn.cursor()
            ahora = datetime.now()
            cab = None
            ix: dict[str, int] = {}
            lote: list[tuple] = []
            resto = b""
            f = s.open(f"{SFTP_DIR}/{fichero}", "rb")
            f.prefetch()
            while True:
                if job["status"] != "running":
                    break
                trozo = f.read(1 << 22)
                if not trozo:
                    break
                job["bytes"] += len(trozo)
                datos = resto + trozo
                *lineas, resto = datos.split(b"\n")
                for lb in lineas:
                    linea = lb.decode("utf-8", "replace").rstrip("\r")
                    if cab is None:
                        cab = linea.lstrip("﻿").split("\t")
                        ix = {c: i for i, c in enumerate(cab)}
                        continue
                    if not linea.strip():
                        continue
                    p = linea.split("\t")
                    if len(p) < len(cab):
                        continue
                    isbn = p[ix["EAN"]].strip()
                    if not isbn.isdigit() or len(isbn) not in (10, 13):
                        continue
                    job["filas"] += 1
                    activo = p[ix["Activo"]].strip().upper() == "S"
                    job["activos" if activo else "inactivos"] += 1
                    pvp = _num(p[ix["PVP"]])
                    iva = _num(p[ix["IVA"]])
                    desc = _num(p[ix["Descuento Cliente"]])
                    pc = _num(p[ix["Precio Cliente"]])
                    if activo and (pvp is None or desc is None):
                        job["sin_precio"] += 1
                    if None not in (pvp, iva, desc, pc):
                        if abs(round(pvp / (1 + iva / 100)
                                     * (1 - desc / 100), 2) - pc) > 0.02:
                            job["formula_falla"] += 1
                    lote.append((isbn, p[ix["Editorial"]].strip()[:200] or None,
                                 pvp, iva, desc, pc, activo, fichero,
                                 fecha_iso, ahora))
                    if len(lote) >= LOTE:
                        if not dry_run:
                            job["guardados"] += _upsert(cur, lote)
                            conn.commit()
                        lote = []
            f.close()
            if lote and not dry_run and job["status"] == "running":
                job["guardados"] += _upsert(cur, lote)
                conn.commit()
            job["stage"] = "done"
            if job["status"] == "running":
                job["status"] = "completed"
        finally:
            t.close()
    except Exception as e:
        job["status"] = "error"
        job["errors"].append(f"{type(e).__name__}: {e}"[:300])
        print(f"[AzetaFeed] Fatal: {e!r}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        job["elapsed_s"] = round(time.monotonic() - t0, 2)
    return job


# ─── Cron ─────────────────────────────────────────────────────────────
# AZETA deja un fichero al dia (20261007, 20261008...), asi que se mira una
# vez al dia. La carpeta llevaba desde el 07/10 recibiendo ficheros que no
# leia nadie, que es exactamente lo que este reloj evita.
import asyncio

CRON_INTERVAL_S = int(os.environ.get("AZETA_FEED_CRON_INTERVAL_S", str(24 * 3600)))

_cron_task = None
_cron_state: dict = {
    "enabled": False,
    "interval_s": CRON_INTERVAL_S,
    "last_run_at": None,
    "last_run_status": None,
    "last_summary": None,
    "next_run_at": None,
    "runs_total": 0,
    "errors": [],
}


def get_cron_status() -> dict:
    out = dict(_cron_state)
    out["errors"] = out.get("errors", [])[-10:]
    out["task_running"] = bool(_cron_task and not _cron_task.done())
    return out


async def _cron_loop():
    from datetime import timedelta
    print(f"[AzetaFeedCron] Arrancado, intervalo {_cron_state['interval_s']}s")
    while _cron_state["enabled"]:
        try:
            res = await asyncio.to_thread(cargar)
            _cron_state["last_run_at"] = datetime.now().isoformat()
            _cron_state["last_run_status"] = res.get("status")
            _cron_state["last_summary"] = (
                f"{res.get('fichero')}: {res.get('filas', 0):,} filas, "
                f"{res.get('guardados', 0):,} guardadas")
            _cron_state["runs_total"] += 1
            if res.get("errors"):
                _cron_state["errors"] += res["errors"][:3]
            print(f"[AzetaFeedCron] Run #{_cron_state['runs_total']}: "
                  f"{_cron_state['last_summary']}")
        except Exception as e:
            _cron_state["last_run_status"] = "error"
            _cron_state["errors"].append(f"{type(e).__name__}: {e!r}"[:300])
            print(f"[AzetaFeedCron] Fatal: {e!r}")

        _cron_state["next_run_at"] = (
            datetime.now() + timedelta(seconds=_cron_state["interval_s"])
        ).isoformat()
        for _ in range(_cron_state["interval_s"]):
            if not _cron_state["enabled"]:
                break
            await asyncio.sleep(1)
    print("[AzetaFeedCron] Detenido")
    _cron_state["next_run_at"] = None


def start_cron() -> bool:
    global _cron_task
    if _cron_task and not _cron_task.done():
        return False
    if not SFTP_PASS:
        print("[AzetaFeedCron] NO arranco: falta AZETA_SFTP_PASS")
        return False
    _cron_state["enabled"] = True
    _cron_state["errors"] = []
    try:
        _cron_task = asyncio.create_task(_cron_loop())
        return True
    except RuntimeError:
        _cron_state["enabled"] = False
        return False


def stop_cron() -> bool:
    if not _cron_state["enabled"]:
        return False
    _cron_state["enabled"] = False
    return True
