"""Webhook WhatsApp Cloud API (modalità reale): riscontri di consegna.

Senza questo riscontro il sistema non indica mai "consegnato".
"""
import hmac

from flask import Blueprint, request, current_app, abort

from . import services
from .db import get_db, transaction
from .utils import iso_utc

bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


@bp.route("/whatsapp", methods=["GET", "POST"])
def whatsapp():
    cfg = current_app.config
    if cfg["DEMO"]:
        abort(404)
    verify = cfg.get("WA_WEBHOOK_VERIFY_TOKEN")
    if request.method == "GET":
        if verify and hmac.compare_digest(request.args.get("hub.verify_token", ""), verify):
            return request.args.get("hub.challenge", ""), 200
        abort(403)
    data = request.get_json(silent=True) or {}
    conn = get_db()
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for st in (change.get("value") or {}).get("statuses", []):
                msg_id, status = st.get("id"), st.get("status")
                if not msg_id or status not in ("delivered", "read"):
                    continue
                row = conn.execute("SELECT * FROM outbox WHERE provider_ref=? AND channel='whatsapp'", (msg_id,)).fetchone()
                if not row or row["status"] == "consegnato":
                    continue
                with transaction(conn):
                    conn.execute("UPDATE outbox SET status='consegnato' WHERE id=?", (row["id"],))
                    if row["request_id"] and row["purpose"] == "link":
                        services.add_event(conn, row["request_id"], row["org_id"], "link_delivered", "system",
                                           details={"canale": "whatsapp", "riscontro": status, "ricevuto": iso_utc()})
    return {"ok": True}
