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
        conn.commit()
    except Exception as e:
        print(f"[Audit] ensure_table FAIL: {e}")
        try: conn.rollback()
        except Exception: pass
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