"""Best-effort notifications to the email docker (never breaks a request)."""
import requests

from .config import EMAIL_URL, verify


def send_email(to, subject, body):
    if not to:
        return
    try:
        requests.post(f"{EMAIL_URL}/send", json={"to": to, "subject": subject, "body": body},
                      timeout=3, verify=verify())
    except Exception:  # noqa: BLE001 — email is best-effort
        pass
