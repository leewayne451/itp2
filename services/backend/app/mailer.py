"""Best-effort notifications to the email docker (never breaks a request)."""
import requests

from .config import EMAIL_URL, verify


def send_email(to, subject, body):
    """True once the email service has the message (it is then in the mailbox,
    whether or not real SMTP delivery is configured). False when the email service
    could not be reached at all."""
    if not to:
        return False
    try:
        r = requests.post(f"{EMAIL_URL}/send", json={"to": to, "subject": subject, "body": body},
                          timeout=3, verify=verify())
        return r.ok
    except requests.exceptions.ReadTimeout:
        # The request reached the email service, which is still relaying it over real
        # SMTP (can take several seconds); it records the message when that finishes.
        return True
    except Exception as e:  # noqa: BLE001 — email is best-effort
        print(f"[email] could not reach the email service for {to}: {type(e).__name__}", flush=True)
        return False
