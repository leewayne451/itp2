"""
Email service.

Receives notifications from the backend and does two things:
  1. ALWAYS records them in a mailbox you can read at https://localhost:8090, saved to
     MAILBOX_FILE (a docker volume) so it survives the container being recreated
  2. If SMTP is configured, ALSO delivers them for real to the recipient's inbox

SMTP is configured purely through environment variables (see .env.example). If it is
not configured the service says so plainly on the mailbox page and in each message's
delivery status — so it is obvious why nothing arrived in a real inbox.
"""
import json
import os
import re
import ssl
import smtplib
import threading
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as email_policy
from collections import deque
from datetime import datetime, timedelta, timezone

# Mailbox times are shown in Singapore time (UTC+8, no daylight saving).
SGT = timezone(timedelta(hours=8), "SGT")

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
MAILBOX_FILE = os.environ.get("MAILBOX_FILE", "").strip()
_mailbox_lock = threading.Lock()   # Flask threads + the SMTP listener thread


def load_mailbox():
    """Reload the saved mailbox (newest first) after a restart. A missing or
    unreadable file just means an empty mailbox."""
    if not MAILBOX_FILE or not os.path.exists(MAILBOX_FILE):
        return
    try:
        with open(MAILBOX_FILE, encoding="utf-8") as f:
            MAILBOX.extend(json.load(f))
        print(f"[email] loaded {len(MAILBOX)} message(s) from {MAILBOX_FILE}", flush=True)
    except (OSError, ValueError) as e:
        print(f"[email] could not read {MAILBOX_FILE}: {e}", flush=True)


def save_mailbox():
    """Write the mailbox atomically. Call with _mailbox_lock held."""
    if not MAILBOX_FILE:
        return
    tmp = MAILBOX_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(MAILBOX), f)
        os.replace(tmp, MAILBOX_FILE)
    except OSError as e:
        print(f"[email] could not save {MAILBOX_FILE}: {e}", flush=True)


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


def record(to, subject, text, source):
    """Store in the local mailbox and, if SMTP is configured, deliver for real."""
    status, detail = deliver(to, subject, text)
    with _mailbox_lock:
        MAILBOX.appendleft({
            "to": to, "subject": subject, "body": text, "source": source,
            "status": status, "detail": detail,
            "at": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
        })
        save_mailbox()
    print(f"[email] {status}: {to} — {subject} ({detail})", flush=True)
    return status, detail


@app.post("/send")
def send():
    body = request.get_json(silent=True) or {}
    status, detail = record(body.get("to", "(unknown)"), body.get("subject", "(no subject)"),
                            body.get("body", ""), "backend")
    return jsonify(ok=True, status=status, detail=detail)


# --------------------------------------------------------------------------- #
# SMTP listener for Keycloak (password-reset and other account e-mails). Keycloak
# speaks SMTP, not our JSON API, so it relays here on the internal network; the
# message lands in the same mailbox and is forwarded if real SMTP is configured.
# --------------------------------------------------------------------------- #
SMTP_LISTEN_PORT = int(os.environ.get("SMTP_LISTEN_PORT", "1025"))


class _KeycloakMailHandler:
    async def handle_DATA(self, server, session, envelope):
        msg = message_from_bytes(envelope.content, policy=email_policy)
        part = msg.get_body(preferencelist=("plain", "html"))
        text = part.get_content() if part else ""
        for rcpt in envelope.rcpt_tos:
            record(rcpt, str(msg.get("Subject", "(no subject)")), text, "keycloak")
        return "250 Message accepted for delivery"


def start_smtp_listener():
    from aiosmtpd.controller import Controller
    controller = Controller(_KeycloakMailHandler(), hostname="0.0.0.0", port=SMTP_LISTEN_PORT)
    controller.start()
    print(f"[email] SMTP listener for Keycloak on port {SMTP_LISTEN_PORT}", flush=True)
    return controller


@app.get("/api/inbox")
def inbox():
    return jsonify(list(MAILBOX))


@app.post("/api/clear")
def clear():
    with _mailbox_lock:
        MAILBOX.clear()
        save_mailbox()
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
    load_mailbox()
    start_smtp_listener()
    app.run(host="0.0.0.0", port=5000, ssl_context=ssl_context())
