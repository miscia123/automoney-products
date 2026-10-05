"""Notifiche: Telegram (consigliato, arriva sul telefono in un secondo) ed email."""
from __future__ import annotations

import html
import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import unquote, urlparse

from .http import Http
from .models import Deal, SaleKind

log = logging.getLogger(__name__)

LEVEL_LABEL = {"hot": "🔥 GRANDE AFFARE", "good": "✅ Buon affare", "watch": "👀 Da tenere d'occhio"}
CAT_LABEL = {"watch": "orologio", "gold": "oro", "jewelry": "gioiello", "bullion_coin": "moneta/lingotto",
             "coin": "moneta", "card": "carta", "bag": "borsa", "art": "arte", "wine": "vino",
             "collectible": "collezionismo", "other": "altro"}


def _eur(x: float | None) -> str:
    if x is None:
        return "n.d."
    return f"€ {x:,.0f}".replace(",", ".")


def _num(x: float) -> str:
    return f"{x:,.0f}".replace(",", ".")


def _time_left(deal: Deal) -> str:
    if not deal.listing.ends_at:
        return ""
    end = deal.listing.ends_at
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    h = (end - datetime.now(timezone.utc)).total_seconds() / 3600
    if h < 0:
        return "terminata"
    if h < 1:
        return f"{h * 60:.0f} minuti"
    return f"{h:.0f} ore" if h < 48 else f"{h / 24:.0f} giorni"


def format_deal(deal: Deal) -> str:
    l, v, a = deal.listing, deal.valuation, deal.attrs
    e = html.escape
    lines = [
        f"<b>{LEVEL_LABEL.get(deal.level, deal.level)}</b> · {e(l.source)} · {CAT_LABEL.get(a.category.value, '')}",
        f"<a href=\"{e(l.url)}\">{e(l.title[:150])}</a>",
        "",
        f"💶 Prezzo: <b>{_num(l.price)} {l.currency}</b>"
        + (f" (offerta attuale, {l.bids or 0} offerte, fine tra {_time_left(deal)})" if l.kind == SaleKind.AUCTION else ""),
        f"🧾 Costo finale in mano: <b>{_eur(deal.cost.total)}</b> (comm. {_eur(deal.cost.buyer_premium + deal.cost.vat_on_premium)},"
        f" sped. {_eur(deal.cost.shipping)}" + (f", dazi+IVA {_eur(deal.cost.import_duty + deal.cost.import_vat + deal.cost.other_fees)}"
                                                  if deal.cost.import_vat or deal.cost.import_duty else "") + ")",
        f"📊 Valore di mercato: <b>{_eur(v.fair_value)}</b> (range {_eur(v.low)}–{_eur(v.high)})",
        f"    ↳ {e(v.method)}; affidabilità stima {v.confidence:.0%}",
    ]
    if l.kind == SaleKind.AUCTION and l.raw.get("predicted_final") and l.raw["predicted_final"] > l.price:
        lines.insert(4, f"📈 Prezzo finale stimato: {_num(l.raw['predicted_final'])} {l.currency} (i calcoli usano questo)")
    if v.melt_value:
        lines.append(f"    ↳ valore del metallo fino: {_eur(v.melt_value)}")
    if deal.repair.high:
        lines.append(f"🔧 Riparazioni: {_eur(deal.repair.low)}–{_eur(deal.repair.high)} ({e('; '.join(deal.repair.items))})")
    lines += [
        f"💰 Guadagno stimato: <b>{_eur(deal.profit)}</b> (ROI {deal.roi:.0%}) vendendo su {e(deal.exit_channel)}",
        f"⏱ Tempo di rivendita: {deal.resale_days[0]}–{deal.resale_days[1]} giorni",
        f"🛡 Affidabilità: <b>{deal.reliability}/100</b> (rischio {deal.risk})",
    ]
    if deal.max_bid is not None:
        lines.append(f"🎯 Offerta massima consigliata: <b>{_num(deal.max_bid)} {l.currency}</b>")
    if deal.risk_reasons[1:]:
        lines.append("⚠️ " + e("; ".join(deal.risk_reasons[1:5])))
    if deal.llm_notes:
        n = deal.llm_notes
        lines.append(f"🤖 Perito AI ({n.get('verdict')}): {e(n.get('summary_it', ''))}")
        if n.get("red_flags"):
            lines.append("    🚩 " + e("; ".join(n["red_flags"][:4])))
    comps = [c for c in v.comps if c.url][:3]
    if comps:
        lines.append("🔎 Venduti simili: " + " · ".join(
            f"<a href=\"{e(c.url)}\">{_eur(c.price_eur)}</a>" for c in comps))
    return "\n".join(lines)


class Notifier:
    def __init__(self, http: Http, cfg: dict, secrets: dict):
        self.http = http
        self.cfg = cfg
        self.secrets = secrets

    @property
    def telegram_ready(self) -> bool:
        return bool(self.cfg.get("telegram", {}).get("enabled") and self.secrets.get("telegram_token")
                    and self.secrets.get("telegram_chat_id"))

    async def send_deal(self, deal: Deal) -> bool:
        text = format_deal(deal)
        sent = False
        if self.telegram_ready and deal.level in self.cfg["telegram"].get("levels", ["hot", "good"]):
            sent |= await self.telegram(text, photo=deal.listing.images[0] if deal.listing.images else None)
        if self.cfg.get("email", {}).get("enabled") and deal.level in self.cfg["email"].get("levels", ["hot"]):
            sent |= self.email(f"{LEVEL_LABEL.get(deal.level)}: {deal.listing.title[:80]}", text)
        if not sent:
            log.info("ALERT (nessun canale configurato):\n%s", text)
        return sent

    async def telegram(self, text: str, photo: str | None = None) -> bool:
        token, chat = self.secrets["telegram_token"], self.secrets["telegram_chat_id"]
        base = f"https://api.telegram.org/bot{token}"
        try:
            if photo and len(text) <= 1024:
                await self.http.post_json(f"{base}/sendPhoto", {"chat_id": chat, "photo": photo,
                                                                 "caption": text, "parse_mode": "HTML"}, retries=1)
            else:
                await self.http.post_json(f"{base}/sendMessage", {
                    "chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": False}}, retries=2)
            return True
        except Exception as e:
            if photo:  # la foto a volte non è scaricabile da Telegram: riprova solo testo
                return await self.telegram(text, None)
            log.warning("Telegram non raggiungibile: %s", e)
            return False

    def email(self, subject: str, body_html: str) -> bool:
        url, to = self.secrets.get("smtp_url"), self.secrets.get("email_to")
        if not url or not to:
            return False
        u = urlparse(url)
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, unquote(u.username or ""), to
        msg.set_content("Apri la versione HTML")
        msg.add_alternative(body_html.replace("\n", "<br>"), subtype="html")
        try:
            if u.scheme == "smtps":
                with smtplib.SMTP_SSL(u.hostname, u.port or 465, context=ssl.create_default_context()) as s:
                    s.login(unquote(u.username), unquote(u.password))
                    s.send_message(msg)
            else:
                with smtplib.SMTP(u.hostname, u.port or 587) as s:
                    s.starttls(context=ssl.create_default_context())
                    s.login(unquote(u.username), unquote(u.password))
                    s.send_message(msg)
            return True
        except Exception as e:
            log.warning("email non inviata: %s", e)
            return False

    async def send_text(self, text: str) -> None:
        if self.telegram_ready:
            await self.telegram(text)
        else:
            log.info("%s", text)
