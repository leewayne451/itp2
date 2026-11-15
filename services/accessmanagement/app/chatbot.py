"""Local intent handling and workflow guidance for the signed-in chat assistant."""
import re

from .labels import STATUS_LABEL, TYPE_LABEL


def intent_for(message):
    text = " ".join(message.casefold().split()).strip("!?.")
    if text in {"hi", "hi there", "hello", "hello there", "hey", "good morning",
                "good afternoon", "good evening"}:
        return "greeting"
    if "policy" in text or "mnd" in text:
        return "policy"
    if any(term in text for term in ("status", "progress", "track", "where is my", "where's my")):
        return "status"
    if (re.search(r"\bmy\b.*\brequest\b", text)
            or re.search(r"\bwhere\b.*\brequest\b", text)
            or re.search(r"\brequest\s*#\d+\b", text)):
        return "status"
    if any(term in text for term in (
            "revoke", "revocation", "remove access", "offboard", "return", "request a return")):
        return "return"
    if any(term in text for term in ("onboard", "onboarding", "new hire", "new starter")):
        return "onboard"
    if any(term in text for term in ("approval", "approver", "workflow", "approval steps")):
        return "workflow"
    if any(term in text for term in ("request access", "grant access", "service one", "laptop access")):
        return "grant"
    return "help"


def request_id_for(message):
    match = re.search(r"(?:\brequest\s*)?#(\d+)\b", message, re.IGNORECASE)
    return int(match.group(1)) if match else None


def guidance_for(intent, role):
    can_submit = role == "requestor"
    if intent == "greeting":
        return ("Hi! How may i help you?")
    if intent == "policy":
        return ("This prototype does not have MND HR policy documents loaded, so I can't give "
                "an authoritative policy answer. Please check with HR.")
    if intent == "grant":
        if can_submit:
            return ("Choose the employee and service (for example, Service One / Personal Laptop) "
                    "in the Grant form, then submit it. The request goes to HR, Director, "
                    "Approving Authority, and IT Admin; after IT processes "
                    "it, the requestor books an appointment and EUSS provisions the resource.")
        return ("The existing workflow allows a Requestor to submit access requests. Ask a "
                "Requestor to raise a Grant for you; it then follows HR, Director, Approving "
                "Authority, IT Admin, appointment booking, and EUSS provisioning.")
    if intent == "return":
        if can_submit:
            return ("Use the Return form for a service the employee currently has. It follows HR, "
                    "Director, Approving Authority, and IT Admin; the requestor then books an "
                    "appointment. EUSS retrieves the asset and revokes access at that step, so "
                    "access is not revoked immediately when the request is submitted.")
        return ("A Requestor must submit a Return request for a service the employee currently "
                "has. Access is revoked by EUSS during retrieval after the approval and "
                "appointment steps; this chat does not revoke access directly.")
    if intent == "onboard":
        if can_submit:
            return ("For an employee who has no department, choose them in the Onboard form, "
                    "select the department, and submit. The request follows HR, Director, "
                    "Approving Authority, IT Admin, appointment booking, and EUSS provisioning.")
        return ("A Requestor can start onboarding for an employee who has no department by "
                "selecting the employee and destination department in the Onboard form.")
    if intent == "workflow":
        return ("The standard request path is HR approval → Director approval → Approving "
                "Authority approval → IT Admin processing → requestor books an appointment → "
                "EUSS provisions or retrieves the asset. A Return request revokes access at "
                "EUSS retrieval.")
    return ("I can check requests linked to your signed-in account, explain access requests, "
            "onboarding, returns, and approval steps. I don't have MND HR policy documents "
            "loaded for policy-specific answers.")


def action_for(intent, role, dashboard_url):
    if role != "requestor":
        return []
    forms = {
        "grant": ("Open access request form", "grant-request"),
        "return": ("Open return request form", "return-request"),
        "onboard": ("Open onboarding form", "onboard-request"),
    }
    form = forms.get(intent)
    if not form:
        return []
    label, anchor = form
    return [{"label": label, "url": f"{dashboard_url}#{anchor}"}]


def status_reply(rows, request_id=None):
    if request_id is not None:
        rows = [row for row in rows if row["id"] == request_id]
        if not rows:
            return (f"I couldn't find request #{request_id} among requests submitted by you or "
                    "about you. I can only show requests linked to your signed-in account.")
    if not rows:
        return ("I couldn't find any requests submitted by you or about you. If a Requestor "
                "submitted one for you, make sure it was created in ServiceJourney.")

    lines = []
    for row in rows[:5]:
        kind = TYPE_LABEL.get(row["type"], row["type"].title())
        status = STATUS_LABEL.get(row["status"], row["status"])
        if row["type"] == "onboard":
            resource = f"onboarding to {row.get('new_department_name') or 'a department'}"
        elif row["type"] == "shift":
            source = row.get("source_department_name") or "current department"
            destination = row.get("new_department_name") or "new department"
            resource = f"shift from {source} to {destination}"
        else:
            resource = row.get("service_name") or "service"
        target = f" for {row['target_name']}" if row.get("target_name") else ""
        lines.append(f"Request #{row['id']} — {kind}{target}, {resource}: {status}.")

    if len(rows) > 5:
        lines.append(f"Showing the 5 most recently updated of {len(rows)} requests linked to you.")
    return "\n".join(lines)
