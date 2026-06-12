"""
Tracker de salud por proxy. Marca como muerta una proxy que acumula
N fallos consecutivos tipo conexion/tunnel. El pool de browsers la salta.

NO persiste: si reinicias el contenedor, todas las proxies vuelven a 'alive'.
Eso es intencional — Webshare a veces resucita proxies muertas.
"""
import time
from threading import Lock


# Estado por proxy_spec (el string raw del PROXY_POOL)
# {
#     "http://user:pass@1.2.3.4:8080": {
#         "consecutive_failures": 0,
#         "total_failures": 0,
#         "total_successes": 0,
#         "dead": False,
#         "dead_since": None,
#         "last_event_at": timestamp,
#     }
# }
_state: dict[str, dict] = {}
_lock = Lock()

# Threshold: tras N fallos consecutivos sin un success de por medio,
# la proxy se marca muerta. Suficientemente alto para no matar por
# 1-2 timeouts ocasionales, suficientemente bajo para reaccionar
# antes de quemar miles de libros.
DEAD_THRESHOLD = 5


def _ensure(spec: str):
    if spec not in _state:
        _state[spec] = {
            "consecutive_failures": 0,
            "total_failures": 0,
            "total_successes": 0,
            "dead": False,
            "dead_since": None,
            "last_event_at": None,
        }


def mark_failed(spec: str | None, reason: str = ""):
    """Reporta un fallo de scrape con esta proxy. Si pasa el threshold,
    se marca muerta."""
    if not spec:
        return  # IP directa, nada que trackear
    with _lock:
        _ensure(spec)
        s = _state[spec]
        s["consecutive_failures"] += 1
        s["total_failures"] += 1
        s["last_event_at"] = time.time()
        if not s["dead"] and s["consecutive_failures"] >= DEAD_THRESHOLD:
            s["dead"] = True
            s["dead_since"] = time.time()
            print(f"[ProxyHealth] DEAD {spec[:40]} tras {DEAD_THRESHOLD} "
                  f"fallos consecutivos: {reason[:80]}")


def mark_success(spec: str | None):
    """Reporta un scrape exitoso con esta proxy. Resetea consecutivos."""
    if not spec:
        return
    with _lock:
        _ensure(spec)
        s = _state[spec]
        s["consecutive_failures"] = 0
        s["total_successes"] += 1
        s["last_event_at"] = time.time()
        # Si estaba muerta y de algun modo nos llego un success, resucitar
        # (el caller debio haber filtrado, pero por las dudas)
        if s["dead"]:
            s["dead"] = False
            s["dead_since"] = None
            print(f"[ProxyHealth] RESURRECTED {spec[:40]} tras success")


def is_alive(spec: str | None) -> bool:
    """True si la proxy NO esta muerta. Para IP directa (spec=None) siempre True."""
    if not spec:
        return True
    with _lock:
        _ensure(spec)
        return not _state[spec]["dead"]


def get_alive_proxies(pool: list[str]) -> list[str]:
    """Filtra el pool dejando solo proxies vivas."""
    return [p for p in pool if is_alive(p)]


def snapshot() -> dict:
    """Para endpoint diagnostico — copia del estado actual."""
    with _lock:
        return {
            spec: {**stats, "spec_masked": _mask(spec)}
            for spec, stats in _state.items()
        }


def reset_all():
    """Resetea TODO. Util si Webshare resucita las proxies."""
    with _lock:
        for s in _state.values():
            s["consecutive_failures"] = 0
            s["dead"] = False
            s["dead_since"] = None


def _mask(spec: str) -> str:
    """Oculta password en logs: http://user:***@host:port"""
    if "@" in spec:
        head, host = spec.split("@", 1)
        # head = "http://user:password"
        if ":" in head:
            scheme_user, _ = head.rsplit(":", 1)
            return f"{scheme_user}:***@{host}"
    return spec