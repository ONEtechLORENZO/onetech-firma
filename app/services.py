"""Logica applicativa: ciclo di vita della richiesta, eventi, OTP, documenti, notifiche."""
import hashlib
import hmac
import json
import secrets
from pathlib import Path

from flask import current_app
from werkzeug.security import generate_password_hash

from . import pdfops
from .db import get_db, transaction
from .providers import send_email, send_whatsapp_template, SendResult
from datetime import timezone

from .utils import (new_id, iso_utc, now_utc, parse_iso, sha256_file, hash_token, mask_email,
                    mask_phone, random_otp, fmt_local, expires_in, TZ)

STATUS_LABELS = {
    "bozza": "Bozza",
    "inviata": "Inviata",
    "aperta": "Aperta",
    "in_corso": "In corso",
    "completata": "Completata",
    "scaduta": "Scaduta",
    "revocata": "Revocata",
    "errore": "Errore",
}
ACTIVE_STATUSES = ("inviata", "aperta", "in_corso")

EVENT_LABELS = {
    "request_created": "Richiesta creata",
    "document_uploaded": "Documento caricato",
    "fields_defined": "Campi posizionati",
    "document_filled": "Documento compilato generato e congelato",
    "link_sent": "Invio del link al cliente",
    "link_delivered": "Link consegnato (riscontro provider)",
    "link_opened": "Link aperto dal cliente",
    "document_presented": "Documento presentato al cliente",
    "document_downloaded": "Documento scaricato dal cliente",
    "data_disputed": "Dati segnalati come errati dal cliente",
    "declaration_accepted": "Dichiarazione di sottoscrizione accettata",
    "otp_sent": "Invio OTP",
    "otp_failed": "OTP errato",
    "otp_rejected": "OTP rifiutato",
    "otp_verified": "OTP verificato",
    "final_generated": "Documento finale generato",
    "audit_generated": "Audit Trail generato",
    "completed": "Richiesta completata",
    "expired": "Link scaduto",
    "revoked": "Richiesta revocata dall'operatore",
    "geo_recorded": "Geolocalizzazione fornita dal cliente",
    "error": "Errore di sistema",
}

# Codici "Azione" usati nella tabella dell'Audit Trail
EVENT_ACTIONS = {
    "request_created": "RichiestaCreata",
    "document_uploaded": "DocumentoCaricato",
    "fields_defined": "CampiPosizionati",
    "document_filled": "DocumentoCompilato",
    "link_sent": "LinkInviato",
    "link_delivered": "LinkConsegnato",
    "link_opened": "LinkAperto",
    "document_presented": "DocumentoPresentato",
    "document_downloaded": "DocumentoScaricato",
    "data_disputed": "DatiContestati",
    "declaration_accepted": "DichiarazioneAccettata",
    "otp_sent": "OtpInviato",
    "otp_failed": "OtpErrato",
    "otp_rejected": "OtpRifiutato",
    "otp_verified": "OtpVerificato",
    "final_generated": "DocumentoFirmato",
    "audit_generated": "AuditTrailGenerato",
    "completed": "RichiestaCompletata",
    "expired": "LinkScaduto",
    "revoked": "RichiestaRevocata",
    "geo_recorded": "GeolocalizzazioneRilevata",
    "error": "Errore",
}

GENESIS = "0" * 64


def cfg(key):
    return current_app.config[key]


# ------------------------------------------------------------------ eventi (catena di hash)

def _event_hash(prev_hash, request_id, etype, ts, actor, ip, ua, details_json):
    payload = "|".join([prev_hash, request_id, etype, ts, actor, ip or "", ua or "", details_json])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def add_event(conn, request_id, org_id, etype, actor, *, ip=None, ua=None, details=None):
    """Aggiunge un evento in coda alla catena. Da chiamare dentro una transazione."""
    last = conn.execute("SELECT hash FROM events WHERE request_id=? ORDER BY id DESC LIMIT 1",
                        (request_id,)).fetchone()
    prev = last["hash"] if last else GENESIS
    ts = iso_utc()
    details_json = json.dumps(details or {}, ensure_ascii=False, sort_keys=True)
    h = _event_hash(prev, request_id, etype, ts, actor, ip, ua, details_json)
    conn.execute(
        "INSERT INTO events(request_id, org_id, type, ts_utc, actor, ip, user_agent, details_json, prev_hash, hash)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (request_id, org_id, etype, ts, actor, ip, ua, details_json, prev, h))
    return h


def verify_chain(conn, request_id) -> tuple[bool, int]:
    """Ricalcola la catena: True se nessun evento è stato alterato."""
    rows = conn.execute("SELECT * FROM events WHERE request_id=? ORDER BY id", (request_id,)).fetchall()
    prev = GENESIS
    for r in rows:
        if r["prev_hash"] != prev:
            return False, len(rows)
        h = _event_hash(prev, r["request_id"], r["type"], r["ts_utc"], r["actor"], r["ip"], r["user_agent"],
                        r["details_json"])
        if h != r["hash"]:
            return False, len(rows)
        prev = h
    return True, len(rows)


def events_for(conn, request_id):
    return conn.execute("SELECT * FROM events WHERE request_id=? ORDER BY id", (request_id,)).fetchall()


# ------------------------------------------------------------------ outbox e notifiche

def _outbox(conn, org_id, request_id, channel, purpose, recipient_masked, subject, body, result: SendResult):
    conn.execute(
        "INSERT INTO outbox(org_id, request_id, channel, purpose, recipient_masked, subject, body, mode, status,"
        " provider_ref, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (org_id, request_id, channel, purpose, recipient_masked, subject, body,
         "demo" if cfg("DEMO") else "real", result.status, result.provider_ref, result.error, iso_utc()))


def notify(conn, org_id, user_id, request_id, text):
    conn.execute("INSERT INTO notifications(org_id, user_id, request_id, text, created_at) VALUES (?,?,?,?,?)",
                 (org_id, user_id, request_id, text, iso_utc()))


# ------------------------------------------------------------------ seed

def ensure_seed(app):
    """Crea organizzazione, operatore demo e un documento fittizio di esempio se mancano."""
    import sqlite3
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.row_factory = sqlite3.Row
    try:
        org = conn.execute("SELECT * FROM orgs LIMIT 1").fetchone()
        if org:
            return
        org_id = new_id("ORG", 6)
        user_id = new_id("USR", 6)
        conn.execute("INSERT INTO orgs(id, name, created_at) VALUES (?,?,?)",
                     (org_id, "Banca Esempio S.p.A. (fittizia)", iso_utc()))
        conn.execute("INSERT INTO users(id, org_id, email, name, password_hash, created_at) VALUES (?,?,?,?,?,?)",
                     (user_id, org_id, "operatore@demo.local", "Maria Rossi (operatore demo)",
                      generate_password_hash("demo1234"), iso_utc()))
        # seconda organizzazione: serve a dimostrare l'isolamento dei dati
        org2 = new_id("ORG", 6)
        conn.execute("INSERT INTO orgs(id, name, created_at) VALUES (?,?,?)",
                     (org2, "Altra Banca S.p.A. (fittizia)", iso_utc()))
        conn.execute("INSERT INTO users(id, org_id, email, name, password_hash, created_at) VALUES (?,?,?,?,?,?)",
                     (new_id("USR", 6), org2, "altro@demo.local", "Luca Bianchi (altra banca)",
                      generate_password_hash("demo1234"), iso_utc()))
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ documenti

def store_document(conn, org_id, user_id, filename: str, data: bytes):
    info = pdfops.validate_and_inspect(data, cfg("MAX_PDF_MB"), cfg("MAX_PAGES"))
    doc_id = new_id("DOC")
    folder = Path(cfg("FILES_DIR")) / org_id / doc_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "originale.pdf"
    path.write_bytes(data)
    sha = sha256_file(path)
    conn.execute(
        "INSERT INTO documents(id, org_id, user_id, filename, path_original, sha256_original, size_bytes, page_count,"
        " pages_json, form_fields_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (doc_id, org_id, user_id, filename[:200], str(path), sha, len(data), info["page_count"],
         json.dumps(info["pages"]), json.dumps(info["form_fields"]), iso_utc()))
    return doc_id, info


def get_document(conn, org_id, doc_id):
    return conn.execute("SELECT * FROM documents WHERE id=? AND org_id=?", (doc_id, org_id)).fetchone()


def page_png(conn, org_id, doc_id, page_index: int) -> bytes | None:
    doc = get_document(conn, org_id, doc_id)
    if not doc or page_index < 0 or page_index >= doc["page_count"]:
        return None
    cache = Path(cfg("RENDER_DIR")) / org_id / doc_id / f"p{page_index}.png"
    if cache.exists():
        return cache.read_bytes()
    cache.parent.mkdir(parents=True, exist_ok=True)
    png = pdfops.render_page_png(doc["path_original"], page_index)
    cache.write_bytes(png)
    return png


# ------------------------------------------------------------------ richieste

def get_request(conn, org_id, req_id):
    return conn.execute("SELECT * FROM requests WHERE id=? AND org_id=?", (req_id, org_id)).fetchone()


def create_request(conn, org_id, user_id, document_id=None, fields=None, title="", from_template=False):
    req_id = new_id("REQ")
    ts = iso_utc()
    with transaction(conn):
        conn.execute(
            "INSERT INTO requests(id, org_id, user_id, title, document_id, fields_json, expires_at, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (req_id, org_id, user_id, title, document_id, json.dumps(fields or []),
             expires_in(cfg("LINK_DEFAULT_DAYS")), ts, ts))
        add_event(conn, req_id, org_id, "request_created", f"operator:{user_id}")
        if document_id and from_template:
            doc = get_document(conn, org_id, document_id)
            add_event(conn, req_id, org_id, "document_uploaded", f"operator:{user_id}",
                      details={"document_id": document_id, "da_modello": True,
                               "sha256_originale": doc["sha256_original"] if doc else ""})
    return req_id


def touch(conn, req_id):
    conn.execute("UPDATE requests SET updated_at=? WHERE id=?", (iso_utc(), req_id))


def check_expiry(conn, req) -> object:
    """Se il link è scaduto, aggiorna lo stato (solo per richieste attive)."""
    if req["status"] in ACTIVE_STATUSES and req["expires_at"] and parse_iso(req["expires_at"]) < now_utc():
        with transaction(conn):
            cur = conn.execute("UPDATE requests SET status='scaduta', updated_at=? WHERE id=? AND status IN (?,?,?)",
                               (iso_utc(), req["id"], *ACTIVE_STATUSES))
            if cur.rowcount:
                add_event(conn, req["id"], req["org_id"], "expired", "system",
                          details={"scadenza": req["expires_at"]})
        req = conn.execute("SELECT * FROM requests WHERE id=?", (req["id"],)).fetchone()
    return req


def list_requests(conn, org_id):
    rows = conn.execute("SELECT * FROM requests WHERE org_id=? ORDER BY created_at DESC", (org_id,)).fetchall()
    return [check_expiry(conn, r) for r in rows]


def fields_of(req):
    try:
        return json.loads(req["fields_json"] or "[]")
    except Exception:
        return []


def values_of(req):
    return {"first_name": req["client_first_name"], "last_name": req["client_last_name"],
            "full_name": f"{req['client_first_name']} {req['client_last_name']}".strip(),
            "fiscal_code": req["client_fiscal_code"]}


def device_id(ip, ua) -> str | None:
    """Identificativo del dispositivo derivato da user-agent e IP (non contiene dati in chiaro)."""
    if not ua and not ip:
        return None
    return "dev-" + hashlib.sha256(f"{ua or ''}|{ip or ''}".encode()).hexdigest()[:20]


def _client_details(req, extra=None):
    d = dict(extra or {})
    if req["geo_json"]:
        try:
            g = json.loads(req["geo_json"])
            d["geo"] = f"{g['lat']:.4f}/{g['lon']:.4f} ±{int(g.get('acc', 0))}m"
        except Exception:
            pass
    return d


def declaration_text(req, sha_filled):
    return (f"Dichiaro di aver visualizzato e letto il documento «{req['title']}» "
            f"(impronta SHA-256 {sha_filled[:16]}…), di confermare che i dati riportati — "
            f"{req['client_first_name']} {req['client_last_name']}, codice fiscale {req['client_fiscal_code']} — "
            f"sono corretti e di volerlo sottoscrivere mediante conferma con il codice OTP inviato via WhatsApp al numero indicato.")


def _recipient(req):
    """Recapito del LINK (email o WhatsApp)."""
    if req["channel"] == "whatsapp":
        return req["client_phone"], mask_phone(req["client_phone"])
    return req["client_email"], mask_email(req["client_email"])


def _otp_recipient(req):
    """L'OTP viaggia sempre su WhatsApp."""
    return req["client_phone"], mask_phone(req["client_phone"])


def base_url() -> str:
    """BASE_URL da configurazione; altrimenti l'indirizzo con cui l'operatore sta usando l'app."""
    if cfg("BASE_URL"):
        return cfg("BASE_URL").rstrip("/")
    from flask import has_request_context, request
    if has_request_context():
        return request.url_root.rstrip("/")
    return "http://localhost:5050"


def _client_url(token):
    return f"{base_url()}/c/{token}"


def send_request(conn, req, user, *, ip=None, ua=None) -> tuple[bool, str]:
    """Congela il documento compilato, genera il link e invia (simulato o reale)."""
    if req["status"] != "bozza":
        return False, "La richiesta non è in bozza."
    doc = get_document(conn, req["org_id"], req["document_id"])
    if not doc:
        return False, "Documento mancante."
    fields = fields_of(req)
    if not any(f["type"] == "signature" for f in fields):
        return False, "Posiziona almeno il campo firma."
    if not (req["client_first_name"] and req["client_last_name"] and req["client_fiscal_code"]):
        return False, "Dati del cliente incompleti."
    addr, masked = _recipient(req)
    if not addr:
        return False, "Recapito mancante per il canale scelto."
    if not req["client_phone"]:
        return False, "Numero WhatsApp mancante: serve per l'OTP."

    # 1) documento compilato (congelato) esattamente come verrà presentato al cliente
    folder = Path(doc["path_original"]).parent.parent / req["id"]
    folder.mkdir(parents=True, exist_ok=True)
    path_filled = folder / "compilato.pdf"
    pdfops.fill_document(doc["path_original"], str(path_filled), fields, values_of(req),
                         demo=cfg("DEMO"), final=False)
    sha_filled = sha256_file(path_filled)

    # 2) link casuale: nel DB solo l'HMAC
    token = secrets.token_urlsafe(32)
    token_hash = hash_token(token, current_app.secret_key)
    url = _client_url(token)
    decl = declaration_text(req, sha_filled)

    subject = f"{req['title']} — documento da confermare"
    body_public = (f"Gentile {req['client_first_name']} {req['client_last_name']},\n"
                   f"{user['name']} ({org_name(conn, req['org_id'])}) le ha inviato il documento "
                   f"«{req['title']}» da visualizzare e confermare.\n"
                   + (f"Messaggio: {req['message']}\n" if req['message'] else "")
                   + f"Il collegamento scade il {fmt_local(req['expires_at'], False)}.\n"
                   f"Collegamento: {url if cfg('DEMO') else '[collegamento riservato]'}")

    if req["channel"] == "whatsapp":
        result = send_whatsapp_template(current_app.config, addr, "link",
                                        [req["client_first_name"], req["title"]], button_param=token,
                                        text=body_public.replace("[collegamento riservato]", url))
    else:
        result = send_email(current_app.config, addr, subject,
                            body_public.replace("[collegamento riservato]", url))

    with transaction(conn):
        cur = conn.execute("SELECT status FROM requests WHERE id=?", (req["id"],)).fetchone()
        if cur["status"] != "bozza":
            return False, "La richiesta è già stata inviata."
        ok = result.status in ("simulato", "inviato")
        conn.execute(
            "UPDATE requests SET path_filled=?, sha256_filled=?, link_token_hash=?, demo_link_token=?, "
            "declaration_text=?, status=?, sent_at=?, last_error=?, updated_at=? WHERE id=?",
            (str(path_filled), sha_filled, token_hash, token if cfg("DEMO") else None, decl,
             "inviata" if ok else "errore", iso_utc() if ok else None, result.error, iso_utc(), req["id"]))
        add_event(conn, req["id"], req["org_id"], "fields_defined", f"operator:{user['id']}", ip=ip, ua=ua,
                  details={"campi": [f["type"] for f in fields]})
        add_event(conn, req["id"], req["org_id"], "document_filled", f"operator:{user['id']}", ip=ip, ua=ua,
                  details={"sha256_compilato": sha_filled, "sha256_originale": doc["sha256_original"]})
        add_event(conn, req["id"], req["org_id"], "link_sent" if ok else "error", f"operator:{user['id']}",
                  ip=ip, ua=ua,
                  details={"canale": req["channel"], "destinatario": masked,
                           "esito": result.status, "scadenza_link": req["expires_at"],
                           **({"errore": result.error} if result.error else {})})
        _outbox(conn, req["org_id"], req["id"], req["channel"], "link", masked, subject, body_public, result)
    if ok:
        return True, "Richiesta inviata." if not cfg("DEMO") else "Invio simulato: nessun messaggio reale è partito."
    return False, f"Invio non riuscito: {result.error}"


def org_name(conn, org_id):
    r = conn.execute("SELECT name FROM orgs WHERE id=?", (org_id,)).fetchone()
    return r["name"] if r else ""


def revoke_request(conn, req, user):
    if req["status"] not in ACTIVE_STATUSES + ("bozza", "errore"):
        return False
    with transaction(conn):
        conn.execute("UPDATE requests SET status='revocata', link_revoked=1, demo_link_token=NULL, updated_at=? WHERE id=?",
                     (iso_utc(), req["id"]))
        add_event(conn, req["id"], req["org_id"], "revoked", f"operator:{user['id']}")
    return True


# ------------------------------------------------------------------ cliente

def request_by_token(conn, token: str):
    """Risolve il link cliente. Restituisce (richiesta, motivo_rifiuto)."""
    if not token or len(token) < 20 or len(token) > 128:
        return None, "link_non_valido"
    h = hash_token(token, current_app.secret_key)
    req = conn.execute("SELECT * FROM requests WHERE link_token_hash=?", (h,)).fetchone()
    if not req:
        return None, "link_non_valido"
    req = check_expiry(conn, req)
    if req["link_revoked"] or req["status"] == "revocata":
        return req, "revocato"
    if req["status"] == "scaduta":
        return req, "scaduto"
    if req["status"] == "errore":
        return req, "errore"
    return req, None


def client_open(conn, req, *, ip, ua):
    """Prima apertura del link: inviata → aperta. Registra sempre l'apertura (non la lettura)."""
    with transaction(conn):
        if req["status"] == "inviata":
            conn.execute("UPDATE requests SET status='aperta', opened_at=?, updated_at=? WHERE id=? AND status='inviata'",
                         (iso_utc(), iso_utc(), req["id"]))
        add_event(conn, req["id"], req["org_id"], "link_opened", "client", ip=ip, ua=ua, details=_client_details(req))
    return conn.execute("SELECT * FROM requests WHERE id=?", (req["id"],)).fetchone()


def client_presented(conn, req, *, ip, ua):
    with transaction(conn):
        add_event(conn, req["id"], req["org_id"], "document_presented", "client", ip=ip, ua=ua,
                  details=_client_details(req, {"sha256_compilato": req["sha256_filled"], "pagina": "tutte"}))


def client_download(conn, req, which, *, ip, ua):
    with transaction(conn):
        add_event(conn, req["id"], req["org_id"], "document_downloaded", "client", ip=ip, ua=ua,
                  details=_client_details(req, {"file": which}))


def client_dispute(conn, req, reason, *, ip, ua):
    with transaction(conn):
        conn.execute("UPDATE requests SET status='errore', dispute_reason=?, last_error=?, updated_at=? WHERE id=?",
                     (reason[:500], "Il cliente ha segnalato dati errati.", iso_utc(), req["id"]))
        add_event(conn, req["id"], req["org_id"], "data_disputed", "client", ip=ip, ua=ua,
                  details=_client_details(req, {"motivo": reason[:500]}))
        notify(conn, req["org_id"], req["user_id"], req["id"],
               f"Il cliente ha segnalato dati errati nella richiesta {req['id']}: «{reason[:120]}»")


def client_geo(conn, req, lat, lon, acc, *, ip, ua):
    """Posizione fornita volontariamente dal browser del cliente. Registrata solo se effettivamente ricevuta."""
    try:
        lat, lon, acc = float(lat), float(lon), float(acc or 0)
    except (TypeError, ValueError):
        return False
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False
    geo = {"lat": round(lat, 5), "lon": round(lon, 5), "acc": int(acc)}
    with transaction(conn):
        conn.execute("UPDATE requests SET geo_json=?, updated_at=? WHERE id=?", (json.dumps(geo), iso_utc(), req["id"]))
        req2 = conn.execute("SELECT * FROM requests WHERE id=?", (req["id"],)).fetchone()
        add_event(conn, req["id"], req["org_id"], "geo_recorded", "client", ip=ip, ua=ua, details=_client_details(req2))
    return True


def client_accept_declaration(conn, req, *, ip, ua):
    with transaction(conn):
        if req["status"] not in ("aperta", "in_corso"):
            return False
        conn.execute("UPDATE requests SET status='in_corso', declaration_accepted_at=?, updated_at=? WHERE id=?",
                     (iso_utc(), iso_utc(), req["id"]))
        add_event(conn, req["id"], req["org_id"], "declaration_accepted", "client", ip=ip, ua=ua,
                  details=_client_details(req, {"sha256_dichiarazione": hashlib.sha256(req["declaration_text"].encode()).hexdigest()}))
    return True


def _otp_hash(code: str) -> str:
    return hmac.new(current_app.secret_key.encode(), code.encode(), hashlib.sha256).hexdigest()


def otp_send(conn, req, *, ip, ua) -> tuple[bool, str]:
    """Genera e invia un nuovo OTP. Limita i reinvii e invalida i codici precedenti."""
    if req["status"] != "in_corso" or not req["declaration_accepted_at"]:
        return False, "Accetta prima la dichiarazione di sottoscrizione."
    rows = conn.execute("SELECT created_at FROM otps WHERE request_id=? ORDER BY id DESC", (req["id"],)).fetchall()
    if len(rows) >= cfg("OTP_MAX_RESENDS") + 1:
        return False, "Numero massimo di invii OTP raggiunto. Contatta l'operatore."
    if rows:
        elapsed = (now_utc() - parse_iso(rows[0]["created_at"])).total_seconds()
        if elapsed < cfg("OTP_RESEND_COOLDOWN_SECONDS"):
            return False, f"Attendi {int(cfg('OTP_RESEND_COOLDOWN_SECONDS') - elapsed)} secondi prima di richiedere un nuovo codice."

    code = random_otp(cfg("OTP_LENGTH"))
    addr, masked = _otp_recipient(req)
    if not addr:
        return False, "Numero WhatsApp mancante."
    text_public = (f"[WhatsApp] Codice di conferma per «{req['title']}»: ******. "
                   f"Valido {cfg('OTP_TTL_SECONDS') // 60} minuti, uso singolo. (codice non archiviato nei messaggi)")
    result = send_whatsapp_template(current_app.config, addr, "otp", [code], button_param=code,
                                    text=f"Codice di conferma per «{req['title']}»: {code}. "
                                         f"Valido {cfg('OTP_TTL_SECONDS') // 60} minuti, uso singolo.")
    ok = result.status in ("simulato", "inviato")
    with transaction(conn):
        conn.execute("UPDATE otps SET invalidated=1 WHERE request_id=? AND used_at IS NULL", (req["id"],))
        if ok:
            from datetime import timedelta
            conn.execute(
                "INSERT INTO otps(request_id, code_hash, demo_code, created_at, expires_at) VALUES (?,?,?,?,?)",
                (req["id"], _otp_hash(code), code if cfg("DEMO") else None, iso_utc(),
                 iso_utc(now_utc() + timedelta(seconds=cfg("OTP_TTL_SECONDS")))))
        from datetime import timedelta as _td
        add_event(conn, req["id"], req["org_id"], "otp_sent" if ok else "error", "client", ip=ip, ua=ua,
                  details=_client_details(req, {"canale": "whatsapp", "destinatario": masked, "esito": result.status,
                           "invio_n": len(rows) + 1, "id_transazione": f"OTP-{req['id'][-4:]}-{len(rows) + 1}",
                           "scadenza": iso_utc(now_utc() + _td(seconds=cfg("OTP_TTL_SECONDS"))),
                           **({"errore": result.error} if result.error else {})}))
        _outbox(conn, req["org_id"], req["id"], "whatsapp", "otp", masked, "Codice di conferma", text_public, result)
    if ok:
        return True, "Codice inviato." if not cfg("DEMO") else "Invio OTP simulato (vedi console operatore)."
    return False, f"Invio OTP non riuscito: {result.error}"


def otp_verify_and_confirm(conn, req, code: str, *, ip, ua) -> tuple[bool, str]:
    """Verifica l'OTP in transazione esclusiva, poi finalizza. Idempotente contro doppie conferme."""
    code = (code or "").strip()
    with transaction(conn):
        cur = conn.execute("SELECT * FROM requests WHERE id=?", (req["id"],)).fetchone()
        if cur["status"] == "completata":
            return True, "già_completata"
        if cur["status"] != "in_corso":
            return False, "La richiesta non è in uno stato che consente la conferma."
        if cur["otp_verified_at"]:
            # OTP già verificato: la finalizzazione è in corso o fallita. Nessuna seconda verifica.
            return False, "Conferma già registrata. Attendi il completamento."
        otp = conn.execute("SELECT * FROM otps WHERE request_id=? ORDER BY id DESC LIMIT 1", (req["id"],)).fetchone()
        if not otp or otp["invalidated"] or otp["used_at"]:
            add_event(conn, req["id"], req["org_id"], "otp_rejected", "client", ip=ip, ua=ua,
                      details=_client_details(req, {"motivo": "nessun codice valido / codice già utilizzato"}))
            return False, "Nessun codice valido. Richiedi un nuovo codice."
        if parse_iso(otp["expires_at"]) < now_utc():
            conn.execute("UPDATE otps SET invalidated=1 WHERE id=?", (otp["id"],))
            add_event(conn, req["id"], req["org_id"], "otp_rejected", "client", ip=ip, ua=ua,
                      details=_client_details(req, {"motivo": "codice scaduto"}))
            return False, "Codice scaduto. Richiedi un nuovo codice."
        if otp["attempts"] >= cfg("OTP_MAX_ATTEMPTS"):
            conn.execute("UPDATE otps SET invalidated=1 WHERE id=?", (otp["id"],))
            add_event(conn, req["id"], req["org_id"], "otp_rejected", "client", ip=ip, ua=ua,
                      details=_client_details(req, {"motivo": "tentativi esauriti"}))
            return False, "Tentativi esauriti. Richiedi un nuovo codice."
        if not (len(code) == cfg("OTP_LENGTH") and hmac.compare_digest(_otp_hash(code), otp["code_hash"])):
            conn.execute("UPDATE otps SET attempts=attempts+1 WHERE id=?", (otp["id"],))
            left = cfg("OTP_MAX_ATTEMPTS") - otp["attempts"] - 1
            add_event(conn, req["id"], req["org_id"], "otp_failed", "client", ip=ip, ua=ua,
                      details=_client_details(req, {"tentativo": otp["attempts"] + 1, "rimasti": left}))
            return False, f"Codice errato. Tentativi rimasti: {left}." if left > 0 else "Codice errato. Tentativi esauriti: richiedi un nuovo codice."
        ts = iso_utc()
        conn.execute("UPDATE otps SET used_at=?, attempts=attempts+1 WHERE id=?", (ts, otp["id"]))
        conn.execute("UPDATE requests SET otp_verified_at=?, signed_at=?, updated_at=? WHERE id=?",
                     (ts, ts, ts, req["id"]))
        add_event(conn, req["id"], req["org_id"], "otp_verified", "client", ip=ip, ua=ua,
                  details=_client_details(req, {"esito": "verificato", "tentativo": otp["attempts"] + 1, "invio_n": otp["id"]}))
    ok, msg = finalize_request(conn, req["id"])
    return ok, ("completata" if ok else msg)


# ------------------------------------------------------------------ finalizzazione

def _audit_event_rows(conn, req):
    """Righe per la tabella dell'Audit Trail: Data e ora | Azione | Descrizione | Firmatario | IP | Geo."""
    signer_name = f"{req['client_first_name']} {req['client_last_name']}"
    link_addr, link_masked = _recipient(req)
    _, otp_masked = _otp_recipient(req)
    rows = []
    for e in events_for(conn, req["id"]):
        d = json.loads(e["details_json"] or "{}")
        t = e["type"]
        if e["actor"] == "client":
            signer = signer_name
        elif e["actor"] == "system":
            signer = ""
        else:
            u = conn.execute("SELECT name FROM users WHERE id=?", (e["actor"].split(":", 1)[-1],)).fetchone()
            signer = f"Operatore: {u['name']}" if u else "Operatore"
        dev = device_id(e["ip"], e["user_agent"])
        lines = []
        if t == "request_created":
            lines.append(f"Richiesta {req['id']} creata nell'area operatore")
        elif t == "document_uploaded":
            lines.append(f"Documento {d.get('document_id', '')} caricato" + (" (da modello)" if d.get("da_modello") else ""))
            if d.get("pagine"):
                lines.append(f"Pagine: {d['pagine']}")
        elif t == "fields_defined":
            lines.append("Campi posizionati: " + ", ".join(pdfops.FIELD_TYPES.get(c, c) for c in d.get("campi", [])))
        elif t == "document_filled":
            lines.append("Documento compilato con i dati dichiarati e congelato")
            lines.append(f"SHA-256: {d.get('sha256_compilato', '')[:32]}...")
        elif t == "link_sent":
            lines.append(f"Link di accesso inviato via {d.get('canale', '')} a {d.get('destinatario', '')}")
            lines.append(f"Esito provider: {d.get('esito', '')}")
            lines.append(f"Scadenza link: {fmt_local(d.get('scadenza_link'), False)}")
        elif t == "link_delivered":
            lines.append(f"Riscontro di consegna dal provider: {d.get('riscontro', '')}")
        elif t == "link_opened":
            lines.append("Collegamento aperto dal cliente (apertura del link, non lettura del documento)")
            if dev:
                lines.append(f"Dispositivo: {dev}")
        elif t == "document_presented":
            lines.append("Documento mostrato al cliente (tutte le pagine)")
            lines.append(f"SHA-256 documento presentato: {d.get('sha256_compilato', '')[:32]}...")
        elif t == "document_downloaded":
            lines.append(f"Il cliente ha scaricato il file: {d.get('file', '')}")
        elif t == "data_disputed":
            lines.append(f"Il cliente ha segnalato dati errati: \"{d.get('motivo', '')}\"")
        elif t == "declaration_accepted":
            lines.append("Dichiarazione di sottoscrizione accettata esplicitamente (casella + pulsante)")
            lines.append(f"SHA-256 testo dichiarazione: {d.get('sha256_dichiarazione', '')[:32]}...")
        elif t == "otp_sent":
            lines.append(f"Preparata autenticazione con provider 'WhatsApp' (invio n. {d.get('invio_n', '')})")
            lines.append(f"Numero di telefono: {d.get('destinatario', otp_masked)}")
            lines.append(f"ID transazione: {d.get('id_transazione', '')}")
            lines.append(f"Scadenza: {fmt_local(d.get('scadenza'), False)}")
            lines.append(f"Esito invio: {d.get('esito', '')}")
        elif t == "otp_failed":
            lines.append(f"Codice OTP errato (tentativo {d.get('tentativo', '')}, rimasti {d.get('rimasti', '')})")
        elif t == "otp_rejected":
            lines.append(f"Codice OTP rifiutato: {d.get('motivo', '')}")
        elif t == "otp_verified":
            lines.append("Autenticato con provider 'WhatsApp'")
            lines.append(f"Numero di telefono: {otp_masked}")
            lines.append(f"Tentativo: {d.get('tentativo', '')} - codice monouso consumato (non riportato)")
        elif t == "final_generated":
            lines.append(f"Documento firmato: riquadro firma applicato a pagina {d.get('pagina_firma', '')} "
                         f"da {signer_name} (firma elettronica semplice tramite OTP)")
            lines.append(f"SHA-256 documento finale: {d.get('sha256_finale', '')[:32]}...")
        elif t == "audit_generated":
            lines.append("Audit Trail generato e archiviato")
        elif t == "completed":
            lines.append("Processo completato: documento finale e Audit Trail resi disponibili all'operatore e al cliente")
        elif t == "expired":
            lines.append(f"Link scaduto il {fmt_local(d.get('scadenza'), False)}")
        elif t == "revoked":
            lines.append("Richiesta revocata dall'operatore: link non più utilizzabile")
        elif t == "geo_recorded":
            lines.append(f"Posizione fornita dal browser del cliente (con consenso): {d.get('geo', '')}")
        elif t == "error":
            lines.append(f"Errore ({d.get('fase', '')}): {d.get('errore', '')}")
        else:
            lines.append(EVENT_LABELS.get(t, t))
        rows.append({"when": fmt_local(e["ts_utc"], False), "action": EVENT_ACTIONS.get(t, t),
                     "description": "\n".join(lines), "signer": signer, "ip": e["ip"] or "",
                     "geo": d.get("geo") or "N/A"})
    return rows


def _place_date_text(req):
    """Testo del campo 'Luogo e data' nel documento finale."""
    dt = parse_iso(req["signed_at"]).astimezone(TZ)
    place = req["place"].strip()
    return (f"{place}, " if place else "") + dt.strftime("%d/%m/%Y")


def finalize_request(conn, req_id) -> tuple[bool, str]:
    """Genera documento finale + Audit Trail; solo dopo l'archiviazione la richiesta è 'completata'."""
    req = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if req["status"] == "completata":
        return True, "già completata"
    if not req["otp_verified_at"]:
        return False, "OTP non verificato."
    doc = get_document(conn, req["org_id"], req["document_id"])
    user = conn.execute("SELECT * FROM users WHERE id=?", (req["user_id"],)).fetchone()
    try:
        # Il documento compilato archiviato deve esistere ed essere integro: non si rigenera in silenzio.
        if not req["path_filled"] or not Path(req["path_filled"]).exists():
            raise RuntimeError("Documento compilato mancante in archivio.")
        if sha256_file(req["path_filled"]) != req["sha256_filled"]:
            raise RuntimeError("Il documento compilato archiviato non corrisponde all'impronta registrata.")

        folder = Path(req["path_filled"]).parent
        path_final = folder / "finale.pdf"
        signed_local = fmt_local(req["signed_at"])
        signed_utc = parse_iso(req["signed_at"]).astimezone(timezone.utc)
        # IP e dispositivo: quelli dell'evento di verifica OTP (effettivamente rilevati), altrimenti "non rilevato"
        ver = conn.execute("SELECT ip, user_agent FROM events WHERE request_id=? AND type='otp_verified' ORDER BY id DESC LIMIT 1",
                           (req_id,)).fetchone()
        sig_fields = [f for f in fields_of(req) if f["type"] in ("signature", "place_date")]
        signature_block = {
            "role": req["signer_role"] or "Il Cliente",
            "name": f"{req['client_first_name']} {req['client_last_name']}",
            "device": device_id(ver["ip"], ver["user_agent"]) if ver else None,
            "when_utc": signed_utc.strftime("%d/%m/%Y %H:%M:%S UTC"),
            "ip": ver["ip"] if ver else None,
            "place": req["place"].strip(),
            "date": signed_utc.astimezone(TZ).strftime("%d/%m/%Y"),
            "method": "firma elettronica semplice tramite OTP",
        }
        footer = (f"Richiesta {req['id']} - documento confermato il {signed_local} con firma elettronica semplice "
                  f"(conferma OTP via WhatsApp). Non è una firma digitale crittografica. Impronta del documento compilato: "
                  f"{req['sha256_filled'][:24]}...")
        pdfops.fill_document(req["path_filled"], str(path_final), sig_fields, {"place_date": _place_date_text(req)},
                             demo=False, final=True, signature_block=signature_block, footer=footer)
        sha_final = sha256_file(path_final)
        sig_page = next((f["page"] + 1 for f in sig_fields if f["type"] == "signature"), "")
        with transaction(conn):
            add_event(conn, req_id, req["org_id"], "final_generated", "system",
                      details={"sha256_finale": sha_final, "pagina_firma": sig_page})
            last = conn.execute("SELECT hash FROM events WHERE request_id=? ORDER BY id DESC LIMIT 1", (req_id,)).fetchone()

        _, link_masked = _recipient(req)
        _, otp_masked = _otp_recipient(req)
        info = {
            "app_name": cfg("APP_NAME"), "request_id": req["id"], "title": req["title"],
            "sender": f"{user['name']} <{user['email']}>", "org_name": org_name(conn, req["org_id"]),
            "final_status": "Completata", "signed_at": signed_local,
            "client_name": f"{req['client_first_name']} {req['client_last_name']}",
            "client_fiscal_code": req["client_fiscal_code"],
            "signer_role": req["signer_role"], "place": req["place"],
            "link_channel_label": "WhatsApp" if req["channel"] == "whatsapp" else "Email",
            "link_recipient_masked": link_masked, "otp_recipient_masked": otp_masked,
            "method": "Accettazione esplicita della dichiarazione + codice OTP monouso inviato via WhatsApp",
            "otp_result": f"Verificato il {fmt_local(req['otp_verified_at'])}",
            "documents": [
                {"label": "PDF originale caricato", "name": f"{doc['id']} - {doc['filename']}", "sha256": doc["sha256_original"]},
                {"label": "PDF compilato presentato al cliente", "name": f"{req['id']} - compilato.pdf", "sha256": req["sha256_filled"]},
                {"label": "PDF finale dopo la conferma", "name": f"{req['id']} - finale.pdf", "sha256": sha_final},
            ],
            "declaration_text": req["declaration_text"],
            "declaration_accepted_at": fmt_local(req["declaration_accepted_at"]),
            "generated_at": fmt_local(iso_utc()),
            "chain_last_hash": last["hash"],
        }
        path_audit = folder / "audit_trail.pdf"
        pdfops.build_audit_pdf(str(path_audit), info, _audit_event_rows(conn, req), demo=cfg("DEMO"))
        sha_audit = sha256_file(path_audit)

        with transaction(conn):
            cur = conn.execute(
                "UPDATE requests SET path_final=?, sha256_final=?, path_audit=?, sha256_audit=?, status='completata', "
                "last_error=NULL, updated_at=? WHERE id=? AND status='in_corso'",
                (str(path_final), sha_final, str(path_audit), sha_audit, iso_utc(), req_id))
            if cur.rowcount == 0:
                raise RuntimeError("Stato cambiato durante la finalizzazione.")
            add_event(conn, req_id, req["org_id"], "audit_generated", "system", details={"sha256_audit": sha_audit})
            add_event(conn, req_id, req["org_id"], "completed", "system")
            notify(conn, req["org_id"], req["user_id"], req_id,
                   f"Richiesta {req['id']} completata: «{req['title']}» confermata da "
                   f"{req['client_first_name']} {req['client_last_name']} il {fmt_local(req['signed_at'], False)}.")
            link = f"{base_url()}/op/requests/{req_id}"
            body = (f"La richiesta {req['id']} («{req['title']}») è stata completata il {signed_local}.\n"
                    f"Documento finale e Audit Trail sono disponibili nell'area operatore (accesso protetto): {link}")
            result = send_email(current_app.config, user["email"], f"Richiesta {req['id']} completata", body)
            _outbox(conn, req["org_id"], req_id, "email", "notify_operator", mask_email(user["email"]),
                    f"Richiesta {req['id']} completata", body, result)
        return True, "completata"
    except Exception as e:  # noqa: BLE001
        with transaction(conn):
            conn.execute("UPDATE requests SET status='errore', last_error=?, updated_at=? WHERE id=?",
                         (f"Finalizzazione non riuscita: {e}", iso_utc(), req_id))
            add_event(conn, req_id, req["org_id"], "error", "system", details={"fase": "finalizzazione", "errore": str(e)[:200]})
        return False, f"Finalizzazione non riuscita: {e}"


def retry_finalize(conn, req, user) -> tuple[bool, str]:
    """L'operatore può ritentare la generazione dei file se la finalizzazione è fallita."""
    if req["status"] != "errore" or not req["otp_verified_at"]:
        return False, "Nessuna finalizzazione da ripetere."
    with transaction(conn):
        conn.execute("UPDATE requests SET status='in_corso', updated_at=? WHERE id=?", (iso_utc(), req["id"]))
    return finalize_request(conn, req["id"])


# ------------------------------------------------------------------ modelli

def save_template(conn, org_id, user_id, name, document_id, fields):
    tid = new_id("TPL", 6)
    conn.execute("INSERT INTO templates(id, org_id, user_id, name, document_id, fields_json, created_at) VALUES (?,?,?,?,?,?,?)",
                 (tid, org_id, user_id, name[:120], document_id, json.dumps(fields), iso_utc()))
    return tid


def list_templates(conn, org_id):
    return conn.execute("SELECT t.*, d.filename, d.page_count FROM templates t JOIN documents d ON d.id=t.document_id "
                        "WHERE t.org_id=? ORDER BY t.created_at DESC", (org_id,)).fetchall()
