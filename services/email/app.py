"""
Email service.

Receives notifications from the backend and does two things:
  1. ALWAYS records them in an in-memory mailbox you can read at http://localhost:8090
  2. If SMTP is configured, ALSO delivers them for real to the recipient's inbox

SMTP is configured purely through environment variables (see .env.example). If it is
not configured the service says so plainly on the mailbox page and in each message's
delivery status — so it is obvious why nothing arrived in a real inbox.
"""
import os
import re
import ssl
import smtplib
from email.message import EmailMessage
from collections import deque
from datetime import datetime, timezone

from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
SMTP_USER = os.environ.get("SMTP_USER", "").strip()


def _normalise_password(raw):
    """Google shows App Passwords grouped as 'xxxx xxxx xxxx xxxx' for readability,
    and people often paste them with those spaces (or hyphens). Those separators are
    NOT part of the password. Strip them only when the result is exactly a
    16-character alphanumeric string, so real passwords containing symbols or dashes
    are never altered."""
    raw = (raw or "").strip()
    compact = re.sub(r"[\s\-]", "", raw)
    return compact if len(compact) == 16 and compact.isalnum() else raw


SMTP_PASSWORD = _normalise_password(os.environ.get("SMTP_PASSWORD", ""))
SMTP_FROM = os.environ.get("SMTP_FROM", "").strip() or SMTP_USER or "accessmgmt@localhost"
# "true" -> STARTTLS on 587 (Gmail);  "false" -> implicit SSL on 465
SMTP_STARTTLS = os.environ.get("SMTP_TLS", "true").strip().lower() != "false"
SMTP_ENABLED = bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)

TLS_CERT = os.environ.get("TLS_CERT", "/certs/server.crt")
TLS_KEY = os.environ.get("TLS_KEY", "/certs/server.key")


def ssl_context():
    if os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY):
        return (TLS_CERT, TLS_KEY)
    print("[tls] certificate not found — serving plain HTTP", flush=True)
    return None


MAILBOX = deque(maxlen=500)


def deliver(to, subject, body):
    """Try a real send. Returns (status, detail) — never raises."""
    if not SMTP_ENABLED:
        return "not-configured", "SMTP not configured — stored in this local mailbox only."
    try:
        msg = EmailMessage()
        msg["From"] = SMTP_FROM
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        context = ssl.create_default_context()
        if SMTP_STARTTLS:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                s.starttls(context=context)
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=20) as s:
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        return "sent", f"Delivered to {to} via {SMTP_HOST}"
    except Exception as e:  # noqa: BLE001 — surface the reason instead of crashing
        return "failed", f"{type(e).__name__}: {e}"


@app.post("/send")
def send():
    body = request.get_json(silent=True) or {}
    to = body.get("to", "(unknown)")
    subject = body.get("subject", "(no subject)")
    text = body.get("body", "")
    status, detail = deliver(to, subject, text)
    MAILBOX.appendleft({
        "to": to, "subject": subject, "body": text,
        "status": status, "detail": detail,
        "at": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
    })
    print(f"[email] {status}: {to} — {subject} ({detail})", flush=True)
    return jsonify(ok=True, status=status, detail=detail)


@app.get("/api/inbox")
def inbox():
    return jsonify(list(MAILBOX))


@app.post("/api/clear")
def clear():
    MAILBOX.clear()
    return jsonify(ok=True)


@app.get("/")
def home():
    return render_template("inbox.html", messages=list(MAILBOX), count=len(MAILBOX),
                           smtp_enabled=SMTP_ENABLED, smtp_host=SMTP_HOST,
                           smtp_user=SMTP_USER, smtp_from=SMTP_FROM)


@app.get("/health")
def health():
    return jsonify(status="ok", smtp_enabled=SMTP_ENABLED, smtp_host=SMTP_HOST or None)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, ssl_context=ssl_context())
