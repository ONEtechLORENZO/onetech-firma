"""Configurazione dell'applicazione.

Tutto ciò che è sensibile (chiavi, credenziali provider) arriva da variabili
d'ambiente. In modalità demo la chiave segreta viene generata una volta e
salvata nella cartella dati, così la demo parte senza configurazione.
"""
import os
import secrets
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Config:
    # Nome del prodotto, modificabile da variabile d'ambiente.
    APP_NAME = os.environ.get("APP_NAME", "One Tech")
    APP_TAGLINE = "Invio e firma elettronica semplice con Audit Trail"

    # "demo" = invii simulati, OTP di test visibile nella console operatore.
    # "real" = integrazioni server-side (email, WhatsApp) tramite provider.
    MODE = os.environ.get("APP_MODE", "demo").strip().lower()
    DEMO = MODE != "real"

    BASE_DIR = Path(__file__).resolve().parent.parent
    DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
    DB_PATH = DATA_DIR / "firma.sqlite3"
    FILES_DIR = DATA_DIR / "files"       # PDF originali, compilati, finali, audit
    RENDER_DIR = DATA_DIR / "renders"    # anteprime PNG delle pagine

    # URL pubblico dei link cliente. Se vuoto, si usa l'indirizzo con cui l'operatore sta usando l'app.
    BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")

    MAX_PDF_MB = int(os.environ.get("MAX_PDF_MB", "10"))
    MAX_PAGES = int(os.environ.get("MAX_PAGES", "50"))

    # OTP
    OTP_LENGTH = 6
    OTP_TTL_SECONDS = 300          # 5 minuti
    OTP_MAX_ATTEMPTS = 5
    OTP_MAX_RESENDS = 3
    OTP_RESEND_COOLDOWN_SECONDS = 30

    # Link cliente
    LINK_DEFAULT_DAYS = 7
    LINK_MAX_DAYS = 30

    # Rate limiting (per IP, in memoria): richieste max per finestra
    RATE_LIMIT_WINDOW_SECONDS = 600
    RATE_LIMIT_MAX = 60

    # Provider (modalità reale). Se mancano → "non configurato".
    SMTP_HOST = os.environ.get("SMTP_HOST")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
    SMTP_FROM = os.environ.get("SMTP_FROM")
    SMTP_TLS = _env_bool("SMTP_TLS", True)

    WA_ACCESS_TOKEN = os.environ.get("WA_ACCESS_TOKEN")
    WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID")
    WA_TEMPLATE_LINK = os.environ.get("WA_TEMPLATE_LINK")      # template approvato per il link
    WA_TEMPLATE_OTP = os.environ.get("WA_TEMPLATE_OTP")        # template approvato (categoria authentication)
    WA_TEMPLATE_LANG = os.environ.get("WA_TEMPLATE_LANG", "it")
    # Solo per prove live: messaggi di testo libero invece dei template. Meta li consegna
    # solo se il destinatario ha scritto al numero aziendale nelle ultime 24 ore.
    WA_TEXT_MODE = _env_bool("WA_TEXT_MODE", False)
    WA_WEBHOOK_VERIFY_TOKEN = os.environ.get("WA_WEBHOOK_VERIFY_TOKEN")

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env_bool("COOKIE_SECURE", False)
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 8

    @classmethod
    def secret_key(cls) -> str:
        key = os.environ.get("SECRET_KEY")
        if key:
            return key
        if not cls.DEMO:
            raise RuntimeError("In modalità reale SECRET_KEY deve essere impostata come variabile d'ambiente.")
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)
        p = cls.DATA_DIR / ".secret_key"
        if p.exists():
            return p.read_text().strip()
        key = secrets.token_urlsafe(48)
        p.write_text(key)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return key
