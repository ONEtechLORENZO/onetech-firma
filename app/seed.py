"""Dati dimostrativi: documento fittizio, modello con campi già posizionati, richiesta di esempio."""
import json
from pathlib import Path

import pymupdf
from flask import current_app

from . import pdfops, services
from .db import get_db, transaction
from .utils import iso_utc, expires_in


SAMPLE_PDF = Path(__file__).resolve().parent.parent / "samples" / "CQ_CONSENSO_TRATT_PENSIONATI_0123.pdf"


def _locate_fields_consenso(pdf_path: str) -> list[dict]:
    """Campi per il modulo "Consenso dell'interessato - Cessione del Quinto Pensionati".
    Posizioni ancorate alle etichette del documento NOTO (non è un riconoscimento automatico generico)."""
    fields = []
    with pymupdf.open(pdf_path) as doc:
        page = doc[0]
        W, H = page.rect.width, page.rect.height

        def add(ftype, x0, y0, x1, y1):
            fields.append({"id": ftype, "type": ftype, "page": 0,
                           "x": round(x0 / W, 4), "y": round(y0 / H, 4),
                           "w": round((x1 - x0) / W, 4), "h": round((y1 - y0) / H, 4)})

        def hit(label):
            r = page.search_for(label)
            return r[0] if r else None

        r = hit("Il/la sottoscritto/a")
        if r:
            add("full_name", r.x1 + 4, r.y0 - 2, W - 56, r.y1 + 1)
        r = hit("C.F.")
        if r:
            add("fiscal_code", r.x1 + 4, r.y0 - 2, 338, r.y1 + 1)
        r = hit("Luogo e data")
        if r:
            add("place_date", r.x1 + 6, r.y0 - 2, W - 56, r.y1 + 1)
        r = hit("In fede")
        if r:
            # riquadro firma a destra di "In fede", sopra la dicitura "(Firma leggibile del Cliente)"
            add("signature", r.x1 + 10, r.y0 - 38, 400, r.y1 + 13)
    return fields


def _locate_fields_generic(pdf_path: str) -> list[dict]:
    """Campi per il modulo di esempio generato (fallback se manca il PDF del cliente)."""
    fields = []
    with pymupdf.open(pdf_path) as doc:
        page = doc[0]
        W, H = page.rect.width, page.rect.height
        mm = 72 / 25.4

        def box(label, ftype, x_mm, w_mm, height, dy):
            hits = page.search_for(label)
            if not hits:
                return
            r = hits[0]
            x0, width = x_mm * mm, w_mm * mm
            y0 = r.y1 + dy - height
            fields.append({"id": ftype, "type": ftype, "page": 0,
                           "x": round(x0 / W, 4), "y": round(y0 / H, 4),
                           "w": round(width / W, 4), "h": round(height / H, 4)})

        box("Nome", "first_name", 49, 52, 16, 2)
        box("Cognome", "last_name", 132, 52, 16, 2)
        box("Codice fiscale", "fiscal_code", 49, 135, 16, 2)
        box("Firma del cliente", "signature", 83, 88, 56, 3)
    return fields


def ensure_demo_request(app):
    conn = get_db()
    if conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] > 0:
        return
    user = conn.execute("SELECT * FROM users WHERE email='operatore@demo.local'").fetchone()
    if not user:
        return
    if SAMPLE_PDF.exists():
        data = SAMPLE_PDF.read_bytes()
        filename = SAMPLE_PDF.name
        title = "Consenso trattamento dati — CQ Pensionati"
        tpl_name = "Consenso trattamento dati (CQ Pensionati INPS)"
        locate = _locate_fields_consenso
    else:
        tmp = Path(app.config["DATA_DIR"]) / "modulo_esempio.pdf"
        pdfops.make_sample_pdf(str(tmp))
        data = tmp.read_bytes()
        filename = "modulo_adesione_esempio.pdf"
        title = "Modulo di adesione — demo"
        tpl_name = "Modulo di adesione (esempio fittizio)"
        locate = _locate_fields_generic
    with transaction(conn):
        doc_id, _info = services.store_document(conn, user["org_id"], user["id"], filename, data)
    doc = services.get_document(conn, user["org_id"], doc_id)
    fields = locate(doc["path_original"])
    with transaction(conn):
        services.save_template(conn, user["org_id"], user["id"], tpl_name, doc_id, fields)

    # Richiesta dimostrativa già inviata (in demo: invio simulato) a un cliente FITTIZIO.
    req_id = services.create_request(conn, user["org_id"], user["id"], doc_id, fields, title, from_template=True)
    with transaction(conn):
        conn.execute(
            "UPDATE requests SET client_first_name=?, client_last_name=?, client_fiscal_code=?, client_email=?, "
            "client_phone=?, channel='email', signer_role=?, place=?, message=?, expires_at=?, updated_at=? WHERE id=?",
            ("Mario", "Rossi", "RSSMRA55M01H501K", "mario.rossi@example.invalid", "+390000000000",
             "Il Delegante", "25045 CASTEGNATO BS",
             "Gentile cliente, le chiediamo di confermare il consenso allegato.", expires_in(7), iso_utc(), req_id))
    req = services.get_request(conn, user["org_id"], req_id)
    services.send_request(conn, req, user)

    # Una seconda richiesta lasciata in bozza, per mostrare il percorso da zero partendo dal modello.
    services.create_request(conn, user["org_id"], user["id"], doc_id, fields, title + " — bozza", from_template=True)
