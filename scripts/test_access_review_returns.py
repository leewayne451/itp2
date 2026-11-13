"""End-to-end check for governed access-review removals on a fresh demo stack.

It creates Sales and Engineering campaigns, certifies Bob's Account, proves a
rejected return reopens an item, then takes Alice's Laptop return through the
normal approval, appointment and EUSS retrieval workflow. It changes demo data.
"""
from datetime import datetime, timedelta, timezone
import sys

from _e2e import audit_missing, check, get, post, session_for, summary


def _item(campaign, username, resource):
    return next((item for item in campaign["items"]
                 if item["employee_username"] == username and item["service_name"] == resource), None)


def _campaign(it, department_id, director_id, name):
    due = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    return post("/api/access-reviews", {"name": name, "department_id": department_id,
                                         "reviewer_id": director_id, "due_at": due}, it)


def main():
    # session_for deliberately prepares these demo accounts; use a disposable stack.
    tokens = {user: session_for(user)[1] for user in ("itadmin", "director", "hr", "aa", "req", "euss")}
    it, director = tokens["itadmin"], tokens["director"]
    employees = {e["username"]: e for e in get("/api/employees", it)[1]}
    departments = {d["name"]: d["id"] for d in get("/api/departments", it)[1]}

    code, sales = _campaign(it, departments["Sales"], employees["director"]["id"], "Sales recertification E2E")
    check("IT Admin starts a Sales campaign", code == 201, sales)
    if code != 201:
        return summary()

    bob_account = _item(sales, "bob", "Account")
    check("Sales campaign includes Bob's Account", bob_account is not None)
    if bob_account:
        code, result = post(f"/api/access-review-items/{bob_account['id']}/decision",
                            {"decision": "keep", "reason": "Manager recertified business need"}, director)
        check("Director certifies Bob's Account", code == 200 and result.get("decision") == "keep", result)

    # A rejected governed return must leave the item unresolved and available for review again.
    bob_laptop = _item(sales, "bob", "Personal Laptop")
    if bob_laptop:
        code, result = post(f"/api/access-review-items/{bob_laptop['id']}/decision",
                            {"decision": "revoke", "reason": "E2E rejection-path check"}, director)
        reject_id = result.get("return_request_id")
        check("Review removal creates a return instead of revoking immediately",
              code == 200 and reject_id and bob_laptop["actual_access"], result)
        if reject_id:
            post(f"/api/requests/{reject_id}/reject", {"reason": "E2E deliberate rejection"}, tokens["hr"])
            _, refreshed = get(f"/api/access-reviews/{sales['id']}", it)
            reopened = next(i for i in refreshed["items"] if i["id"] == bob_laptop["id"])
            check("Rejected return reopens the review item", reopened["decision"] is None, reopened)

    code, engineering = _campaign(it, departments["Engineering"], employees["director"]["id"],
                                  "Engineering recertification E2E")
    check("IT Admin starts an Engineering campaign", code == 201, engineering)
    if code != 201:
        return summary()

    alice_laptop = _item(engineering, "alice", "Personal Laptop")
    check("Engineering campaign includes Alice's Laptop", alice_laptop is not None)
    if not alice_laptop:
        return summary()
    code, result = post(f"/api/access-review-items/{alice_laptop['id']}/decision",
                        {"decision": "revoke", "reason": "Laptop no longer required"}, director)
    return_id = result.get("return_request_id")
    check("Alice's removal creates a linked automated return", code == 200 and return_id, result)
    _, current = get(f"/api/access-reviews/{engineering['id']}", it)
    current_alice = next(i for i in current["items"] if i["id"] == alice_laptop["id"])
    check("Alice retains access until the governed return completes",
          current_alice["actual_access"] and current_alice["return_status"] == "pending_hr", current_alice)

    if return_id:
        for user in ("hr", "director", "aa"):
            post(f"/api/requests/{return_id}/approve", {}, tokens[user])
        _, processed = post(f"/api/requests/{return_id}/process", {}, tokens["itadmin"])
        if processed.get("status") == "pending_appointment":   # otherwise auto-booked (CHANGE 011)
            appointment = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
            post(f"/api/requests/{return_id}/appointment", {"appointment_at": appointment}, tokens["req"])
        code, completed = post(f"/api/requests/{return_id}/retrieve", {}, tokens["euss"])
        check("EUSS completes Alice's governed return", code == 200 and completed.get("status") == "completed", completed)
        # Campaign items are point-in-time evidence; reconciliation refreshes the
        # actual-access snapshot after the standard return workflow changes it.
        code, _ = post(f"/api/access-reviews/{engineering['id']}/reconcile", {}, it)
        check("IT reconciles the completed return into the campaign", code == 200)
        _, current = get(f"/api/access-reviews/{engineering['id']}", it)
        current_alice = next(i for i in current["items"] if i["id"] == alice_laptop["id"])
        check("Completed return remediates Alice's review item",
              not current_alice["actual_access"] and current_alice["remediation_status"] == "remediated", current_alice)

    missing = audit_missing(it, ["access_review_created", "access_review_item_keep",
                                 "access_review_item_revoke", "access_review_return_rejected"])
    check("Review certifications and governed returns are audited", not missing, missing)
    return summary()


if __name__ == "__main__":
    sys.exit(main())
