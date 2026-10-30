"""Protezione dai tentativi ripetuti: finestra fissa per chiave (es. IP + azione), su SQLite."""
from flask import current_app, request

from .db import get_db
from .utils import now_utc, iso_utc, parse_iso


def rate_limited(action: str, limit: int | None = None, window: int | None = None) -> bool:
    cfg = current_app.config
    limit = limit or cfg["RATE_LIMIT_MAX"]
    window = window or cfg["RATE_LIMIT_WINDOW_SECONDS"]
    key = f"{action}:{request.remote_addr or 'na'}"
    conn = get_db()
    row = conn.execute("SELECT * FROM rate_limits WHERE key=?", (key,)).fetchone()
    now = now_utc()
    if not row or (now - parse_iso(row["window_start"])).total_seconds() > window:
        conn.execute("INSERT OR REPLACE INTO rate_limits(key, window_start, count) VALUES (?,?,1)", (key, iso_utc(now)))
        return False
    if row["count"] >= limit:
        return True
    conn.execute("UPDATE rate_limits SET count=count+1 WHERE key=?", (key,))
    return False
