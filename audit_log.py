"""
Registro de eventos para auditoría (tab Auditoría del dashboard).

Tabla event_log en Postgres: cada job registra qué recibió, qué actualizó
y qué falló. Persiste entre rebuilds (a diferencia de los jobs en memoria).

Categorías usadas:
- azeta_stock_fetch:  descarga CSV stock AZETA -> libros_proveedor
- azeta_stock_push:   push stock.quant a Odoo AZE01
- azeta_catalog:      catálogo full AZETA -> mirror
- sinli_sync:         sync SINLI -> Odoo (stock + precio)
- tags:               clasificación de tags en Odoo
- cdl_scrape:         scraping CDL
- cron:               ciclos automáticos
- odoo_create:        creación de productos nuevos en Odoo
"""
import json
from datetime import datetime

import db


def ensure_table():
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS event_log (
                id BIGSERIAL PRIMARY KEY,
                ts TIMESTAMPTZ DEFAULT NOW(),
                categoria TEXT NOT NULL,
                evento TEXT NOT NULL,
                resumen TEXT,
                detalle JSONB,
                nivel TEXT DEFAULT 'info'
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_event_log_ts ON event_log(ts DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_event_log_cat ON event_log(categoria)
        """)
        # Registro de peticiones API entrantes (acciones POST): quien llamo
        # que endpoint, con que payload y que respondio el servidor.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS api_request_log (
                id BIGSERIAL PRIMARY KEY,
                ts TIMESTAMPTZ DEFAULT NOW(),
                method TEXT,
                path TEXT,
                query TEXT,
                body TEXT,
                status_code INTEGER,
                duration_ms INTEGER,
                client_ip TEXT,
                username TEXT,
                user_agent TEXT
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_api_req_ts ON api_request_log(ts DESC)
        """)
        conn.commit()
    except Exception as e:
        print(f"[Audit] ensure_table FAIL: {e}")
        try: conn.rollback()
        except Exception: pass
    finally:
        conn.close()


def log_api_request(method: str, path: str, query: str, body: str,
                     status_code: int, duration_ms: int,
                     client_ip: str, username: str, user_agent: str):
    """Registra una peticion API entrante. Nunca lanza."""
    conn = None
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        db.execute_query(cur, """
            INSERT INTO api_request_log
                (method, path, query, body, status_code, duration_ms,
                 client_ip, username, user_agent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (method, path[:300], (query or "")[:1000], (body or "")[:2000],
              status_code, duration_ms, (client_ip or "")[:60],
              (username or "")[:60], (user_agent or "")[:200]))
        conn.commit()
    except Exception as e:
        print(f"[Audit] log_api_request FAIL: {e}")
        try:
            if conn: conn.rollback()
        except Exception:
            pass
    finally:
        try:
            if conn: conn.close()
        except Exception:
            pass


def get_api_requests(limit: int = 100, path_like: str | None = None) -> list[dict]:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        if path_like:
            db.execute_query(cur, """
                SELECT ts, method, path, query, body, status_code,
                       duration_ms, client_ip, username, user_agent
                FROM api_request_log
                WHERE path LIKE ?
                ORDER BY ts DESC LIMIT ?
            """, (f"%{path_like}%", limit))
        else:
            db.execute_query(cur, """
                SELECT ts, method, path, query, body, status_code,
                       duration_ms, client_ip, username, user_agent
                FROM api_request_log
                ORDER BY ts DESC LIMIT ?
            """, (limit,))
        return [{
            "ts": str(r[0]), "method": r[1], "path": r[2], "query": r[3],
            "body": r[4], "status_code": r[5], "duration_ms": r[6],
            "client_ip": r[7], "username": r[8], "user_agent": r[9],
        } for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def log_event(categoria: str, evento: str, resumen: str = "",
              detalle: dict | None = None, nivel: str = "info"):
    """
    Registra un evento. Nunca lanza excepción (la auditoría no debe
    romper el job que la llama).
    """
    conn = None
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        db.execute_query(cur, """
            INSERT INTO event_log (categoria, evento, resumen, detalle, nivel)
            VALUES (?, ?, ?, ?, ?)
        """, (categoria, evento, resumen[:500],
              json.dumps(detalle or {}, default=str)[:5000], nivel))
        conn.commit()
    except Exception as e:
        print(f"[Audit] log_event FAIL ({categoria}/{evento}): {e}")
        try:
            if conn: conn.rollback()
        except Exception:
            pass
    finally:
        try:
            if conn: conn.close()
        except Exception:
            pass


def get_events(categoria: str | None = None, nivel: str | None = None,
               limit: int = 100, offset: int = 0) -> list[dict]:
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        where = []
        params: list = []
        if categoria:
            where.append("categoria = ?")
            params.append(categoria)
        if nivel:
            where.append("nivel = ?")
            params.append(nivel)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        params.extend([limit, offset])
        db.execute_query(cur, f"""
            SELECT id, ts, categoria, evento, resumen, detalle, nivel
            FROM event_log
            {where_sql}
            ORDER BY ts DESC
            LIMIT ? OFFSET ?
        """, tuple(params))
        out = []
        for r in cur.fetchall():
            detalle = r[5]
            if isinstance(detalle, str):
                try: detalle = json.loads(detalle)
                except Exception: pass
            out.append({
                "id": r[0], "ts": str(r[1]), "categoria": r[2],
                "evento": r[3], "resumen": r[4], "detalle": detalle,
                "nivel": r[6],
            })
        return out
    finally:
        conn.close()


# Mapeo nombre-proveedor de sinli_auditoria -> proveedor_email de
# libros_proveedor (el n8n usa razon social, nosotros el buzon).
_AUDITORIA_TO_EMAIL = {
    "ICARO DISTRIBUIDORA, S.L.": "sinli.icaro@zonalibros.com",
    "DISTRIFORMA, S.A.": "fandite@distriforma.es",
    "LES PUNXES DISTRIBUIDORA S.L.": "sinli@punxes.es",
    "DISTRIBUCIONES ALFAOMEGA, S.L.": "sinli@alfaomega.es",
    "Ediciones Akal S.A.": "sinli@akal.com",
    "DISBOOK, S.L.": "sinli.disbookbcn@zonalibros.com",
    "DISTRIFER LIBROS, S.L.": "sinli.distrifer@zonalibros.com",
}


def get_cegald_overview() -> list[dict]:
    """
    Auditoría CEGALD por proveedor.

    Fuente primaria: sinli_auditoria (cada ARCHIVO CEGALD recibido por el
    n8n del Server A, con su nº de registros). Es la verdad de "¿llegó el
    CEGALD completo?" — los timestamps de libros_proveedor NO sirven para
    esto porque el upsert del n8n solo toca stock_actualizado_en cuando el
    stock cambia (un CEGALD de 45k libros sin cambios toca ~15 filas).

    Se excluye KALAMO BOOKS (buzón propio, CEGALDs de 0 registros).
    """
    conn = db.get_connection()
    cur = conn.cursor()
    out: list[dict] = []
    try:
        # 1. Ultimo archivo CEGALD por proveedor (fuente cruda)
        auditoria: dict[str, dict] = {}
        try:
            db.execute_query(cur, """
                SELECT DISTINCT ON (proveedor)
                       proveedor, procesado_en, registros
                FROM sinli_auditoria
                WHERE email_asunto ILIKE ?
                  AND proveedor IS NOT NULL
                  AND proveedor != 'KALAMO BOOKS'
                ORDER BY proveedor, procesado_en DESC
            """, ('%CEGALD%',))
            for r in cur.fetchall():
                email = _AUDITORIA_TO_EMAIL.get(r[0])
                auditoria[email or r[0]] = {
                    "nombre": r[0],
                    "ultimo_cegald": str(r[1]),
                    "registros": r[2],
                }
        except Exception as e:
            print(f"[Audit] sinli_auditoria no accesible: {e}")
            try: conn.rollback()
            except Exception: pass

        # 2. Estado en libros_proveedor por proveedor
        db.execute_query(cur, """
            SELECT proveedor_email,
                   COUNT(*) FILTER (WHERE stock_disponible > 0),
                   MAX(stock_actualizado_en)
            FROM libros_proveedor
            GROUP BY proveedor_email
            ORDER BY 2 DESC
        """)
        for r in cur.fetchall():
            email = r[0]
            a = auditoria.get(email, {})
            registros = a.get("registros")
            total_con_stock = r[1]
            # Fantasmas estimados: si el ultimo CEGALD trae N registros y
            # en BD hay M con stock, sobran ~(M - N) que el proveedor ya
            # no reporta. Solo estimable cuando hay dato de auditoria.
            fantasmas_est = None
            if registros is not None and total_con_stock is not None:
                fantasmas_est = max(0, total_con_stock - registros)
            out.append({
                "proveedor": email,
                "nombre": a.get("nombre"),
                "total_con_stock": total_con_stock,
                "ultimo_evento": str(r[2]) if r[2] else None,
                "ultimo_cegald_completo": a.get("ultimo_cegald"),
                "libros_cegald": registros,
                "fantasmas": fantasmas_est,
            })
        return out
    finally:
        conn.close()


def get_summary(days: int = 7) -> dict:
    """
    Resumen de auditoría:
    - eventos por categoría/día (event_log)
    - libros RECIBIDOS por día y proveedor (libros_proveedor.stock_actualizado_en)
    """
    conn = db.get_connection()
    cur = conn.cursor()
    out: dict = {}
    try:
        # Eventos por dia + categoria
        db.execute_query(cur, f"""
            SELECT DATE(ts) AS dia, categoria, COUNT(*),
                   COUNT(*) FILTER (WHERE nivel = 'error')
            FROM event_log
            WHERE ts > NOW() - INTERVAL '{int(days)} days'
            GROUP BY DATE(ts), categoria
            ORDER BY dia DESC, categoria
        """)
        out["eventos_por_dia"] = [{
            "dia": str(r[0]), "categoria": r[1],
            "eventos": r[2], "errores": r[3],
        } for r in cur.fetchall()]

        # Libros recibidos por dia y proveedor (lo que llega a la BD)
        db.execute_query(cur, f"""
            SELECT DATE(stock_actualizado_en) AS dia,
                   SPLIT_PART(proveedor_email, '@', 1) AS proveedor,
                   COUNT(*)
            FROM libros_proveedor
            WHERE stock_actualizado_en > NOW() - INTERVAL '{int(days)} days'
            GROUP BY DATE(stock_actualizado_en), SPLIT_PART(proveedor_email, '@', 1)
            ORDER BY dia DESC, COUNT(*) DESC
        """)
        out["recibidos_por_dia"] = [{
            "dia": str(r[0]), "proveedor": r[1], "libros": r[2],
        } for r in cur.fetchall()]

        # Stats de hoy
        db.execute_query(cur, """
            SELECT
                (SELECT COUNT(*) FROM libros_proveedor
                 WHERE stock_actualizado_en::date = CURRENT_DATE) AS recibidos_hoy,
                (SELECT COUNT(*) FROM event_log
                 WHERE ts::date = CURRENT_DATE) AS eventos_hoy,
                (SELECT COUNT(*) FROM event_log
                 WHERE ts::date = CURRENT_DATE AND nivel = 'error') AS errores_hoy
        """)
        r = cur.fetchone()
        out["hoy"] = {
            "recibidos": r[0], "eventos": r[1], "errores": r[2],
        }

        # Ultimo evento por categoria (para ver que cron esta vivo)
        db.execute_query(cur, """
            SELECT DISTINCT ON (categoria) categoria, ts, evento, resumen
            FROM event_log
            ORDER BY categoria, ts DESC
        """)
        out["ultimo_por_categoria"] = [{
            "categoria": r[0], "ts": str(r[1]),
            "evento": r[2], "resumen": r[3],
        } for r in cur.fetchall()]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        conn.close()
    return out