"""Area operatore: elenco richieste, creazione, editor campi, anteprima, invio, dettaglio, download."""
import io
import json
import zipfile
from pathlib import Path

from flask import (Blueprint, render_template, request, redirect, url_for, g, flash, abort, send_file,
                   Response, jsonify, current_app)

from . import services, pdfops
from .db import get_db, transaction
from .routes_auth import login_required
from .utils import (valid_email, valid_fiscal_code, normalize_phone, valid_phone, client_ip, device_info,
                    expires_in, iso_utc, sha256_file, fmt_local)

bp = Blueprint("operator", __name__, url_prefix="/op")

FIELD_TYPES = pdfops.FIELD_TYPES


def _csrf():
    from . import check_csrf
    check_csrf()


def _req_or_404(req_id):
    req = services.get_request(get_db(), g.user["org_id"], req_id)
    if not req:
        abort(404)
    return services.check_expiry(get_db(), req)


@bp.before_request
@login_required
def _guard():
    pass


# ------------------------------------------------------------------ elenco

@bp.route("/requests")
def requests_list():
    conn = get_db()
    rows = services.list_requests(conn, g.user["org_id"])
    status = request.args.get("stato")
    if status:
        rows = [r for r in rows if r["status"] == status]
    counts = {}
    for r in services.list_requests(conn, g.user["org_id"]):
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    unread = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (g.user["id"],)).fetchone()[0]
    return render_template("op/requests_list.html", rows=rows, counts=counts, filter_status=status, unread=unread)


# ------------------------------------------------------------------ creazione

@bp.route("/requests/new", methods=["GET", "POST"])
def request_new():
    conn = get_db()
    if request.method == "POST":
        _csrf()
        f = request.files.get("pdf")
        if not f or not f.filename:
            flash("Seleziona un file PDF.", "error")
            return redirect(url_for("operator.request_new"))
        if not f.filename.lower().endswith(".pdf"):
            flash("Il file deve avere estensione .pdf.", "error")
            return redirect(url_for("operator.request_new"))
        data = f.read()
        try:
            with transaction(conn):
                doc_id, info = services.store_document(conn, g.user["org_id"], g.user["id"], f.filename, data)
        except pdfops.PdfError as e:
            flash(str(e), "error")
            return redirect(url_for("operator.request_new"))
        # campi compilabili presenti nel PDF → proposti come punto di partenza
        suggested = []
        for ff in info["form_fields"]:
            name = ff["name"].lower()
            ftype = None
            if ff["kind"] == "signature" or "firma" in name or "sign" in name:
                ftype = "signature"
            elif "cognome" in name or "surname" in name or "last" in name:
                ftype = "last_name"
            elif "nome" in name or "name" in name:
                ftype = "first_name"
            elif "fiscal" in name or "codice" in name or "cf" == name:
                ftype = "fiscal_code"
            if ftype and ftype not in [s["type"] for s in suggested]:
                suggested.append({"id": ftype, "type": ftype, "page": ff["page"], "x": ff["x"], "y": ff["y"],
                                  "w": ff["w"], "h": ff["h"], "widget_name": ff["name"]})
        title = (request.form.get("title") or f.filename.rsplit(".", 1)[0])[:120]
        req_id = services.create_request(conn, g.user["org_id"], g.user["id"], doc_id, suggested, title)
        doc = services.get_document(conn, g.user["org_id"], doc_id)
        with transaction(conn):
            services.add_event(conn, req_id, g.user["org_id"], "document_uploaded", f"operator:{g.user['id']}",
                               ip=client_ip(request), ua=device_info(request),
                               details={"document_id": doc_id, "sha256_originale": doc["sha256_original"],
                                        "pagine": info["page_count"],
                                        "campi_compilabili_trovati": len(info["form_fields"])})
        flash("PDF caricato. Posiziona ora i campi sul documento.", "ok")
        return redirect(url_for("operator.request_fields", req_id=req_id))
    templates = services.list_templates(conn, g.user["org_id"])
    return render_template("op/request_new.html", templates=templates, max_mb=current_app.config["MAX_PDF_MB"])


@bp.post("/templates/<tpl_id>/use")
def template_use(tpl_id):
    _csrf()
    conn = get_db()
    tpl = conn.execute("SELECT * FROM templates WHERE id=? AND org_id=?", (tpl_id, g.user["org_id"])).fetchone()
    if not tpl:
        abort(404)
    req_id = services.create_request(conn, g.user["org_id"], g.user["id"], tpl["document_id"],
                                     json.loads(tpl["fields_json"]), tpl["name"], from_template=True)
    flash("Richiesta creata dal modello. Verifica i campi e inserisci i dati del cliente.", "ok")
    return redirect(url_for("operator.request_fields", req_id=req_id))


# ------------------------------------------------------------------ editor campi

@bp.route("/requests/<req_id>/fields")
def request_fields(req_id):
    req = _req_or_404(req_id)
    if req["status"] != "bozza":
        return redirect(url_for("operator.request_detail", req_id=req_id))
    doc = services.get_document(get_db(), g.user["org_id"], req["document_id"])
    return render_template("op/request_fields.html", req=req, doc=doc, pages=json.loads(doc["pages_json"]),
                           fields=services.fields_of(req), field_types=FIELD_TYPES, step=1)


@bp.post("/requests/<req_id>/fields")
def request_fields_save(req_id):
    _csrf()
    req = _req_or_404(req_id)
    if req["status"] != "bozza":
        return jsonify({"ok": False, "error": "La richiesta non è modificabile."}), 400
    payload = request.get_json(silent=True) or {}
    doc = services.get_document(get_db(), g.user["org_id"], req["document_id"])
    fields = _clean_fields(payload.get("fields"), doc["page_count"])
    if fields is None:
        return jsonify({"ok": False, "error": "Campi non validi."}), 400
    conn = get_db()
    with transaction(conn):
        conn.execute("UPDATE requests SET fields_json=?, updated_at=? WHERE id=?", (json.dumps(fields), iso_utc(), req_id))
    if payload.get("save_template"):
        name = (payload.get("template_name") or f"Modello {doc['filename']}")[:120]
        with transaction(conn):
            services.save_template(conn, g.user["org_id"], g.user["id"], name, req["document_id"], fields)
    return jsonify({"ok": True, "next": url_for("operator.request_data", req_id=req_id)})


def _clean_fields(raw, page_count):
    if not isinstance(raw, list) or len(raw) > 20:
        return None
    out, seen = [], set()
    for f in raw:
        try:
            t = f["type"]
            if t not in FIELD_TYPES or t in seen:
                continue
            page = int(f["page"])
            x, y, w, h = (float(f[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            return None
        if not (0 <= page < page_count) or not (0 <= x < 1 and 0 <= y < 1 and 0.005 < w <= 1 and 0.005 < h <= 1):
            return None
        x, w = max(0, x), min(w, 1 - x)
        y, h = max(0, y), min(h, 1 - y)
        item = {"id": t, "type": t, "page": page, "x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}
        if f.get("widget_name"):
            item["widget_name"] = str(f["widget_name"])[:100]
        out.append(item)
        seen.add(t)
    return out


@bp.route("/documents/<doc_id>/page/<int:n>.png")
def document_page(doc_id, n):
    png = services.page_png(get_db(), g.user["org_id"], doc_id, n)
    if png is None:
        abort(404)
    return Response(png, mimetype="image/png")


# ------------------------------------------------------------------ dati cliente

@bp.route("/requests/<req_id>/data", methods=["GET", "POST"])
def request_data(req_id):
    req = _req_or_404(req_id)
    if req["status"] != "bozza":
        return redirect(url_for("operator.request_detail", req_id=req_id))
    errors = {}
    form = dict(request.form) if request.method == "POST" else {
        "title": req["title"], "message": req["message"], "first_name": req["client_first_name"],
        "last_name": req["client_last_name"], "fiscal_code": req["client_fiscal_code"],
        "email": req["client_email"], "phone": req["client_phone"], "channel": req["channel"],
        "signer_role": req["signer_role"], "place": req["place"],
        "expiry_days": str(current_app.config["LINK_DEFAULT_DAYS"]),
    }
    if request.method == "POST":
        _csrf()
        title = (form.get("title") or "").strip()[:120]
        first = (form.get("first_name") or "").strip()[:80]
        last = (form.get("last_name") or "").strip()[:80]
        cf = (form.get("fiscal_code") or "").strip().upper()
        email = (form.get("email") or "").strip().lower()
        phone = normalize_phone(form.get("phone") or "")
        channel = form.get("channel") or "email"
        message = (form.get("message") or "").strip()[:500]
        signer_role = (form.get("signer_role") or "Il Cliente").strip()[:40] or "Il Cliente"
        place = (form.get("place") or "").strip()[:80]
        try:
            days = max(1, min(int(form.get("expiry_days") or 7), current_app.config["LINK_MAX_DAYS"]))
        except ValueError:
            days = current_app.config["LINK_DEFAULT_DAYS"]
        if not title:
            errors["title"] = "Inserisci un titolo."
        if not first:
            errors["first_name"] = "Inserisci il nome."
        if not last:
            errors["last_name"] = "Inserisci il cognome."
        if not valid_fiscal_code(cf):
            errors["fiscal_code"] = "Codice fiscale non valido (controllo formale: 16 caratteri e carattere di controllo)."
        if email and not valid_email(email):
            errors["email"] = "Email non valida."
        if phone and not valid_phone(phone):
            errors["phone"] = "Numero non valido (usa il prefisso internazionale, es. +39...)."
        if channel not in ("email", "whatsapp"):
            errors["channel"] = "Canale non valido."
        if channel == "email" and not email:
            errors["email"] = "Per il canale email serve l'indirizzo email."
        if not phone:
            errors["phone"] = "Il numero di cellulare è obbligatorio: l'OTP viene inviato via WhatsApp."
        if not errors:
            conn = get_db()
            with transaction(conn):
                conn.execute(
                    "UPDATE requests SET title=?, message=?, client_first_name=?, client_last_name=?, client_fiscal_code=?, "
                    "client_email=?, client_phone=?, channel=?, signer_role=?, place=?, expires_at=?, updated_at=? WHERE id=?",
                    (title, message, first, last, cf, email, phone, channel, signer_role, place, expires_in(days),
                     iso_utc(), req_id))
            return redirect(url_for("operator.request_preview", req_id=req_id))
        form.update({"fiscal_code": cf, "phone": phone, "email": email})
    return render_template("op/request_data.html", req=req, form=form, errors=errors, step=2,
                           max_days=current_app.config["LINK_MAX_DAYS"])


# ------------------------------------------------------------------ anteprima e invio

def _preview_path(req):
    doc = services.get_document(get_db(), g.user["org_id"], req["document_id"])
    folder = Path(current_app.config["RENDER_DIR"]) / g.user["org_id"] / "preview" / req["id"]
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / "anteprima.pdf"
    pdfops.fill_document(doc["path_original"], str(p), services.fields_of(req), services.values_of(req),
                         demo=current_app.config["DEMO"], final=False)
    return p, doc


@bp.route("/requests/<req_id>/preview")
def request_preview(req_id):
    req = _req_or_404(req_id)
    if req["status"] != "bozza":
        return redirect(url_for("operator.request_detail", req_id=req_id))
    p, doc = _preview_path(req)
    fields = services.fields_of(req)
    problems = []
    if not any(f["type"] == "signature" for f in fields):
        problems.append("Manca il campo firma.")
    if not (req["client_first_name"] and req["client_last_name"] and req["client_fiscal_code"]):
        problems.append("Dati del cliente incompleti.")
    if req["channel"] == "email" and not req["client_email"]:
        problems.append("Email mancante.")
    if not req["client_phone"]:
        problems.append("Cellulare mancante (serve per l'OTP via WhatsApp).")
    return render_template("op/request_preview.html", req=req, doc=doc, page_count=doc["page_count"],
                           problems=problems, step=3)


@bp.route("/requests/<req_id>/preview/page/<int:n>.png")
def request_preview_page(req_id, n):
    req = _req_or_404(req_id)
    folder = Path(current_app.config["RENDER_DIR"]) / g.user["org_id"] / "preview" / req["id"]
    p = folder / "anteprima.pdf"
    if not p.exists():
        p, _ = _preview_path(req)
    try:
        return Response(pdfops.render_page_png(str(p), n, 900), mimetype="image/png")
    except Exception:
        abort(404)


@bp.post("/requests/<req_id>/send")
def request_send(req_id):
    _csrf()
    req = _req_or_404(req_id)
    ok, msg = services.send_request(get_db(), req, g.user, ip=client_ip(request), ua=device_info(request))
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("operator.request_detail", req_id=req_id))


# ------------------------------------------------------------------ dettaglio

@bp.route("/requests/<req_id>")
def request_detail(req_id):
    conn = get_db()
    req = _req_or_404(req_id)
    doc = services.get_document(conn, g.user["org_id"], req["document_id"]) if req["document_id"] else None
    events = services.events_for(conn, req_id)
    outbox = conn.execute("SELECT * FROM outbox WHERE request_id=? ORDER BY id DESC", (req_id,)).fetchall()
    demo_otp = None
    if current_app.config["DEMO"]:
        o = conn.execute("SELECT demo_code, expires_at, used_at, invalidated FROM otps WHERE request_id=? "
                         "ORDER BY id DESC LIMIT 1", (req_id,)).fetchone()
        if o and not o["used_at"] and not o["invalidated"]:
            demo_otp = o
    chain_ok, n_events = services.verify_chain(conn, req_id)
    with transaction(conn):
        conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=? AND request_id=?", (g.user["id"], req_id))
    return render_template("op/request_detail.html", req=req, doc=doc, events=events, outbox=outbox,
                           demo_otp=demo_otp, chain_ok=chain_ok, n_events=n_events,
                           details=lambda e: json.loads(e["details_json"] or "{}"),
                           client_url=(f"{services.base_url()}/c/{req['demo_link_token']}"
                                       if req["demo_link_token"] else None))


@bp.post("/requests/<req_id>/revoke")
def request_revoke(req_id):
    _csrf()
    req = _req_or_404(req_id)
    if services.revoke_request(get_db(), req, g.user):
        flash("Richiesta revocata: il link non è più utilizzabile.", "ok")
    else:
        flash("La richiesta non può essere revocata in questo stato.", "error")
    return redirect(url_for("operator.request_detail", req_id=req_id))


@bp.post("/requests/<req_id>/retry")
def request_retry(req_id):
    _csrf()
    req = _req_or_404(req_id)
    ok, msg = services.retry_finalize(get_db(), req, g.user)
    flash("Finalizzazione completata." if ok else msg, "ok" if ok else "error")
    return redirect(url_for("operator.request_detail", req_id=req_id))


@bp.post("/requests/<req_id>/open-as-client")
def request_open_as_client(req_id):
    """Solo in demo: apre il link cliente dalla console operatore per mostrare il percorso."""
    _csrf()
    if not current_app.config["DEMO"]:
        abort(404)
    req = _req_or_404(req_id)
    if not req["demo_link_token"]:
        flash("Link cliente non disponibile per questa richiesta.", "error")
        return redirect(url_for("operator.request_detail", req_id=req_id))
    return redirect(url_for("client.landing", token=req["demo_link_token"]))


# ------------------------------------------------------------------ download (autorizzati: stessa organizzazione)

@bp.route("/requests/<req_id>/download/<which>")
def request_download(req_id, which):
    req = _req_or_404(req_id)
    doc = services.get_document(get_db(), g.user["org_id"], req["document_id"])
    files = {
        "originale": (doc["path_original"] if doc else None, f"{req_id}_originale.pdf"),
        "compilato": (req["path_filled"], f"{req_id}_compilato.pdf"),
        "finale": (req["path_final"], f"{req_id}_finale.pdf"),
        "audit": (req["path_audit"], f"{req_id}_audit_trail.pdf"),
    }
    if which == "zip":
        if req["status"] != "completata":
            abort(404)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for key in ("finale", "audit"):
                path, name = files[key]
                z.write(path, name)
            z.writestr(f"{req_id}_impronte_sha256.txt",
                       f"{req_id}_finale.pdf  {req['sha256_final']}\n{req_id}_audit_trail.pdf  {req['sha256_audit']}\n"
                       f"{req_id}_compilato.pdf  {req['sha256_filled']}\n")
        buf.seek(0)
        return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=f"{req_id}_firmato.zip")
    if which not in files or not files[which][0] or not Path(files[which][0]).exists():
        abort(404)
    return send_file(files[which][0], mimetype="application/pdf", as_attachment=True, download_name=files[which][1])


# ------------------------------------------------------------------ modelli, notifiche, comunicazioni

@bp.route("/templates")
def templates_list():
    return render_template("op/templates.html", templates=services.list_templates(get_db(), g.user["org_id"]))


@bp.route("/notifications")
def notifications():
    conn = get_db()
    rows = conn.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 100", (g.user["id"],)).fetchall()
    with transaction(conn):
        conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (g.user["id"],))
    return render_template("op/notifications.html", rows=rows)


@bp.route("/outbox")
def outbox():
    rows = get_db().execute("SELECT * FROM outbox WHERE org_id=? ORDER BY id DESC LIMIT 200", (g.user["org_id"],)).fetchall()
    return render_template("op/outbox.html", rows=rows)


@bp.app_context_processor
def _unread():
    if g.get("user"):
        n = get_db().execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (g.user["id"],)).fetchone()[0]
        return {"unread_count": n}
    return {"unread_count": 0}
