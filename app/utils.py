"""Funzioni di supporto: identificativi, date, hash, mascheramento, validazioni."""
import hashlib
import hmac
import re
import secrets
import string
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

TZ_NAME = "Europe/Rome"
TZ = ZoneInfo(TZ_NAME)

_ALPHABET = string.ascii_uppercase + string.digits


def new_id(prefix: str, length: int = 8) -> str:
    return prefix + "-" + "".join(secrets.choice(_ALPHABET) for _ in range(length))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_local(s: str | None, with_tz: bool = True) -> str:
    """Formato leggibile in ora italiana, con indicazione del fuso."""
    dt = parse_iso(s)
    if not dt:
        return "—"
    loc = dt.astimezone(TZ)
    out = loc.strftime("%d/%m/%Y %H:%M:%S")
    if with_tz:
        out += f" ({TZ_NAME}, UTC{loc.strftime('%z')[:3]}:{loc.strftime('%z')[3:]})"
    return out


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_token(token: str, secret: str) -> str:
    """Nel DB si conserva solo l'HMAC del token: il link in chiaro non è mai archiviato."""
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return "—"
    local, _, domain = email.partition("@")
    vis = local[:2] if len(local) > 2 else local[:1]
    return f"{vis}{'*' * max(3, len(local) - len(vis))}@{domain}"


def mask_phone(phone: str) -> str:
    if not phone:
        return "—"
    digits = re.sub(r"\D", "", phone)
    if len(digits) <= 4:
        return "*" * len(digits)
    return phone[:3] + "*" * (len(phone) - 6) + phone[-3:] if len(phone) > 6 else "***" + phone[-2:]


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
CF_RE = re.compile(r"^[A-Z]{6}[0-9LMNPQRSTUV]{2}[ABCDEHLMPRST][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]$")


def valid_email(v: str) -> bool:
    return bool(v) and bool(EMAIL_RE.match(v.strip())) and len(v) <= 254


def normalize_phone(v: str) -> str:
    v = re.sub(r"[\s\-().]", "", v or "")
    if v.startswith("00"):
        v = "+" + v[2:]
    if v and not v.startswith("+"):
        v = "+39" + v  # prefisso Italia di default
    return v


def valid_phone(v: str) -> bool:
    return bool(re.match(r"^\+[1-9]\d{7,14}$", v or ""))


def cf_check_char(cf: str) -> bool:
    """Controllo formale del carattere di controllo del codice fiscale.
    È una verifica sintattica: NON è una verifica dell'identità della persona."""
    odd = {"0": 1, "1": 0, "2": 5, "3": 7, "4": 9, "5": 13, "6": 15, "7": 17, "8": 19, "9": 21,
           "A": 1, "B": 0, "C": 5, "D": 7, "E": 9, "F": 13, "G": 15, "H": 17, "I": 19, "J": 21,
           "K": 2, "L": 4, "M": 18, "N": 20, "O": 11, "P": 3, "Q": 6, "R": 8, "S": 12, "T": 14,
           "U": 16, "V": 10, "W": 22, "X": 25, "Y": 24, "Z": 23}
    even = {c: i for i, c in enumerate("0123456789")}
    even.update({c: i for i, c in enumerate(string.ascii_uppercase)})
    total = 0
    for i, c in enumerate(cf[:15]):
        total += odd[c] if i % 2 == 0 else even[c]
    return string.ascii_uppercase[total % 26] == cf[15]


def valid_fiscal_code(v: str) -> bool:
    v = (v or "").strip().upper()
    return bool(CF_RE.match(v)) and cf_check_char(v)


def client_ip(request) -> str | None:
    # Dietro un reverse proxy fidato si userebbe X-Forwarded-For; qui si usa l'indirizzo diretto.
    return request.remote_addr


def device_info(request) -> str | None:
    ua = request.headers.get("User-Agent", "")
    return ua[:300] if ua else None


def random_otp(length: int) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(length))


def expires_in(days: int) -> str:
    return iso_utc(now_utc() + timedelta(days=days))
