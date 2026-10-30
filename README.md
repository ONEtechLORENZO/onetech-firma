# One Tech — demo di invio e firma elettronica semplice con Audit Trail

Web app dimostrativa, indipendente da altri progetti, per mostrare a una banca il percorso:

**caricamento PDF → posizionamento campi → dati cliente → invio link (email o WhatsApp) → pagina cliente sul telefono → OTP via WhatsApp → documento finale → Audit Trail PDF → ritorno automatico alla banca.**

È una demo di **firma elettronica semplice (FES)**: conferma esplicita tramite OTP, registrata con cronologia degli eventi e impronte SHA-256. Non è una firma avanzata, qualificata o certificata, non include sigilli digitali né marche temporali qualificate, e la sua idoneità ai requisiti di ciascun istituto va valutata dall'istituto.

Il nome "One Tech" si cambia con la variabile d'ambiente `APP_NAME`.

---

## 1. Avvio della demo

Requisiti: Python 3.11+ (testato con 3.13).

```bash
cd firma-demo
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

Apri **http://localhost:5050**.

- Operatore demo: `operatore@demo.local` / `demo1234` (precompilato nella pagina di accesso).
- Seconda organizzazione (per mostrare l'isolamento dei dati): `altro@demo.local` / `demo1234`.
- Al primo avvio vengono creati: il documento `samples/CQ_CONSENSO_TRATT_PENSIONATI_0123.pdf`, un **modello** con i campi già posizionati, una **richiesta già inviata** a un cliente fittizio (Mario Rossi) e una **bozza** da completare.
- I dati stanno in `data/` (SQLite + file). Per ripartire da zero: cancella la cartella `data/`.

Per provare dal telefono sulla stessa rete: `HOST=0.0.0.0 python run.py` e apri `http://<ip-del-pc>:5050`. I link cliente usano automaticamente l'indirizzo con cui l'operatore sta usando l'app (oppure `BASE_URL` se impostata).

Variabili utili (tutte facoltative in demo): `APP_NAME`, `APP_MODE` (`demo`|`real`), `PORT`, `HOST`, `BASE_URL`, `DATA_DIR`, `MAX_PDF_MB`.

---

## 2. Percorso da seguire durante la presentazione

Durata indicativa: 8–10 minuti. Tenere aperti due dispositivi (o due finestre): **console operatore** sul PC e **pagina cliente** sul telefono.

1. **Accesso** → elenco richieste con gli stati (bozza, inviata, aperta, in corso, completata, scaduta, revocata, errore).
2. **Nuova richiesta** → *Carica un PDF* (il documento reale di consenso, o qualunque altro PDF) oppure *Usa modello* "Consenso trattamento dati (CQ Pensionati INPS)" con i campi già posizionati.
3. **Campi sul documento**: mostrare che il PDF non ha campi compilabili e che l'operatore li posiziona a mano (Nome e cognome, Codice fiscale, Luogo e data, Firma); trascinare/ridimensionare; spuntare *Salva come modello*.
4. **Dati del cliente**: nome, cognome, codice fiscale (solo controllo formale: non è verifica d'identità), **cellulare WhatsApp (obbligatorio: riceve l'OTP)**, email, canale del link (email o WhatsApp), ruolo nel riquadro firma (es. "Il Delegante"), luogo, scadenza del link.
5. **Anteprima e invio**: il documento compilato esattamente come lo vedrà il cliente, destinatario e canale. *Invia (simulato)* → il documento viene **congelato** e ne viene registrata l'impronta SHA-256.
6. **Dettaglio richiesta**: stato *Inviata*, comunicazione simulata con il link, cronologia eventi. Pulsante **"Apri il link come cliente"** (solo demo): aprirlo sul telefone o in una seconda finestra.
7. **Pagina cliente** (nessun account): documento compilato, download, controllo dati, pulsante *I dati non sono corretti* (interrompe la firma e avvisa l'operatore), **dichiarazione di sottoscrizione** da accettare esplicitamente → richiesta OTP.
8. **OTP**: nella console operatore compare l'**OTP di test** (solo ambiente demo; in modalità reale viaggia solo su WhatsApp). Mostrare un codice **errato** (tentativi residui), poi quello corretto → *Conferma e firma*.
9. **Completamento cliente**: pagina di conferma con data/ora, download di documento finale e Audit Trail, impronte SHA-256.
10. **Ritorno alla banca**: la richiesta passa a *Completata*, notifica all'operatore (badge "Notifiche" + email simulata), documento finale con il **riquadro firma** (ruolo, confermato da, dispositivo, ora/data UTC, IP, luogo, data, "firma elettronica semplice tramite OTP"), **Audit Trail PDF** (tabella Data e ora / Azione / Descrizione / Firmatario / IP / Geolocalizzazione), download separati e **ZIP**. Mostrare il chip *Registro integro* (catena di hash).

Cose da dire chiaramente alla banca: "Aperto" significa apertura del link, non lettura; il possesso del recapito e il controllo formale del codice fiscale non identificano con certezza la persona; le impronte SHA-256 non sono una firma digitale né una marca temporale.

---

## 3. Funzioni realmente operative e funzioni simulate

**Realmente operative (anche in demo)**

- Accesso operatore con password (hash), sessione, CSRF, intestazioni di sicurezza, isolamento per organizzazione, download autorizzati.
- Caricamento PDF con validazione (estensione, intestazione, dimensione max, pagine max, PDF cifrati rifiutati); conservazione dell'originale e SHA-256.
- Editor visivo dei campi (tutte le pagine, spostamento, ridimensionamento, zoom indipendente, rotazione gestita); coordinate salvate in frazioni della pagina PDF; riuso dei campi compilabili se presenti; salvataggio come modello.
- Compilazione reale del PDF (PyMuPDF), anteprima, congelamento del documento compilato con impronta.
- Link cliente casuale (token da 256 bit), nel DB solo l'HMAC; scadenza; revoca; rate limiting per IP.
- Pagina cliente responsive: documento, download, segnalazione dati errati, dichiarazione, OTP, completamento.
- OTP: 6 cifre, scadenza 5 minuti, uso singolo, 5 tentativi, max 3 reinvii con attesa, conservato solo come HMAC (in demo anche in chiaro in una colonna separata, mai nei log/report).
- Doppia conferma e richieste concorrenti gestite con transazione esclusiva; completamento solo dopo generazione e archiviazione di documento finale e Audit Trail; in caso di errore stato *errore* con possibilità di ripetere la generazione; il documento compilato mancante o alterato **non** viene rigenerato in silenzio.
- Documento finale con riquadro firma, campo "Luogo e data", piè di pagina; Audit Trail PDF; impronte SHA-256 di originale, compilato, finale, audit; ZIP.
- Registro eventi append-only con catena di hash e verifica di integrità (la manomissione di una riga viene rilevata).
- Notifiche all'operatore; geolocalizzazione facoltativa dal browser del cliente (solo con consenso; altrimenti "N/A").

**Simulate in modalità demo**

- Invio email e WhatsApp: nessun messaggio parte; i testi compaiono in *Comunicazioni* etichettati "simulato".
- OTP: non viene inviato; il codice di test è visibile nella console operatore.
- Notifica email all'operatore: simulata (la notifica in-app è reale).
- Il pulsante "Apri il link come cliente" esiste solo in demo.
- Documento finale e Audit Trail sono marcati "DEMO — processo dimostrativo".

**Modalità reale (`APP_MODE=real`)** — integrazioni server-side già predisposte: SMTP per email (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TLS`), WhatsApp Cloud API con template approvati (`WA_ACCESS_TOKEN`, `WA_PHONE_NUMBER_ID`, `WA_TEMPLATE_LINK`, `WA_TEMPLATE_OTP`, `WA_TEMPLATE_LANG`; per una prova live senza template `WA_TEXT_MODE=1` invia testo libero, consegnato da Meta solo se il destinatario ha scritto al numero nelle ultime 24 ore; webhook `/webhooks/whatsapp` con `WA_WEBHOOK_VERIFY_TOKEN` per il riscontro di consegna). Senza credenziali il servizio è mostrato come "non configurato" e l'invio non viene segnato come "inviato". `SECRET_KEY` obbligatoria. Queste integrazioni **non sono state collaudate con provider reali** in questa consegna.

---

## 4. Requisiti mancanti per l'uso con clienti reali

1. **Provider reali e collaudo**: account SMTP, WhatsApp Business (numero verificato, template "link" e template di autenticazione OTP approvati da Meta), test end-to-end con numeri reali; gestione degli errori di consegna e delle risposte del webhook (firma delle richieste webhook).
2. **Hosting e sicurezza di produzione**: HTTPS obbligatorio (`COOKIE_SECURE=1`), reverse proxy con gestione corretta dell'IP reale (`X-Forwarded-For`), server WSGI (gunicorn/uwsgi) al posto del server di sviluppo, backup cifrati, rotazione dei log, segreti in un vault.
3. **Archiviazione a norma**: oggi i file sono su disco locale e SQLite; servono storage cifrato con conservazione, politiche di ritenzione/cancellazione (GDPR), log immutabili esterni (WORM) oltre alla catena di hash interna.
4. **Identità e utenti**: gestione utenti/ruoli reale (SSO/AD, MFA per gli operatori), più operatori per organizzazione, audit degli accessi operatore.
5. **Valutazione legale/compliance della banca**: inquadramento FES vs FEA/FEQ per il tipo di documento, testo della dichiarazione di sottoscrizione approvato dall'ufficio legale, informativa privacy per cliente e per la geolocalizzazione, eventuale marca temporale qualificata e sigillo elettronico sui file finali (non inclusi), conservazione sostitutiva.
6. **Identificazione del firmatario**: la demo non verifica l'identità (il possesso del numero WhatsApp non è identificazione certa). Se richiesto dalla banca: SPID/CIE, verifica documento, o processo di onboarding già in essere.
7. **Operatività**: scadenze/promemoria automatici, cancellazione dei file scaduti, monitoraggio, limiti di volume, accessibilità (WCAG) e test su più browser/dispositivi reali.
8. **Qualità del posizionamento**: il riconoscimento automatico dei punti di firma non è previsto; per documenti standard conviene usare i modelli salvati.

---

## 5. Test eseguiti

Test automatici (`python -m pytest tests -q`, 15 test, tutti superati):

- accesso protetto e credenziali errate;
- dati dimostrativi (richiesta inviata, bozza, modello);
- validazione PDF (non-PDF, PDF corrotto, estensione errata), anteprime pagine, pagina inesistente;
- controllo formale del codice fiscale; cellulare obbligatorio; campo firma obbligatorio; campi fuori pagina rifiutati;
- percorso completo fino al completamento: stati, congelamento, OTP errato poi corretto, **corrispondenza delle impronte SHA-256** con i file archiviati, assenza di OTP e token nel report e negli eventi, contenuto del riquadro firma e dell'Audit Trail, **doppia conferma** idempotente, download cliente e operatore, ZIP, notifica;
- OTP: tentativi esauriti, codice riutilizzato rifiutato, cooldown reinvio, nuovo codice, **codice scaduto**;
- **scadenza del link**, token non valido, revoca;
- segnalazione dati errati (interruzione e notifica);
- isolamento tra organizzazioni;
- rilevamento della manomissione del registro eventi;
- documento compilato mancante → errore, nessuna rigenerazione silenziosa;
- pagina ruotata (90°) e riuso del modello;
- modalità reale senza credenziali → "non configurato", nessun "inviato".

Verifica manuale in browser (Chromium via Playwright, desktop 1280px e iPhone 13): accesso, elenco, editor con trascinamento del campo firma, inserimento dati, anteprima, invio simulato, pagina cliente sul telefono con geolocalizzazione, OTP errato e corretto, pagina di completamento, dettaglio richiesta completata, notifiche, comunicazioni; nessun errore in console.

Nota: le integrazioni reali (SMTP, WhatsApp Cloud API) non sono state eseguite verso provider veri.
