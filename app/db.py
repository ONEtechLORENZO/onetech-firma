"""Accesso al database SQLite (persistenza locale, zero configurazione)."""
import sqlite3
from contextlib import contextmanager
from flask import g, current_app

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS orgs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    filename TEXT NOT NULL,
    path_original TEXT NOT NULL,
    sha256_original TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    page_count INTEGER NOT NULL,
    pages_json TEXT NOT NULL,          -- [{w,h,rotation}] dimensioni visibili in punti PDF
    form_fields_json TEXT NOT NULL,    -- campi compilabili trovati nel PDF (se presenti)
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES documents(id),
    fields_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    title TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'bozza',
    document_id TEXT REFERENCES documents(id),
    fields_json TEXT NOT NULL DEFAULT '[]',
    client_first_name TEXT NOT NULL DEFAULT '',
    client_last_name TEXT NOT NULL DEFAULT '',
    client_fiscal_code TEXT NOT NULL DEFAULT '',
    client_email TEXT NOT NULL DEFAULT '',
    client_phone TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT 'email',   -- canale del LINK: email | whatsapp (l'OTP viaggia sempre su WhatsApp)
    signer_role TEXT NOT NULL DEFAULT 'Il Cliente',  -- intestazione del riquadro firma (es. "Il Delegante")
    place TEXT NOT NULL DEFAULT '',          -- luogo di sottoscrizione dichiarato (es. "25045 CASTEGNATO BS")
    geo_json TEXT,                           -- ultima geolocalizzazione fornita dal cliente (consenso del browser)
    expires_at TEXT,
    link_token_hash TEXT,
    demo_link_token TEXT,              -- valorizzato SOLO in modalità demo (apertura dal pannello operatore)
    link_revoked INTEGER NOT NULL DEFAULT 0,
    path_filled TEXT, sha256_filled TEXT,
    path_final TEXT,  sha256_final TEXT,
    path_audit TEXT,  sha256_audit TEXT,
    declaration_text TEXT NOT NULL DEFAULT '',
    declaration_accepted_at TEXT,
    otp_verified_at TEXT,
    signed_at TEXT,
    dispute_reason TEXT,
    last_error TEXT,
    sent_at TEXT,
    opened_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_org ON requests(org_id, created_at);
CREATE INDEX IF NOT EXISTS idx_requests_token ON requests(link_token_hash);

CREATE TABLE IF NOT EXISTS otps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES requests(id),
    code_hash TEXT NOT NULL,
    demo_code TEXT,                     -- valorizzato SOLO in modalità demo
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    used_at TEXT,
    invalidated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_otps_request ON otps(request_id, id);

-- Registro eventi append-only con catena di hash (ogni riga include l'hash della precedente).
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES requests(id),
    org_id TEXT NOT NULL,
    type TEXT NOT NULL,
    ts_utc TEXT NOT NULL,
    actor TEXT NOT NULL,                -- operator:<user_id> | client | system
    ip TEXT,
    user_agent TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_request ON events(request_id, id);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    request_id TEXT,
    text TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Messaggi in uscita (email / WhatsApp). In demo: simulati. In reale: esito del provider.
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id TEXT NOT NULL,
    request_id TEXT,
    channel TEXT NOT NULL,              -- email | whatsapp
    purpose TEXT NOT NULL,              -- link | otp | notify_operator
    recipient_masked TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,                 -- senza OTP e senza token segreti
    mode TEXT NOT NULL,                 -- demo | real
    status TEXT NOT NULL,               -- simulato | inviato | consegnato | errore | non_configurato
    provider_ref TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limits (
    key TEXT PRIMARY KEY,
    window_start TEXT NOT NULL,
    count INTEGER NOT NULL
);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DB_PATH"],
            detect_types=sqlite3.PARSE_DECLTYPES,
            timeout=10,
            isolation_level=None,  # autocommit; le transazioni sono esplicite
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        g.db = conn
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(app):
    app.config["DB_PATH"].parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Transazione esclusiva: evita doppie conferme e richieste concorrenti."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
