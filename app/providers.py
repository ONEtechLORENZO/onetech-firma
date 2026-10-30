"""Provider di invio (email, WhatsApp) e consegna OTP.

Modalità demo  → nessun invio reale: i messaggi finiscono in "outbox" come SIMULATI.
Modalità reale → SMTP per le email, WhatsApp Cloud API (template approvati) per WhatsApp.
                 Lo stato "inviato" viene registrato solo se il provider risponde con successo;
                 "consegnato" solo a fronte di un riscontro (webhook WhatsApp). Senza credenziali
                 o template il servizio è mostrato come NON CONFIGURATO.
"""
import json
import smtplib
import urllib.request
import urllib.error
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass
class SendResult:
    status: str                 # simulato | inviato | errore | non_configurato
    provider_ref: str | None = None
    error: str | None = None


def email_configured(cfg) -> bool:
    return bool(cfg.get("SMTP_HOST") and cfg.get("SMTP_FROM"))


def whatsapp_configured(cfg, purpose: str) -> bool:
    base = bool(cfg.get("WA_ACCESS_TOKEN") and cfg.get("WA_PHONE_NUMBER_ID"))
    if cfg.get("WA_TEXT_MODE"):
        return base
    tpl = cfg.get("WA_TEMPLATE_LINK") if purpose == "link" else cfg.get("WA_TEMPLATE_OTP")
    return base and bool(tpl)


def service_status(cfg) -> dict:
    """Stato dei servizi da mostrare all'operatore."""
    if cfg.get("DEMO"):
        return {
            "mode": "demo",
            "email": "simulato", "whatsapp": "simulato", "otp": "OTP di test (solo ambiente demo)",
        }
    return {
        "mode": "real",
        "email": "configurato" if email_configured(cfg) else "non configurato",
        "whatsapp": "configurato" if whatsapp_configured(cfg, "link") and whatsapp_configured(cfg, "otp")
        else "non configurato",
        "otp": "consegna tramite il canale scelto",
    }


# ------------------------------------------------------------------ email

def send_email(cfg, to_addr: str, subject: str, body: str) -> SendResult:
    if cfg.get("DEMO"):
        return SendResult("simulato")
    if not email_configured(cfg):
        return SendResult("non_configurato", error="SMTP non configurato (SMTP_HOST / SMTP_FROM).")
    msg = EmailMessage()
    msg["From"] = cfg["SMTP_FROM"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=20) as s:
            if cfg.get("SMTP_TLS"):
                s.starttls()
            if cfg.get("SMTP_USER"):
                s.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"] or "")
            s.send_message(msg)
        # SMTP accetta il messaggio: è "inviato", non "consegnato".
        return SendResult("inviato", provider_ref="smtp-accepted")
    except Exception as e:  # noqa: BLE001
        return SendResult("errore", error=f"Errore SMTP: {type(e).__name__}")


# ------------------------------------------------------------------ WhatsApp

def send_whatsapp_template(cfg, to_phone: str, purpose: str, params: list[str],
                           button_param: str | None = None, text: str | None = None) -> SendResult:
    if cfg.get("DEMO"):
        return SendResult("simulato")
    if not whatsapp_configured(cfg, purpose):
        return SendResult("non_configurato", error="WhatsApp non configurato (token, phone number id o template mancanti).")
    if cfg.get("WA_TEXT_MODE") and text:
        payload = {
            "messaging_product": "whatsapp",
            "to": to_phone.lstrip("+"),
            "type": "text",
            "text": {"body": text, "preview_url": purpose == "link"},
        }
    else:
        template = cfg["WA_TEMPLATE_LINK"] if purpose == "link" else cfg["WA_TEMPLATE_OTP"]
        components = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]
        if button_param is not None:
            # Template "authentication" (OTP) o pulsante URL dinamico: parametro del pulsante
            components.append({"type": "button", "sub_type": "url", "index": "0",
                               "parameters": [{"type": "text", "text": button_param}]})
        payload = {
            "messaging_product": "whatsapp",
            "to": to_phone.lstrip("+"),
            "type": "template",
            "template": {"name": template, "language": {"code": cfg.get("WA_TEMPLATE_LANG", "it")},
                         "components": components},
        }
    req = urllib.request.Request(
        f"https://graph.facebook.com/v20.0/{cfg['WA_PHONE_NUMBER_ID']}/messages",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {cfg['WA_ACCESS_TOKEN']}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
            msg_id = (data.get("messages") or [{}])[0].get("id")
            return SendResult("inviato", provider_ref=msg_id)  # "accepted" dal provider; consegna via webhook
    except urllib.error.HTTPError as e:
        return SendResult("errore", error=f"WhatsApp API HTTP {e.code}")
    except Exception as e:  # noqa: BLE001
        return SendResult("errore", error=f"Errore WhatsApp: {type(e).__name__}")
