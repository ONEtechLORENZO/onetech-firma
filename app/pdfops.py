"""Operazioni sui PDF: validazione, anteprime, compilazione, documento finale, Audit Trail.

Le coordinate dei campi sono salvate come frazioni (0..1) della pagina COME VISTA
(cioè già ruotata secondo /Rotate). La conversione verso lo spazio del PDF
(matrice di de-rotazione) avviene qui, una sola volta.
"""
import json
import io
from datetime import datetime

import pymupdf
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                                KeepTogether)

FIELD_TYPES = {
    "first_name": "Nome",
    "last_name": "Cognome",
    "full_name": "Nome e cognome",
    "fiscal_code": "Codice fiscale",
    "place_date": "Luogo e data",
    "signature": "Firma",
}


class PdfError(ValueError):
    pass


# ---------------------------------------------------------------- validazione

def validate_and_inspect(data: bytes, max_mb: int, max_pages: int) -> dict:
    if not data or len(data) < 100:
        raise PdfError("Il file è vuoto o troppo piccolo.")
    if len(data) > max_mb * 1024 * 1024:
        raise PdfError(f"Il file supera il limite di {max_mb} MB.")
    if not data.startswith(b"%PDF-"):
        raise PdfError("Il file non è un PDF valido (intestazione mancante).")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        raise PdfError("Impossibile aprire il PDF: file danneggiato o non supportato.")
    with doc:
        if doc.needs_pass:
            raise PdfError("Il PDF è protetto da password: rimuovere la protezione prima del caricamento.")
        if doc.page_count == 0:
            raise PdfError("Il PDF non contiene pagine.")
        if doc.page_count > max_pages:
            raise PdfError(f"Il PDF ha troppe pagine (massimo {max_pages}).")
        pages = []
        form_fields = []
        for i, page in enumerate(doc):
            r = page.rect  # già ruotato (vista)
            pages.append({"w": round(r.width, 2), "h": round(r.height, 2), "rotation": page.rotation})
            try:
                for w in page.widgets():
                    if w.field_type_string not in ("Text", "Signature"):
                        continue
                    wr = w.rect * page.rotation_matrix
                    wr.normalize()
                    form_fields.append({
                        "name": w.field_name or f"campo_{len(form_fields) + 1}",
                        "kind": "signature" if w.field_type_string == "Signature" else "text",
                        "page": i,
                        "x": round(wr.x0 / r.width, 4), "y": round(wr.y0 / r.height, 4),
                        "w": round(wr.width / r.width, 4), "h": round(wr.height / r.height, 4),
                    })
            except Exception:
                pass  # alcuni PDF hanno AcroForm malformati: si ignora e si procede
        return {"page_count": doc.page_count, "pages": pages, "form_fields": form_fields}


# ---------------------------------------------------------------- anteprime

def render_page_png(path: str, page_index: int, width_px: int = 1100) -> bytes:
    with pymupdf.open(path) as doc:
        page = doc[page_index]
        zoom = width_px / page.rect.width
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png")


# ---------------------------------------------------------------- compilazione

def _rect_for(page, field) -> pymupdf.Rect:
    W, H = page.rect.width, page.rect.height
    r = pymupdf.Rect(field["x"] * W, field["y"] * H, (field["x"] + field["w"]) * W, (field["y"] + field["h"]) * H)
    ru = r * page.derotation_matrix
    ru.normalize()
    return ru


def _ascii_safe(text: str) -> str:
    """I font base del PDF (Helvetica) non coprono alcuni caratteri: sostituzioni sicure."""
    return (text.replace("—", "-").replace("–", "-").replace("’", "'").replace("“", '"').replace("”", '"')
            .replace("…", "...").replace("✓", "v"))


def _insert_fitted(page, rect, text, color, max_size=12.0, min_size=5.0, align=0):
    """Inserisce il testo riducendo il carattere finché entra nel rettangolo (insert_textbox
    restituisce un valore negativo quando il testo non entra)."""
    usable_h = rect.height if page.rotation in (0, 180) else rect.width
    size = min(max_size, max(min_size, usable_h / 1.7))
    while size >= min_size:
        rc = page.insert_textbox(rect, text, fontname="helv", fontsize=size, color=color,
                                 align=align, rotate=page.rotation)
        if rc >= 0:
            return size
        size -= 0.5
    return None



def fill_document(path_in: str, path_out: str, fields: list, values: dict, *,
                  demo: bool, final: bool, signature_block: dict | None = None,
                  footer: str | None = None) -> None:
    """Scrive i valori nei campi.

    final=False → versione "compilata" presentata al cliente (riquadro firma vuoto, tratteggiato).
    final=True  → versione "finale" dopo la conferma: riquadro firma con la rappresentazione
                  della conferma (ruolo, confermato da, dispositivo, ora/data UTC, IP, luogo).
                  NON è una firma digitale crittografica.
    """
    with pymupdf.open(path_in) as doc:
        widgets_by_name = {}
        for page in doc:
            try:
                for w in page.widgets():
                    widgets_by_name[(page.number, w.field_name)] = w
            except Exception:
                pass

        for f in fields:
            page = doc[int(f["page"])]
            ftype = f["type"]
            rect = _rect_for(page, f)
            if ftype == "signature":
                if final:
                    _draw_signature_block(page, rect, signature_block or {})
                else:
                    shape = page.new_shape()
                    shape.draw_rect(rect)
                    shape.finish(color=(0.45, 0.45, 0.45), fill=(0.97, 0.97, 0.97), width=0.6, dashes="[3 3] 0")
                    shape.commit()
                    inner = pymupdf.Rect(rect.x0 + 3, rect.y0 + 2, rect.x1 - 3, rect.y1 - 2)
                    _insert_fitted(page, inner, "Riquadro firma - in attesa di conferma del cliente",
                                   (0.4, 0.4, 0.4), max_size=7.5)
                continue

            value = _ascii_safe(str(values.get(ftype, "") or ""))
            if not value:
                continue
            widget = widgets_by_name.get((page.number, f.get("widget_name")))
            if widget is not None:
                widget.field_value = value
                widget.update()
                continue
            if page.rotation == 0:
                # testo sulla "riga": baseline vicino al bordo inferiore del riquadro, carattere leggibile
                size = max(6.0, min(11.0, rect.height * 0.72))
                while size > 6.0 and pymupdf.get_text_length(value, fontname="helv", fontsize=size) > rect.width - 4:
                    size -= 0.5
                page.insert_text((rect.x0 + 2, rect.y1 - rect.height * 0.22), value, fontname="helv",
                                 fontsize=size, color=(0, 0, 0.35))
            else:
                _insert_fitted(page, rect, value, (0, 0, 0.35))

        for page in doc:
            if final and footer:
                W, H = page.rect.width, page.rect.height
                r = pymupdf.Rect(8, H - 18, W - 8, H - 4) * page.derotation_matrix
                r.normalize()
                page.insert_textbox(r, _ascii_safe(footer), fontname="helv", fontsize=6.5, color=(0.3, 0.3, 0.3),
                                    align=pymupdf.TEXT_ALIGN_CENTER, rotate=page.rotation)

        doc.save(path_out, garbage=3, deflate=True)


def _draw_signature_block(page, rect, sb: dict):
    """Riquadro firma nel formato richiesto:
        Il Delegante
        v Confermato da: NOME COGNOME
        Dispositivo: dev-xxxx
        Ora/Data: gg/mm/aaaa hh:mm:ss UTC
        Indirizzo IP: x.x.x.x
        Luogo: 25045 CASTEGNATO BS   Data: gg/mm/aaaa
        (firma elettronica semplice tramite OTP)
    """
    shape = page.new_shape()
    shape.draw_rect(rect)
    shape.finish(color=(0.2, 0.2, 0.2), fill=(1, 1, 1), width=0.8)
    shape.commit()
    lines = [
        ("b", sb.get("role") or "Il Cliente"),
        ("n", f"CHECK Confermato da: {sb.get('name', '')}"),
        ("n", f"Dispositivo: {sb.get('device', 'non rilevato')}"),
        ("n", f"Ora/Data: {sb.get('when_utc', '')}"),
        ("n", f"Indirizzo IP: {sb.get('ip') or 'non rilevato'}"),
        ("n", (f"Luogo: {sb['place']}   " if sb.get("place") else "") + f"Data: {sb.get('date', '')}"),
        ("n", f"({sb.get('method', 'firma elettronica semplice tramite OTP')})"),
    ]
    usable_h = rect.height if page.rotation in (0, 180) else rect.width
    inner = pymupdf.Rect(rect.x0 + 4, rect.y0 + 3, rect.x1 - 4, rect.y1 - 3)
    title = _ascii_safe(lines[0][1])
    body = "\n".join(_ascii_safe(t).replace("CHECK ", "     ") for _, t in lines[1:])
    size = max(4.0, min(8.0, usable_h / (len(lines) * 1.45)))
    # riduce finché titolo + corpo entrano nel riquadro (insert_textbox < 0 = non entra)
    while size >= 4.0:
        lh = size * 1.25
        if page.rotation == 0:
            r_title = pymupdf.Rect(inner.x0, inner.y0, inner.x1, inner.y0 + lh * 1.3)
            r_body = pymupdf.Rect(inner.x0, inner.y0 + lh * 1.15, inner.x1, inner.y1)
        else:
            r_title, r_body = inner, inner  # pagine ruotate: blocco unico (titolo in prima riga)
            body = "\n".join([title] + body.split("\n")) if not body.startswith(title) else body
        # prova di inserimento su una pagina di servizio, per non lasciare tentativi parziali nel PDF
        probe = pymupdf.open()
        pp = probe.new_page(width=page.mediabox.width, height=page.mediabox.height)
        pp.set_rotation(page.rotation)
        rc1 = pp.insert_textbox(r_title, title, fontname="hebo", fontsize=size, lineheight=1.25, rotate=page.rotation)
        rc2 = pp.insert_textbox(r_body, body, fontname="helv", fontsize=size, lineheight=1.25, rotate=page.rotation)
        probe.close()
        if rc1 >= 0 and rc2 >= 0:
            page.insert_textbox(r_title, title, fontname="hebo", fontsize=size, color=(0.05, 0.05, 0.05),
                                lineheight=1.25, rotate=page.rotation)
            page.insert_textbox(r_body, body, fontname="helv", fontsize=size, color=(0.05, 0.05, 0.05),
                                lineheight=1.25, rotate=page.rotation)
            break
        size -= 0.25
    if page.rotation == 0:
        # segno di spunta all'inizio della riga "Confermato da"
        lh = size * 1.25
        y = r_body.y0 + size * 0.95
        x = inner.x0 + 0.5
        s = size * 0.55
        shape = page.new_shape()
        shape.draw_polyline([(x, y - s * 0.5), (x + s * 0.45, y), (x + s * 1.25, y - s * 1.2)])
        shape.finish(color=(0.1, 0.5, 0.25), width=max(0.8, size * 0.13))
        shape.commit()


# ---------------------------------------------------------------- Audit Trail

def build_audit_pdf(path_out: str, info: dict, events: list[dict], *, demo: bool) -> None:
    """Audit Trail in formato orizzontale: intestazione con i dati della richiesta e tabella
    Data e ora | Azione | Descrizione | Firmatario | Indirizzo IP | Geolocalizzazione.
    Non contiene OTP, token o link segreti."""
    from reportlab.lib.pagesizes import landscape
    page_w, page_h = landscape(A4)
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=8.5, leading=11)
    cell = ParagraphStyle("cell", parent=body, fontSize=7.6, leading=9.6)
    small = ParagraphStyle("small", parent=body, fontSize=7, leading=9, textColor=colors.HexColor("#555555"))
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontSize=15, leading=18, alignment=0, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=10, leading=12, spaceBefore=8, spaceAfter=3,
                        textColor=colors.HexColor("#1f2a44"))
    head = ParagraphStyle("head", parent=body, fontName="Helvetica-Bold", fontSize=8.5)
    mono = ParagraphStyle("mono", parent=body, fontName="Courier", fontSize=7, leading=9)

    def esc(t):
        return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def P(t, st=body):
        return Paragraph(esc(t), st)

    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawString(12 * mm, 8 * mm, f"{info['app_name']} - Audit Trail richiesta {info['request_id']} - pagina {doc_.page}")
        canvas.drawRightString(page_w - 12 * mm, 8 * mm, "L'Audit Trail è estraibile dal sistema e producibile in sede di contenzioso.")
        canvas.restoreState()

    doc = SimpleDocTemplate(path_out, pagesize=(page_w, page_h), leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=13 * mm, bottomMargin=14 * mm,
                            title=f"Audit Trail {info['request_id']}", author=info["app_name"])
    story = [P("Audit Trail - firma elettronica semplice con conferma OTP", h1),
             P(f"Richiesta {info['request_id']} · {info['title']} · generato il {info['generated_at']} · {info['app_name']}", small)]

    # --- riquadro riepilogo a due colonne
    left = [("Mittente (operatore)", info["sender"]), ("Organizzazione", info["org_name"]),
            ("Firmatario", info["client_name"]), ("Codice fiscale (dichiarato)", info["client_fiscal_code"]),
            ("Ruolo nel documento", info.get("signer_role") or "-"),
            ("Luogo dichiarato", info.get("place") or "-")]
    right = [("Link inviato via", f"{info['link_channel_label']}: {info['link_recipient_masked']}"),
             ("OTP inviato via", f"WhatsApp: {info['otp_recipient_masked']}"),
             ("Metodo di conferma", info["method"]), ("Esito verifica OTP", info["otp_result"]),
             ("Stato finale", info["final_status"]), ("Data/ora conferma", info.get("signed_at") or "-")]

    def kv(rows):
        t = Table([[P(k, small), P(v, cell)] for k, v in rows], colWidths=[36 * mm, 95 * mm])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
                               ("LINEBELOW", (0, 0), (-1, -1), 0.2, colors.HexColor("#e2e6ec"))]))
        return t
    two = Table([[kv(left), kv(right)]], colWidths=[134 * mm, 134 * mm])
    two.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [P("1. Richiesta e firmatario", h2), two,
              P("Nota: il controllo formale del codice fiscale e il possesso del recapito non costituiscono una verifica "
                "certa dell'identità della persona.", small)]

    # --- documenti e impronte
    doc_rows = [[P("Documento", head), P("Identificativo / nome", head), P("SHA-256", head)]]
    for d in info["documents"]:
        doc_rows.append([P(d["label"], cell), P(d["name"], cell), P(d["sha256"] or "-", mono)])
    t = Table(doc_rows, colWidths=[55 * mm, 80 * mm, 133 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f6")),
                           ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                           ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    story += [P("2. Documenti e impronte SHA-256", h2), t,
              P("Le impronte SHA-256 permettono di verificare che un file corrisponda a quello archiviato. "
                "Da sole non sono una firma digitale né una marca temporale.", small)]

    story += [P("3. Dichiarazione accettata dal cliente", h2), P(info["declaration_text"], cell),
              P(f"Accettata il: {info.get('declaration_accepted_at') or '-'}", small)]

    # --- tabella eventi nello stile richiesto
    story.append(P("4. Cronologia degli eventi registrati", h2))
    story.append(P("Sono riportati soltanto gli eventi effettivamente registrati dal sistema. \"Link aperto\" indica "
                   "l'apertura del collegamento, non la lettura del documento. I codici OTP non sono riportati.", small))
    ev_rows = [[P(x, head) for x in ("Data e ora", "Azione", "Descrizione", "Firmatario", "Indirizzo IP", "Geolocalizzazione")]]
    for e in events:
        desc = esc(e["description"]).replace("\n", "<br/>")
        ev_rows.append([P(e["when"], cell), P(e["action"], cell), Paragraph(desc, cell), P(e.get("signer") or "", cell),
                        P(e.get("ip") or "", cell), P(e.get("geo") or "N/A", cell)])
    t = Table(ev_rows, colWidths=[34 * mm, 40 * mm, 102 * mm, 36 * mm, 26 * mm, 30 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEABOVE", (0, 0), (-1, 0), 1.2, colors.HexColor("#222222")),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#222222")),
        ("LINEBELOW", (0, 1), (-1, -1), 0.2, colors.HexColor("#e2e6ec")),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(t)

    story += [P("5. Integrità del registro", h2),
              P("Il registro eventi è append-only e concatenato tramite hash: ogni evento include l'impronta dell'evento "
                "precedente. Hash dell'ultimo evento incluso in questo report:", cell),
              P(info["chain_last_hash"], mono)]
    story.append(KeepTogether([
        P("6. Natura del processo", h2),
        P("Processo di firma elettronica semplice (FES) basato su accettazione esplicita e conferma tramite codice OTP "
          "inviato via WhatsApp al numero indicato. Non è una firma elettronica avanzata, qualificata o certificata e non "
          "include sigilli digitali o marche temporali qualificate. L'idoneità del processo ai requisiti specifici di "
          "ciascun istituto deve essere valutata dall'istituto stesso.", cell)]))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


# ---------------------------------------------------------------- documento di esempio

def make_sample_pdf(path_out: str, bank_name: str = "Banca Esempio S.p.A.") -> None:
    """Modulo fittizio di esempio: un consenso/adesione con righe vuote da compilare."""
    styles = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=styles["BodyText"], fontSize=10, leading=14)
    h = ParagraphStyle("h", parent=styles["Title"], fontSize=15, leading=19)
    small = ParagraphStyle("s", parent=body, fontSize=8, leading=10, textColor=colors.HexColor("#555555"))
    doc = SimpleDocTemplate(path_out, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=22 * mm, bottomMargin=20 * mm, title="Modulo di adesione (fittizio)")
    story = [
        Paragraph(f"{bank_name} — Modulo di adesione al servizio di notifica digitale", h),
        Paragraph("DOCUMENTO FITTIZIO A SOLO SCOPO DIMOSTRATIVO", small),
        Spacer(1, 10),
        Paragraph("Il/La sottoscritto/a", body),
        Spacer(1, 6),
    ]
    lines = Table([["Nome", "", "Cognome", ""], ["Codice fiscale", "", "", ""]],
                  colWidths=[28 * mm, 55 * mm, 28 * mm, 55 * mm], rowHeights=[12 * mm, 12 * mm])
    lines.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LINEBELOW", (1, 0), (1, 0), 0.6, colors.black),
        ("LINEBELOW", (3, 0), (3, 0), 0.6, colors.black),
        ("LINEBELOW", (1, 1), (3, 1), 0.6, colors.black),
        ("SPAN", (1, 1), (3, 1)),
    ]))
    story.append(lines)
    story.append(Spacer(1, 14))
    for t in [
        "chiede di aderire al servizio di notifica digitale e dichiara di aver preso visione dell'informativa "
        "precontrattuale e del foglio informativo allegati, disponibili anche presso le filiali.",
        "Le comunicazioni relative al rapporto saranno inviate al recapito indicato dal cliente. Il cliente "
        "può modificare le proprie preferenze in qualsiasi momento tramite i canali messi a disposizione.",
        "Il presente modulo non comporta oneri aggiuntivi. Le condizioni economiche del rapporto restano "
        "quelle indicate nel contratto in essere.",
    ]:
        story.append(Paragraph(t, body))
        story.append(Spacer(1, 6))
    story.append(Spacer(1, 12))
    story.append(Paragraph("Informativa sul trattamento dei dati (estratto fittizio)", styles["Heading3"]))
    for i in range(1, 6):
        story.append(Paragraph(f"{i}. Lorem ipsum dolor sit amet, consectetur adipiscing elit. Testo segnaposto a "
                               "scopo dimostrativo, privo di qualsiasi valore contrattuale.", body))
    story.append(Spacer(1, 24))
    story.append(Paragraph("Luogo e data: ____________________", body))
    story.append(Spacer(1, 18))
    sig = Table([["Firma del cliente", ""]], colWidths=[45 * mm, 90 * mm], rowHeights=[22 * mm])
    sig.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("BOX", (1, 0), (1, 0), 0.6, colors.black),
    ]))
    story.append(sig)
    doc.build(story)
