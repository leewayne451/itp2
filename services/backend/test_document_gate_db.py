"""Disposable database-backed check for the document approval gate.

Run only against the local POC database:

    DOCUMENT_GATE_TEST_DB=yes python test_document_gate_db.py

The temporary request and document are deleted in ``finally``.
"""
import os

if os.environ.get("DOCUMENT_GATE_TEST_DB") != "yes":
    raise SystemExit("Refusing to run: set DOCUMENT_GATE_TEST_DB=yes for the local POC database.")

from flask import g

from app import create_app, db
from app.api import requests as requests_api


request_id = None
try:
    requestor = db.query("SELECT id FROM employees WHERE username='req'", fetch="one")
    target = db.query("SELECT id FROM employees WHERE username='grace'", fetch="one")
    hr = db.query("SELECT id, role FROM employees WHERE username='hr'", fetch="one")
    service = db.query("SELECT id FROM services WHERE key='service2'", fetch="one")
    if not all((requestor, target, hr, service)):
        raise AssertionError("Required seed accounts or service are missing")

    request_id = db.query(
        """INSERT INTO requests
             (type, requestor_id, target_employee_id, service_id, status, justification)
           VALUES ('grant', %s, %s, %s, 'pending_hr', 'PDPA gate integration test')
           RETURNING id""",
        (requestor["id"], target["id"], service["id"]), fetch="one")["id"]
    db.query(
        """INSERT INTO request_documents
             (request_id, filename, content_base64, scan_status)
           VALUES (%s, 'gate-test.pdf', 'JVBERi0xLjQ=', 'pending')""",
        (request_id,), fetch=None)

    app = create_app()
    with app.test_request_context(f"/api/requests/{request_id}/approve", method="POST"):
        g.user = {"id": hr["id"], "role": hr["role"]}
        response, status = requests_api.approve.__wrapped__(request_id)
        error = response.get_json()["error"]

    current = db.query("SELECT status FROM requests WHERE id=%s", (request_id,), fetch="one")
    assert status == 409, (status, error)
    assert current["status"] == "pending_hr", current
    assert "not ready for approval" in error, error
    print(f"PASS  HR approval blocked with HTTP 409 for quarantined request #{request_id}")
finally:
    if request_id is not None:
        db.query("DELETE FROM requests WHERE id=%s", (request_id,), fetch=None)
