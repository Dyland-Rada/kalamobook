"""
Telegram notifications para monitoreo desde el celular.

Setup:
1. Habla con @BotFather → /newbot → guarda el TOKEN (algo tipo 123456:ABC-DEF...)
2. Habla con @userinfobot (o cualquier bot que dé tu user ID) → guarda tu chat_id
3. Manda /start a tu nuevo bot UNA VEZ (sino el bot no puede iniciar conversacion contigo)
4. Setea env vars en el server:
     TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
     TELEGRAM_CHAT_ID=123456789

Multi-server: cada server manda sus propios mensajes prefijados con WORKER_NAME.
Si quieres recibir solo de un server, pon el TOKEN solo en ese — los que no
tienen las env vars no mandan nada (silenciosos, sin error).
"""
import os
import aiohttp

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
WORKER_LABEL = os.environ.get("WORKER_NAME", "default")


def is_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


async def send_telegram(text: str, parse_mode: str = "Markdown",
                        silent: bool = False) -> bool:
    """
    Envia un mensaje al chat configurado. No-op si TELEGRAM_BOT_TOKEN o
    TELEGRAM_CHAT_ID estan vacios. Retorna True si OK, False si error.

    parse_mode: "Markdown" o "HTML" o "" (sin formato)
    silent: si True usa disable_notification (no hace ping en el celular)
    """
    if not is_configured():
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    prefix = f"🤖 *{WORKER_LABEL}*\n"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": prefix + text,
        "parse_mode": parse_mode,
        "disable_notification": silent,
        "disable_web_page_preview": True,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=payload) as r:
                if r.status == 200:
                    return True
                body = await r.text()
                print(f"[Notify] Telegram returned {r.status}: {body[:200]}")
                return False
    except Exception as e:
        print(f"[Notify] Telegram send error: {e}")
        return False


def _fmt_int(n) -> str:
    """Formato '1,234' con separador de miles."""
    try:
        return f"{int(n):,}".replace(",", ".")
    except Exception:
        return str(n)


async def notify_job_started(target_total: int):
    await send_telegram(
        f"🚀 *Job arrancado*\n"
        f"Target: *{_fmt_int(target_total)}* libros a enriquecer\n"
        f"Vas a recibir un reporte cada hora."
    )


async def notify_job_stopped(written: int, reason: str = "stopped"):
    icon = "✅" if reason == "completed" else "⏹"
    await send_telegram(
        f"{icon} *Job {reason}*\n"
        f"Total escritos a Odoo: *{_fmt_int(written)}*"
    )


async def notify_stats(job: dict, counts: dict, delta_written: int,
                       interval_min: int):
    """Reporte horario con los counters mas relevantes."""
    target = job.get("odoo_total_target", 0) or 1
    written = job.get("written", 0)
    notfound = job.get("notfound", 0)
    cache_hits = job.get("cache_hits", 0)
    direct_hits = job.get("direct_hits", 0)
    gbooks_hits = job.get("gbooks_hits", 0)
    gbooks_merged = job.get("gbooks_merged", 0)
    progress = (written / target * 100) if target else 0
    rate_min = delta_written / interval_min if interval_min else 0

    eta_h = (target - written) / max(rate_min * 60, 1) if rate_min > 0 else 0
    eta_str = f"{eta_h:.0f}h" if eta_h < 72 else f"{eta_h / 24:.1f} días"

    gb_line = f"📚 Google Books: {_fmt_int(gbooks_hits)}"
    if gbooks_merged:
        gb_line += f" (+{_fmt_int(gbooks_merged)} mergeados)"

    msg = (
        f"📊 *Reporte {interval_min} min*\n\n"
        f"✅ Escritos: *{_fmt_int(written)}* / {_fmt_int(target)} "
        f"({progress:.2f}%)\n"
        f"⚡ Ritmo: {rate_min:.0f}/min ({_fmt_int(delta_written)} en {interval_min} min)\n"
        f"⏱ ETA restante: ~{eta_str}\n\n"
        f"⏳ Pendientes: {_fmt_int(counts.get('pending', 0))}\n"
        f"🔄 Scraping: {counts.get('scraping', 0)}\n"
        f"📤 Pushing: {counts.get('pushing', 0)}\n"
        f"❌ Not found: {_fmt_int(notfound)}\n\n"
        f"🎯 Cache hits: {_fmt_int(cache_hits)}\n"
        f"⚡ Direct URL: {_fmt_int(direct_hits)}\n"
        f"{gb_line}"
    )
    await send_telegram(msg, silent=True)


async def notify_alert(level: str, title: str, body: str = ""):
    """
    level: 'warn' | 'error' | 'critical'
    Mensaje no-silent (hace ping fuerte).
    """
    icons = {"warn": "⚠️", "error": "❌", "critical": "🚨"}
    icon = icons.get(level, "⚠️")
    msg = f"{icon} *{title}*"
    if body:
        msg += f"\n{body}"
    await send_telegram(msg, silent=False)


# ── Command dispatcher (incoming webhook) ───────────────────────────────
async def build_status_text() -> str:
    """Snapshot del estado actual de la cola compartida en Postgres."""
    import db
    from enrichment import _count_queue_by_status, get_enrichment_status, get_notfound_count

    try:
        counts = _count_queue_by_status()
        notfound = get_notfound_count()
    except Exception as e:
        return f"❌ Error al consultar la BD: {e}"

    # Desglose por servidor para los workers activos
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        db.execute_query(cur, """
            SELECT claimed_by, status, COUNT(*)
            FROM enrichment_queue
            WHERE status IN ('scraping', 'pushing')
            GROUP BY claimed_by, status
            ORDER BY claimed_by, status
        """, ())
        by_server = cur.fetchall()
        conn.close()
    except Exception:
        by_server = []

    job = get_enrichment_status() or {}
    target = job.get("odoo_total_target", 0)

    written = counts.get("written", 0)
    pending = counts.get("pending", 0)
    scraping = counts.get("scraping", 0)
    pushing = counts.get("pushing", 0)
    scraped = counts.get("scraped", 0)

    total_processed = written + notfound
    progress_pct = (total_processed / target * 100) if target else 0

    by_server_lines = []
    for row in by_server:
        cb = (row[0] or "?")
        by_server_lines.append(f"  `{cb}` {row[1]}: *{row[2]}*")
    by_server_text = "\n".join(by_server_lines) if by_server_lines else "  _(sin workers activos)_"

    progress_line = (
        f"📈 Progreso: *{progress_pct:.2f}%*  ({_fmt_int(total_processed)} / {_fmt_int(target)})"
        if target else
        f"📈 Procesados: *{_fmt_int(total_processed)}*  _(target desconocido en este server)_"
    )

    return (
        f"📊 *Status actual*\n\n"
        f"{progress_line}\n\n"
        f"✅ Escritos: *{_fmt_int(written)}*\n"
        f"❌ Not found: {_fmt_int(notfound)}\n"
        f"⏳ Pendientes: {_fmt_int(pending)}\n"
        f"🔄 Scraping: {scraping}\n"
        f"📤 Pushing: {pushing}\n"
        + (f"📦 Scraped en espera: {scraped}\n" if scraped else "")
        + f"\n*Workers activos por server:*\n{by_server_text}"
    )


def _help_text() -> str:
    return (
        "🤖 *Comandos disponibles*\n\n"
        "/status — snapshot actual de la cola compartida\n"
        "/ping — verificar que el bot responde\n"
        "/help — esta ayuda\n\n"
        "_Los datos vienen del Postgres compartido entre ambos servers._"
    )


async def handle_command(message: dict) -> str | None:
    """
    Procesa un mensaje de Telegram. Retorna el texto de respuesta o None
    si no es un comando reconocido o si el chat_id no esta autorizado.
    """
    chat_id = str(message.get("chat", {}).get("id", ""))
    if not chat_id or chat_id != TELEGRAM_CHAT_ID:
        # Ignorar mensajes de chats no autorizados (seguridad basica)
        print(f"[Notify] Ignored message from chat={chat_id}")
        return None

    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return None

    # Soporta /status@nombre_bot tambien
    cmd = text.split()[0].split("@")[0].lower()

    if cmd in ("/start", "/help"):
        return _help_text()
    if cmd == "/ping":
        return f"🏓 Pong desde *{WORKER_LABEL}*"
    if cmd == "/status":
        return await build_status_text()

    return f"❓ Comando desconocido: `{cmd}`\nUsa /help para ver opciones."


async def register_webhook(webhook_url: str, secret: str = "") -> dict:
    """Llama a Telegram setWebhook para apuntar el bot a webhook_url."""
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN no configurado"}
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
    payload = {"url": webhook_url, "allowed_updates": ["message"]}
    if secret:
        payload["secret_token"] = secret
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=payload) as r:
                return await r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def delete_webhook() -> dict:
    """Quita el webhook (para apagar comandos sin tocar tokens)."""
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN no configurado"}
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url) as r:
                return await r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}
