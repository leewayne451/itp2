"""Login and logout."""
import requests
from flask import Blueprint, render_template, request, redirect, url_for, session, flash

from ..backend_client import login as backend_login

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("token"):
            return redirect(url_for("dashboard.index"))
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    try:
        resp = backend_login(username, password)
    except requests.RequestException:
        flash("Backend unreachable. Is it running?", "error")
        return render_template("login.html")
    if resp.status_code != 200:
        flash("Invalid credentials.", "error")
        return render_template("login.html")
    data = resp.json()
    session["token"] = data["token"]
    session["user"] = data["user"]
    return redirect(url_for("dashboard.index"))


@bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
