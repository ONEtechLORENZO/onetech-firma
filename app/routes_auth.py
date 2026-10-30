"""Accesso protetto all'area operatore."""
from functools import wraps

from flask import Blueprint, render_template, request, redirect, url_for, session, g, flash, abort
from werkzeug.security import check_password_hash

from .db import get_db
from .ratelimit import rate_limited

bp = Blueprint("auth", __name__)


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not g.get("user"):
            return redirect(url_for("auth.login", next=request.path))
        return view(*a, **kw)
    return wrapped


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user"):
        return redirect(url_for("operator.requests_list"))
    error = None
    if request.method == "POST":
        if rate_limited("login", limit=20):
            abort(429)
        from . import check_csrf
        check_csrf()
        email = (request.form.get("email") or "").strip().lower()
        pwd = request.form.get("password") or ""
        user = get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], pwd):
            session.clear()
            session.permanent = True
            session["user_id"] = user["id"]
            nxt = request.form.get("next") or ""
            if not nxt.startswith("/op"):
                nxt = url_for("operator.requests_list")
            return redirect(nxt)
        error = "Credenziali non valide."
    return render_template("login.html", error=error, next=request.args.get("next", ""))


@bp.post("/logout")
def logout():
    session.clear()
    flash("Sessione chiusa.", "info")
    return redirect(url_for("auth.login"))
