"""Test end-to-end del percorso dimostrativo (pytest).

Coprono: caricamento e validazione PDF, posizionamento campi, compilazione, invio simulato,
pagina cliente, OTP errato / riutilizzato / scaduto, doppia conferma, scadenza link, revoca,
download, corrispondenza delle impronte SHA-256, isolamento tra organizzazioni, catena eventi.
"""
import io
import json
import re
import sqlite3
import zipfile
from datetime import timedelta
from pathlib import Path

import pymupdf
import pytest

from app import create_app
from app.utils import iso_utc, now_utc, sha256_file, valid_fiscal_code


# ------------------------------------------------------------------ fixture

@pytest.fixture()
def app(tmp_path):
    app = create_app({"DATA_DIR": tmp_path / "data", "TESTING": True, "SECRET_KEY": "test-secret",
                      "MODE": "demo", "BASE_URL": "http://test.local"})
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf_of(resp):
    return re.search(r'name="_csrf" value="([^"]+)"', resp.get_data(as_text=True)).group(1)


def login(client, email="operatore@demo.local"):
    c = csrf_of(client.get("/login"))
    r = client.post("/login", data={"_csrf": c, "email": email, "password": "demo1234"})
    assert r.status_code == 302
    return csrf_of(client.get("/op/requests"))


def db(app):
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.row_factory = sqlite3.Row
    return conn


def sample_pdf_bytes():
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((60, 200), "Nome e cognome: ____________________", fontsize=11)
    p.insert_text((60, 240), "Codice fiscale: ____________________", fontsize=11)
    p.insert_text((60, 700), "Firma: ", fontsize=11)
    doc.new_page(width=595, height=842).insert_text((60, 100), "Pagina 2 (allegato)", fontsize=11)
    return doc.tobytes()


FIELDS = [
    {"type": "full_name", "page": 0, "x": 0.35, "y": 0.215, "w": 0.4, "h": 0.025},
    {"type": "fiscal_code", "page": 0, "x": 0.3, "y": 0.263, "w": 0.4, "h": 0.025},
    {"type": "signature", "page": 0, "x": 0.2, "y": 0.78, "w": 0.5, "h": 0.08},
]
CLIENT = {"title": "Modulo test", "first_name": "Giulia", "last_name": "Bianchi", "fiscal_code": "bncgli70a41f205d",
          "phone": "+39 333 0000000", "email": "giulia@example.invalid", "channel": "email",
          "signer_role": "Il Delegante", "place": "20134 MILANO MI", "expiry_days": "3"}


def create_and_send(client, csrf, pdf=None, fields=FIELDS, data=CLIENT):
    r = client.post("/op/requests/new", data={"_csrf": csrf, "title": "Modulo test",
                                              "pdf": (io.BytesIO(pdf or sample_pdf_bytes()), "modulo.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 302, r.get_data(as_text=True)
    req_id = r.headers["Location"].split("/requests/")[1].split("/")[0]
    r = client.post(f"/op/requests/{req_id}/fields", json={"fields": fields}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json["ok"]
    r = client.post(f"/op/requests/{req_id}/data", data={"_csrf": csrf, **data})
    assert r.status_code == 302, r.get_data(as_text=True)
    r = client.get(f"/op/requests/{req_id}/preview")
    assert r.status_code == 200 and b"Invia (simulato)" in r.data
    r = client.post(f"/op/requests/{req_id}/send", data={"_csrf": csrf})
    assert r.status_code == 302
    return req_id


def client_token(app, req_id):
    return db(app).execute("SELECT demo_link_token FROM requests WHERE id=?", (req_id,)).fetchone()[0]


def demo_otp(app, req_id):
    row = db(app).execute("SELECT demo_code FROM otps WHERE request_id=? AND used_at IS NULL AND invalidated=0 "
                          "ORDER BY id DESC LIMIT 1", (req_id,)).fetchone()
    return row[0] if row else None


def client_accept(client, tok):
    page = client.get(f"/c/{tok}")
    assert page.status_code == 200
    c = csrf_of(page)
    r = client.post(f"/c/{tok}/accept", data={"_csrf": c, "accept": "yes"})
    assert r.status_code == 302
    return c


# ------------------------------------------------------------------ test

def test_login_required_and_wrong_password(client):
    assert client.get("/op/requests").status_code == 302
    c = csrf_of(client.get("/login"))
    r = client.post("/login", data={"_csrf": c, "email": "operatore@demo.local", "password": "sbagliata"})
    assert r.status_code == 200 and "Credenziali non valide" in r.get_data(as_text=True)


def test_seed_has_demo_request_and_template(client, app):
    login(client)
    html = client.get("/op/requests").get_data(as_text=True)
    assert "Inviata" in html and "Bozza" in html
    assert "Consenso trattamento dati" in client.get("/op/templates").get_data(as_text=True)


def test_pdf_validation(client):
    csrf = login(client)
    r = client.post("/op/requests/new", data={"_csrf": csrf, "pdf": (io.BytesIO(b"non un pdf" * 20), "x.pdf")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "non è un PDF valido" in r.get_data(as_text=True)
    r = client.post("/op/requests/new", data={"_csrf": csrf, "pdf": (io.BytesIO(b"%PDF-1.4 broken" * 20), "x.pdf")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "Impossibile aprire" in r.get_data(as_text=True)
    r = client.post("/op/requests/new", data={"_csrf": csrf, "pdf": (io.BytesIO(sample_pdf_bytes()), "x.txt")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "estensione .pdf" in r.get_data(as_text=True)


def test_fiscal_code_validation():
    assert valid_fiscal_code("BNCGLI70A41F205D")
    assert not valid_fiscal_code("BNCGLI70A41F205X")
    assert not valid_fiscal_code("ABC")


def test_data_validation_requires_phone_and_signature(client):
    csrf = login(client)
    r = client.post("/op/requests/new", data={"_csrf": csrf, "pdf": (io.BytesIO(sample_pdf_bytes()), "m.pdf")},
                    content_type="multipart/form-data")
    req_id = r.headers["Location"].split("/requests/")[1].split("/")[0]
    r = client.post(f"/op/requests/{req_id}/fields", json={"fields": [FIELDS[0]]}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    bad = dict(CLIENT, phone="", fiscal_code="XXX")
    r = client.post(f"/op/requests/{req_id}/data", data={"_csrf": csrf, **bad})
    html = r.get_data(as_text=True)
    assert "obbligatorio" in html and "Codice fiscale non valido" in html
    client.post(f"/op/requests/{req_id}/data", data={"_csrf": csrf, **CLIENT})
    html = client.get(f"/op/requests/{req_id}/preview").get_data(as_text=True)
    assert "Manca il campo firma" in html
    r = client.post(f"/op/requests/{req_id}/send", data={"_csrf": csrf}, follow_redirects=True)
    assert "Posiziona almeno il campo firma" in r.get_data(as_text=True)


def test_fields_validation_rejects_out_of_page(client):
    csrf = login(client)
    r = client.post("/op/requests/new", data={"_csrf": csrf, "pdf": (io.BytesIO(sample_pdf_bytes()), "m.pdf")},
                    content_type="multipart/form-data")
    req_id = r.headers["Location"].split("/requests/")[1].split("/")[0]
    bad = [{"type": "signature", "page": 7, "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1}]
    r = client.post(f"/op/requests/{req_id}/fields", json={"fields": bad}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400
    # anteprime delle pagine (2 pagine) e pagina inesistente
    doc_id = db(client.application).execute("SELECT document_id FROM requests WHERE id=?", (req_id,)).fetchone()[0]
    assert client.get(f"/op/documents/{doc_id}/page/1.png").status_code == 200
    assert client.get(f"/op/documents/{doc_id}/page/2.png").status_code == 404


def test_full_flow_to_completion_with_hashes_and_downloads(client, app):
    csrf = login(client)
    req_id = create_and_send(client, csrf)
    conn = db(app)
    req = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    assert req["status"] == "inviata" and req["sha256_filled"] and req["link_token_hash"]
    assert sha256_file(req["path_filled"]) == req["sha256_filled"]
    tok = client_token(app, req_id)

    # pagina cliente: apertura, documento, dati
    page = client.get(f"/c/{tok}", headers={"User-Agent": "TestPhone/1.0"})
    html = page.get_data(as_text=True)
    assert "Giulia" in html and "BNCGLI70A41F205D" in html and "Accetto e richiedo" in html
    assert client.get(f"/c/{tok}/page/0.png").status_code == 200
    assert client.get(f"/c/{tok}/download/documento").status_code == 200
    assert conn.execute("SELECT status FROM requests WHERE id=?", (req_id,)).fetchone()[0] == "aperta"

    # accettazione + OTP
    c = client_accept(client, tok)
    assert conn.execute("SELECT status FROM requests WHERE id=?", (req_id,)).fetchone()[0] == "in_corso"
    otp = demo_otp(app, req_id)
    assert otp and len(otp) == 6
    # OTP errato
    r = client.post(f"/c/{tok}/otp/verify", data={"_csrf": c, "otp": "000000"}, follow_redirects=True)
    assert "Codice errato" in r.get_data(as_text=True)
    # OTP corretto
    r = client.post(f"/c/{tok}/otp/verify", data={"_csrf": c, "otp": otp})
    assert r.status_code == 302 and r.headers["Location"].endswith("/done")
    req = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    assert req["status"] == "completata" and req["signed_at"] and req["path_final"] and req["path_audit"]
    # impronte corrispondono ai file archiviati
    assert sha256_file(req["path_final"]) == req["sha256_final"]
    assert sha256_file(req["path_audit"]) == req["sha256_audit"]
    assert sha256_file(req["path_filled"]) == req["sha256_filled"]
    # OTP non deve comparire in log/eventi/report; demo_code consumato
    events_blob = " ".join(e[0] for e in conn.execute("SELECT details_json FROM events WHERE request_id=?", (req_id,)))
    assert otp not in events_blob
    audit_text = "".join(p.get_text() for p in pymupdf.open(req["path_audit"]))
    assert otp not in audit_text and tok not in audit_text
    assert "DEMO" not in audit_text and "SHA-256" in audit_text and "OtpVerificato" in audit_text
    assert req["sha256_final"][:24] in audit_text.replace("\n", "")
    # documento finale: riquadro firma con nome, ruolo, luogo, data UTC
    final_text = pymupdf.open(req["path_final"])[0].get_text()
    assert "Il Delegante" in final_text and "Confermato da: Giulia Bianchi" in final_text
    assert "20134 MILANO MI" in final_text and "UTC" in final_text and "tramite OTP" in final_text
    # reuse OTP / doppia conferma: idempotente, nessun secondo completamento
    r = client.post(f"/c/{tok}/otp/verify", data={"_csrf": c, "otp": otp})
    assert r.status_code == 302 and r.headers["Location"].endswith("/done")
    assert conn.execute("SELECT COUNT(*) FROM events WHERE request_id=? AND type='completed'", (req_id,)).fetchone()[0] == 1
    # pagina completamento e download cliente
    assert "Documento confermato" in client.get(f"/c/{tok}/done").get_data(as_text=True)
    assert client.get(f"/c/{tok}/download/finale").status_code == 200
    assert client.get(f"/c/{tok}/download/audit").status_code == 200
    # ritorno alla banca: notifica + download + zip
    html = client.get(f"/op/requests/{req_id}").get_data(as_text=True)
    assert "Completata" in html and "Registro integro" in html
    assert "completata" in client.get("/op/notifications").get_data(as_text=True)
    for which in ("originale", "compilato", "finale", "audit"):
        assert client.get(f"/op/requests/{req_id}/download/{which}").status_code == 200
    z = client.get(f"/op/requests/{req_id}/download/zip")
    assert z.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(z.data)).namelist()
    assert any("finale" in n for n in names) and any("audit" in n for n in names)


def test_otp_attempts_resend_and_expiry(client, app):
    csrf = login(client)
    req_id = create_and_send(client, csrf)
    tok = client_token(app, req_id)
    c = client_accept(client, tok)
    conn = db(app)
    # tentativi esauriti
    for i in range(app.config["OTP_MAX_ATTEMPTS"]):
        r = client.post(f"/c/{tok}/otp/verify", data={"_csrf": c, "otp": "111111"}, follow_redirects=True)
    assert "esauriti" in r.get_data(as_text=True)
    otp = demo_otp(app, req_id)  # ancora in tabella ma non più accettato
    r = client.post(f"/c/{tok}/otp/verify", data={"_csrf": c, "otp": otp}, follow_redirects=True)
    assert "Richiedi un nuovo codice" in r.get_data(as_text=True)
    # reinvio: cooldown
    r = client.post(f"/c/{tok}/otp/send", data={"_csrf": c}, follow_redirects=True)
    assert "Attendi" in r.get_data(as_text=True)
    conn.execute("UPDATE otps SET created_at=? WHERE request_id=?", (iso_utc(now_utc() - timedelta(minutes=2)), req_id))
    conn.commit()
    r = client.post(f"/c/{tok}/otp/send", data={"_csrf": c}, follow_redirects=True)
    assert "Invio OTP simulato" in r.get_data(as_text=True)
    otp2 = demo_otp(app, req_id)
    assert otp2 and otp2 != otp
    # scadenza OTP
    conn.execute("UPDATE otps SET expires_at=? WHERE request_id=? AND used_at IS NULL AND invalidated=0",
                 (iso_utc(now_utc() - timedelta(seconds=1)), req_id))
    conn.commit()
    r = client.post(f"/c/{tok}/otp/verify", data={"_csrf": c, "otp": otp2}, follow_redirects=True)
    assert "Codice scaduto" in r.get_data(as_text=True)
    assert conn.execute("SELECT status FROM requests WHERE id=?", (req_id,)).fetchone()[0] == "in_corso"


def test_link_expiry_and_revoke_and_invalid_token(client, app):
    csrf = login(client)
    req_id = create_and_send(client, csrf)
    tok = client_token(app, req_id)
    conn = db(app)
    conn.execute("UPDATE requests SET expires_at=? WHERE id=?", (iso_utc(now_utc() - timedelta(hours=1)), req_id))
    conn.commit()
    r = client.get(f"/c/{tok}")
    assert r.status_code == 410 and "scaduto" in r.get_data(as_text=True)
    assert conn.execute("SELECT status FROM requests WHERE id=?", (req_id,)).fetchone()[0] == "scaduta"
    assert client.get("/c/abcdefghijklmnopqrstuvwxyz1234567890").status_code == 404
    assert client.get("/c/short").status_code == 404
    # revoca
    req2 = create_and_send(client, csrf)
    tok2 = client_token(app, req2)
    r = client.post(f"/op/requests/{req2}/revoke", data={"_csrf": csrf})
    assert r.status_code == 302
    r = client.get(f"/c/{tok2}")
    assert r.status_code == 410 and "annullata" in r.get_data(as_text=True)


def test_dispute_interrupts_and_notifies(client, app):
    csrf = login(client)
    req_id = create_and_send(client, csrf)
    tok = client_token(app, req_id)
    c = csrf_of(client.get(f"/c/{tok}"))
    r = client.post(f"/c/{tok}/dispute", data={"_csrf": c, "reason": "Il cognome è sbagliato"})
    assert r.status_code == 200 and "Segnalazione registrata" in r.get_data(as_text=True)
    assert db(app).execute("SELECT status FROM requests WHERE id=?", (req_id,)).fetchone()[0] == "errore"
    assert client.get(f"/c/{tok}").status_code == 410
    assert "dati errati" in client.get("/op/notifications").get_data(as_text=True)
    # non si può più accettare
    assert client.post(f"/c/{tok}/accept", data={"_csrf": c, "accept": "yes"}).status_code == 400


def test_org_isolation(client, app):
    csrf = login(client)
    req_id = create_and_send(client, csrf)
    other = app.test_client()
    login(other, "altro@demo.local")
    assert other.get(f"/op/requests/{req_id}").status_code == 404
    assert other.get(f"/op/requests/{req_id}/download/originale").status_code == 404
    assert req_id not in other.get("/op/requests").get_data(as_text=True)


def test_event_chain_detects_tampering(client, app):
    from app.services import verify_chain
    csrf = login(client)
    req_id = create_and_send(client, csrf)
    with app.app_context():
        from app.db import get_db
        ok, n = verify_chain(get_db(), req_id)
        assert ok and n >= 4
        get_db().execute("UPDATE events SET details_json='{\"manomesso\": true}' WHERE request_id=? AND type='link_sent'", (req_id,))
        ok, _ = verify_chain(get_db(), req_id)
        assert not ok
    assert "REGISTRO ALTERATO" in client.get(f"/op/requests/{req_id}").get_data(as_text=True)


def test_missing_filled_document_is_not_regenerated(client, app):
    csrf = login(client)
    req_id = create_and_send(client, csrf)
    tok = client_token(app, req_id)
    c = client_accept(client, tok)
    otp = demo_otp(app, req_id)
    path = db(app).execute("SELECT path_filled FROM requests WHERE id=?", (req_id,)).fetchone()[0]
    Path(path).unlink()
    r = client.post(f"/c/{tok}/otp/verify", data={"_csrf": c, "otp": otp}, follow_redirects=True)
    row = db(app).execute("SELECT status, last_error FROM requests WHERE id=?", (req_id,)).fetchone()
    assert row["status"] == "errore" and "mancante" in row["last_error"]


def test_rotated_page_and_template_reuse(client, app):
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((60, 200), "Nome: ______", fontsize=11)
    p.set_rotation(90)
    csrf = login(client)
    fields = [{"type": "full_name", "page": 0, "x": 0.3, "y": 0.2, "w": 0.3, "h": 0.03},
              {"type": "signature", "page": 0, "x": 0.5, "y": 0.6, "w": 0.4, "h": 0.15}]
    r = client.post("/op/requests/new", data={"_csrf": csrf, "pdf": (io.BytesIO(doc.tobytes()), "ruotato.pdf")},
                    content_type="multipart/form-data")
    req_id = r.headers["Location"].split("/requests/")[1].split("/")[0]
    r = client.post(f"/op/requests/{req_id}/fields", json={"fields": fields, "save_template": True,
                                                           "template_name": "Modello ruotato"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    client.post(f"/op/requests/{req_id}/data", data={"_csrf": csrf, **CLIENT})
    client.post(f"/op/requests/{req_id}/send", data={"_csrf": csrf})
    req = db(app).execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    assert req["status"] == "inviata"
    text = pymupdf.open(req["path_filled"])[0].get_text()
    assert "Giulia Bianchi" in text
    # riuso del modello
    tpl = db(app).execute("SELECT id FROM templates WHERE name='Modello ruotato'").fetchone()[0]
    r = client.post(f"/op/templates/{tpl}/use", data={"_csrf": csrf})
    assert r.status_code == 302 and "/fields" in r.headers["Location"]


def test_real_mode_shows_not_configured(tmp_path):
    app = create_app({"DATA_DIR": tmp_path / "real", "TESTING": True, "SECRET_KEY": "s", "MODE": "real"})
    client = app.test_client()
    csrf = login(client)
    html = client.get("/op/outbox").get_data(as_text=True)
    assert "non configurato" in html
    # l'invio reale senza provider configurato non segna "inviata"
    r = client.post("/op/requests/new", data={"_csrf": csrf, "pdf": (io.BytesIO(sample_pdf_bytes()), "m.pdf")},
                    content_type="multipart/form-data")
    req_id = r.headers["Location"].split("/requests/")[1].split("/")[0]
    client.post(f"/op/requests/{req_id}/fields", json={"fields": FIELDS}, headers={"X-CSRF-Token": csrf})
    client.post(f"/op/requests/{req_id}/data", data={"_csrf": csrf, **CLIENT})
    r = client.post(f"/op/requests/{req_id}/send", data={"_csrf": csrf}, follow_redirects=True)
    assert "non configurato" in r.get_data(as_text=True)
    row = db(app).execute("SELECT status, demo_link_token FROM requests WHERE id=?", (req_id,)).fetchone()
    assert row["status"] == "errore" and row["demo_link_token"] is None
