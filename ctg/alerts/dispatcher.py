"""Alert dispatch over Telegram + email, with dedup so 24/7 running doesn't spam.

A `dedup_key` (e.g. "signal:RELIANCE:LONG:2026-06-13") is stored; re-sending the
same key within the dedup window is suppressed.
"""
from __future__ import annotations

import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText

import requests

from ..config import get_settings
from ..logging_conf import get_logger
from ..storage.db import now_iso, sqlite

log = get_logger("alerts")


def _already_sent(dedup_key: str, within_hours: int = 12) -> bool:
    con = sqlite()
    try:
        cutoff = (datetime.now() - timedelta(hours=within_hours)).isoformat()
        row = con.execute(
            "SELECT 1 FROM alerts WHERE dedup_key=? AND ts>=? LIMIT 1", (dedup_key, cutoff)
        ).fetchone()
        return row is not None
    finally:
        con.close()


def _log_alert(kind: str, dedup_key: str, channel: str, message: str) -> None:
    con = sqlite()
    try:
        con.execute(
            "INSERT INTO alerts(ts,kind,dedup_key,channel,message) VALUES(?,?,?,?,?)",
            (now_iso(), kind, dedup_key, channel, message[:2000]),
        )
        con.commit()
    finally:
        con.close()


def _send_telegram(message: str) -> bool:
    s = get_settings()
    if not s.has_telegram:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
            json={"chat_id": s.telegram_chat_id, "text": message,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=15,
        )
        return r.status_code == 200
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram send failed: %s", exc)
        return False


def _send_email(subject: str, body: str) -> bool:
    s = get_settings()
    if not s.has_email:
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = s.smtp_user
        msg["To"] = s.alert_email_to
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as server:
            server.starttls()
            server.login(s.smtp_user, s.smtp_password)
            server.sendmail(s.smtp_user, [s.alert_email_to], msg.as_string())
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Email send failed: %s", exc)
        return False


def dispatch(kind: str, title: str, message: str, dedup_key: str | None = None) -> dict:
    s = get_settings()
    dedup_key = dedup_key or f"{kind}:{title}"
    if _already_sent(dedup_key):
        return {"sent": False, "reason": "deduped"}

    full = f"*{title}*\n{message}"
    channels = []
    if _send_telegram(full):
        channels.append("telegram")
    if _send_email(title, message):
        channels.append("email")

    _log_alert(kind, dedup_key, ",".join(channels) or "none", full)
    if not channels:
        # No channel configured: still log so the dashboard shows it
        log.info("[ALERT/%s] %s — %s (no channel configured)", kind, title, message[:120])
    return {"sent": bool(channels), "channels": channels}


# --- high-level alert builders -----------------------------------------
def alert_signals(signals: list[dict]) -> None:
    s = get_settings()
    threshold = float(s.get("alerts", "min_conviction_for_alert", default=70))
    today = datetime.now().strftime("%Y-%m-%d")
    for sig in signals:
        if sig["conviction"] >= threshold:
            key = f"signal:{sig['symbol']}:{sig['direction']}:{today}"
            msg = (f"{sig['direction']} {sig['symbol']} ({sig['sector']})\n"
                   f"Conviction: {sig['conviction']}/100\n"
                   f"Exp. return: {sig['expected_return']:+.2f}% | Tail: {sig['tail_risk']:+.1f}%\n"
                   f"{sig.get('thesis','')}")
            dispatch("signal", f"🎯 High-conviction: {sig['symbol']}", msg, key)


def alert_regime_change(new_regime: dict, prev_label: str | None) -> None:
    s = get_settings()
    if not s.get("alerts", "regime_change_alert", default=True):
        return
    label = new_regime.get("label")
    if label and label != prev_label:
        today = datetime.now().strftime("%Y-%m-%d")
        msg = (f"Regime: {prev_label or '—'} → *{label}*\n"
               f"Risk score: {new_regime.get('risk_score')}/100 | "
               f"VIX: {new_regime.get('india_vix')} | trend: {new_regime.get('trend')}")
        dispatch("regime", "🌐 Regime change", msg, f"regime:{label}:{today}")


def alert_option_trades(trades_by_underlying: dict, min_fit: float = 68.0) -> None:
    """Push the single best option setup per index when it clears a fit bar."""
    today = datetime.now().strftime("%Y-%m-%d")
    for u, data in (trades_by_underlying or {}).items():
        if not data.get("available"):
            continue
        sugg = data.get("suggestions") or []
        if not sugg:
            continue
        best = sugg[0]
        if best.get("fit_score", 0) < min_fit:
            continue
        legs = "\n".join(
            f"  {l['action']} {l['type']} {int(l['strike'])} @ ₹{l['ltp']}" for l in best["legs"]
        )
        net = best["net_premium"]
        netlbl = f"credit ₹{abs(net):,.0f}" if net >= 0 else f"debit ₹{abs(net):,.0f}"
        msg = (f"{u} spot {data['spot']:.0f} · exp {data['expiry']}\n"
               f"*{best['strategy']}* (fit {best['fit_score']})\n{legs}\n"
               f"Net {netlbl} · BE {best.get('breakevens')}\n"
               f"Max P {best.get('max_profit')} / Max L {best.get('max_loss')}")
        dispatch("option_trade", f"📐 Option setup: {u}", msg,
                 f"optrade:{u}:{best['strategy'][:12]}:{today}")


def alert_unusual_flow(flow_metrics: dict) -> None:
    s = get_settings()
    if not s.get("alerts", "unusual_flow_alert", default=True):
        return
    fii = flow_metrics.get("fii_net_latest")
    today = datetime.now().strftime("%Y-%m-%d")
    if fii is not None and abs(fii) >= 3000:  # ₹3000cr+ single-day FII move
        side = "buying" if fii > 0 else "selling"
        dispatch("flow", "💸 Large FII flow",
                 f"FII net {side} ₹{abs(fii):.0f}cr today.\n"
                 f"DII net: ₹{flow_metrics.get('dii_net_latest',0):.0f}cr",
                 f"flow:{today}")
