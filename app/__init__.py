"""Factory dell'applicazione Flask."""
import logging
from pathlib import Path

from flask import Flask, session, g, redirect, url_for, request, abort

from .config import Config
from .db import init_db, close_db, get_db
from .services import ensure_seed, STATUS_LABELS, EVENT_LABELS
from .providers import service_status
from .utils import fmt_local, mask_email, mask_phone
from . import seed as seed_mod


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    app.config["DATA_DIR"] = Path(app.config["DATA_DIR"])
    app.config["DB_PATH"] = app.config["DATA_DIR"] / "firma.sqlite3"
    app.config["FILES_DIR"] = app.config["DATA_DIR"] / "files"
    app.config["RENDER_DIR"] = app.config["DATA_DIR"] / "renders"
    app.config["MAX_CONTENT_LENGTH"] = (app.config["MAX_PDF_MB"] + 1) * 1024 * 1024
    app.config["DEMO"] = app.config["MODE"] != "real"
    app.secret_key = app.config.get("SECRET_KEY") or Config.secret_key()

    # Nessun segreto nei log: livello INFO, senza dump di richieste.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    for d in ("DATA_DIR", "FILES_DIR", "RENDER_DIR"):
        app.config[d].mkdir(parents=True, exist_ok=True)
    init_db(app)
    ensure_seed(app)
    with app.app_context():
        seed_mod.ensure_demo_request(app)

    app.teardown_appcontext(close_db)

    from .routes_auth import bp as auth_bp
    from .routes_operator import bp as op_bp
    from .routes_client import bp as client_bp
    from .routes_webhooks import bp as wh_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(op_bp)
    app.register_blueprint(client_bp)
    app.register_blueprint(wh_bp)

    @app.before_request
    def load_user():
        g.user = None
        uid = session.get("user_id")
        if uid:
            g.user = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not g.user:
                session.clear()

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "frame-ancestors 'none'")
        return resp

    @app.context_processor
    def inject():
        return {
            "APP_NAME": app.config["APP_NAME"],
            "APP_TAGLINE": app.config["APP_TAGLINE"],
            "DEMO": app.config["DEMO"],
            "STATUS_LABELS": STATUS_LABELS,
            "EVENT_LABELS": EVENT_LABELS,
            "services": service_status(app.config),
            "fmt_local": fmt_local,
            "mask_email": mask_email,
            "mask_phone": mask_phone,
            "csrf_token": _csrf_token,
        }

    @app.route("/")
    def index():
        return redirect(url_for("operator.requests_list") if g.user else url_for("auth.login"))

    @app.route("/health")
    def health():
        return {"ok": True, "mode": app.config["MODE"]}

    return app


def _csrf_token() -> str:
    import secrets
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(24)
    return session["_csrf"]


def check_csrf():
    import hmac
    token = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
    if not session.get("_csrf") or not hmac.compare_digest(token, session["_csrf"]):
        abort(400, "Token CSRF non valido.")
