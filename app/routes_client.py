"""Pagina cliente: link protetto, visualizzazione, controllo dati, dichiarazione, OTP, completamento."""
from pathlib import Path

from flask import (Blueprint, render_template, request, redirect, url_for, abort, send_file, Response,
                   current_app, flash)

from . import services, pdfops
from .db import get_db
from .ratelimit import rate_limited
from .utils import client_ip, device_info, mask_email, mask_phone

bp = Blueprint("client", __name__, url_prefix="/c")


def _resolve(token):
    if rate_limited("client", limit=300):
        abort(429)
    req, reason = services.request_by_token(get_db(), token)
    if req is None:
        abort(404)
    return req, reason


def _ctx():
    return {"ip": client_ip(request), "ua": device_info(request)}


def _csrf():
    from . import check_csrf
    check_csrf()


@bp.route("/<token>")
def landing(token):
    conn = get_db()
    req, reason = _resolve(token)
    if reason:
        return render_template("client/unavailable.html", reason=reason, req=req), 410
    if req["status"] == "completata":
        return redirect(url_for("client.done", token=token))
    # Dopo un'azione (accettazione, OTP) la pagina viene ricaricata con ?r=1: non è una nuova apertura del link.
    if request.args.get("r") != "1":
        req = services.client_open(conn, req, **_ctx())
        services.client_presented(conn, req, **_ctx())
    org = services.org_name(conn, req["org_id"])
    sender = conn.execute("SELECT name FROM users WHERE id=?", (req["user_id"],)).fetchone()
    recipient = mask_phone(req["client_phone"]) if req["channel"] == "whatsapp" else mask_email(req["client_email"])
    otp_pending = conn.execute("SELECT id, expires_at FROM otps WHERE request_id=? AND used_at IS NULL AND invalidated=0 "
                               "ORDER BY id DESC LIMIT 1", (req["id"],)).fetchone()
    return render_template("client/sign.html", req=req, token=token, org=org, sender=sender,
                           recipient=recipient, otp_pending=otp_pending,
                           page_count=_page_count(req), otp_len=current_app.config["OTP_LENGTH"],
                           otp_minutes=current_app.config["OTP_TTL_SECONDS"] // 60)


def _page_count(req):
    import pymupdf
    try:
        with pymupdf.open(req["path_filled"]) as d:
            return d.page_count
    except Exception:
        return 0


@bp.route("/<token>/page/<int:n>.png")
def page(token, n):
    req, reason = _resolve(token)
    if reason and reason != "completato":
        abort(404)
    path = req["path_final"] if req["status"] == "completata" and req["path_final"] else req["path_filled"]
    if not path:
        abort(404)
    cache = Path(current_app.config["RENDER_DIR"]) / req["org_id"] / "client" / req["id"] / f"{req['status']}_p{n}.png"
    if cache.exists():
        return Response(cache.read_bytes(), mimetype="image/png")
    try:
        png = pdfops.render_page_png(path, n, 900)
    except Exception:
        abort(404)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(png)
    return Response(png, mimetype="image/png")


@bp.route("/<token>/download/<which>")
def download(token, which):
    req, reason = _resolve(token)
    if reason:
        abort(404)
    conn = get_db()
    if which == "documento" and req["status"] != "completata":
        path, name = req["path_filled"], f"{req['id']}_documento.pdf"
    elif which == "finale" and req["status"] == "completata":
        path, name = req["path_final"], f"{req['id']}_documento_finale.pdf"
    elif which == "audit" and req["status"] == "completata":
        path, name = req["path_audit"], f"{req['id']}_audit_trail.pdf"
    else:
        abort(404)
    if not path or not Path(path).exists():
        abort(404)
    services.client_download(conn, req, which, **_ctx())
    return send_file(path, mimetype="application/pdf", as_attachment=True, download_name=name)


@bp.post("/<token>/geo")
def geo(token):
    """Posizione facoltativa dal browser (navigator.geolocation), inviata solo col consenso del cliente."""
    _csrf()
    req, reason = _resolve(token)
    if reason or req["status"] not in ("aperta", "in_corso"):
        return {"ok": False}, 400
    j = request.get_json(silent=True) or {}
    ok = services.client_geo(get_db(), req, j.get("lat"), j.get("lon"), j.get("acc"), **_ctx())
    return {"ok": ok}


@bp.post("/<token>/dispute")
def dispute(token):
    _csrf()
    req, reason = _resolve(token)
    if reason or req["status"] not in ("aperta", "in_corso", "inviata"):
        abort(400)
    motivo = (request.form.get("reason") or "").strip()
    if not motivo:
        flash("Indica quale dato non è corretto.", "error")
        return redirect(url_for("client.landing", token=token, r=1))
    services.client_dispute(get_db(), req, motivo, **_ctx())
    return render_template("client/disputed.html", req=req)


@bp.post("/<token>/accept")
def accept(token):
    _csrf()
    req, reason = _resolve(token)
    if reason:
        abort(400)
    if request.form.get("accept") != "yes":
        flash("Per procedere devi accettare la dichiarazione di sottoscrizione.", "error")
        return redirect(url_for("client.landing", token=token, r=1))
    if not services.client_accept_declaration(get_db(), req, **_ctx()):
        flash("Non è possibile accettare la dichiarazione in questo stato.", "error")
        return redirect(url_for("client.landing", token=token, r=1))
    req = services.get_request(get_db(), req["org_id"], req["id"])
    ok, msg = services.otp_send(get_db(), req, **_ctx())
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("client.landing", token=token, r=1) + "#otp")


@bp.post("/<token>/otp/send")
def otp_send(token):
    _csrf()
    if rate_limited("otp_send", limit=10):
        abort(429)
    req, reason = _resolve(token)
    if reason:
        abort(400)
    ok, msg = services.otp_send(get_db(), req, **_ctx())
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("client.landing", token=token, r=1) + "#otp")


@bp.post("/<token>/otp/verify")
def otp_verify(token):
    _csrf()
    if rate_limited("otp_verify", limit=30):
        abort(429)
    req, reason = _resolve(token)
    if reason:
        abort(400)
    ok, msg = services.otp_verify_and_confirm(get_db(), req, request.form.get("otp", ""), **_ctx())
    if ok:
        return redirect(url_for("client.done", token=token))
    flash(msg, "error")
    return redirect(url_for("client.landing", token=token, r=1) + "#otp")


@bp.route("/<token>/done")
def done(token):
    req, reason = _resolve(token)
    if reason:
        return render_template("client/unavailable.html", reason=reason, req=req), 410
    if req["status"] != "completata":
        return redirect(url_for("client.landing", token=token))
    return render_template("client/done.html", req=req, token=token, org=services.org_name(get_db(), req["org_id"]))
