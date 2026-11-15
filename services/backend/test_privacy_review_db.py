"""Disposable database-backed test of the document privacy exception workflow."""
import base64
import os
from pathlib import Path

if os.environ.get("PRIVACY_REVIEW_TEST_DB") != "yes":
    raise SystemExit("Refusing to run: set PRIVACY_REVIEW_TEST_DB=yes for the local POC database.")

from app import db, document_privacy


IMAGE_PDF = Path(os.environ.get("PDPA_IMAGE_PDF", "/tmp/pdpa-image-only-test-document.pdf"))
TEXT_PDF = Path(os.environ.get("PDPA_TEXT_PDF", "/tmp/pdpa-synthetic-test-document.pdf"))
request_id = None


def insert_document(raw, name):
    return db.query(
        """INSERT INTO request_documents
             (request_id,filename,content_base64,scan_status,uploaded_at)
           VALUES (%s,%s,%s,'pending',now()-interval '1 minute') RETURNING id""",
        (request_id, name, base64.b64encode(raw).decode("ascii")), fetch="one")["id"]


try:
    image_raw, text_raw = IMAGE_PDF.read_bytes(), TEXT_PDF.read_bytes()
    requestor = db.query("SELECT id,role FROM employees WHERE username='req'", fetch="one")
    target = db.query("SELECT id FROM employees WHERE username='grace'", fetch="one")
    it_admin = db.query("SELECT id,role FROM employees WHERE username='itadmin'", fetch="one")
    service = db.query("SELECT id FROM services WHERE key='service2'", fetch="one")
    request_id = db.query(
        """INSERT INTO requests
             (type,requestor_id,target_employee_id,service_id,status,justification)
           VALUES ('grant',%s,%s,%s,'pending_hr','Privacy review integration test')
           RETURNING id""",
        (requestor["id"], target["id"], service["id"]), fetch="one")["id"]

    ocr_id = insert_document(image_raw, IMAGE_PDF.name)
    assert document_privacy.process_document(ocr_id)
    ocr = db.query(
        "SELECT scan_status,scan_method,ocr_confidence,pii_summary FROM request_documents WHERE id=%s",
        (ocr_id,), fetch="one")
    assert ocr["scan_status"] == "redacted", ocr
    assert ocr["scan_method"] == "ocr" and ocr["ocr_confidence"] is not None, ocr
    assert ocr["pii_summary"] == {"address": 1, "email": 1, "nric_fin": 1, "phone": 1}, ocr

    normal_threshold = document_privacy.OCR_MIN_CONFIDENCE
    document_privacy.OCR_MIN_CONFIDENCE = 99
    low_confidence = document_privacy.scan_pdf(image_raw)
    document_privacy.OCR_MIN_CONFIDENCE = normal_threshold
    assert low_confidence[0] == "review_required" and low_confidence[1] is None
    assert low_confidence[4]["ocr_confidence"] is not None, low_confidence[4]

    document_id = insert_document(b"%PDF-1.4 deliberately broken", "unreadable-test.pdf")
    assert document_privacy.process_document(document_id)
    state = db.query("SELECT scan_status FROM request_documents WHERE id=%s",
                     (document_id,), fetch="one")["scan_status"]
    assert state == "failed", state
    assert any(row["id"] == document_id for row in document_privacy.review_items())

    row = document_privacy.request_replacement(
        document_id, it_admin["id"], "Upload a searchable or sanitised PDF")
    assert row and "replacement requested" in document_privacy.approval_blocker(request_id)

    replacement = {"filename": TEXT_PDF.name,
                   "content_base64": base64.b64encode(text_raw).decode("ascii")}
    assert document_privacy.replace_document(
        document_id, {"id": -1, "role": "requestor"}, replacement) is None
    row = document_privacy.replace_document(document_id, requestor, replacement)
    assert row and "pending" in document_privacy.approval_blocker(request_id)
    db.query("UPDATE request_documents SET uploaded_at=now()-interval '1 minute' WHERE id=%s",
             (document_id,), fetch=None)
    assert document_privacy.process_document(document_id)
    state = db.query("SELECT scan_status FROM request_documents WHERE id=%s",
                     (document_id,), fetch="one")["scan_status"]
    assert state == "redacted", state
    assert document_privacy.approval_blocker(request_id) is None

    removable_id = insert_document(b"%PDF-1.4 another broken fixture", "unneeded-broken.pdf")
    assert document_privacy.process_document(removable_id)
    assert document_privacy.retry_document(removable_id, it_admin["id"])
    db.query("UPDATE request_documents SET uploaded_at=now()-interval '1 minute' WHERE id=%s",
             (removable_id,), fetch=None)
    assert document_privacy.process_document(removable_id)
    row = document_privacy.remove_document(
        removable_id, it_admin["id"], "Attachment is not required for the access decision")
    assert row and "replacement requested" in document_privacy.approval_blocker(request_id)
    removed = db.query(
        "SELECT scan_status,content_base64,review_note FROM request_documents WHERE id=%s",
        (removable_id,), fetch="one")
    assert removed["scan_status"] == "replacement_requested" and removed["content_base64"] is None
    assert removed["review_note"]

    row = document_privacy.replace_document(removable_id, requestor, replacement)
    assert row
    db.query("UPDATE request_documents SET uploaded_at=now()-interval '1 minute' WHERE id=%s",
             (removable_id,), fetch=None)
    assert document_privacy.process_document(removable_id)
    assert document_privacy.approval_blocker(request_id) is None

    previous_policy = db.query(
        "SELECT retention_seconds FROM document_retention_policies WHERE outcome='rejected'",
        fetch="one")["retention_seconds"]
    assert document_privacy.update_retention_policy("rejected", 180, it_admin["id"])
    assert db.query(
        "SELECT retention_seconds FROM document_retention_policies WHERE outcome='rejected'",
        fetch="one")["retention_seconds"] == 180
    document_privacy.update_retention_policy("rejected", previous_policy, it_admin["id"])

    db.query(
        "UPDATE requests SET status='rejected',closed_at=now()-interval '5 minutes' WHERE id=%s",
        (request_id,), fetch=None)
    assert document_privacy.apply_legal_hold(ocr_id, "Integration test hold", it_admin["id"])
    document_privacy.schedule_retention()
    document_privacy.purge_due()
    held = db.query("SELECT scan_status,legal_hold FROM request_documents WHERE id=%s",
                    (ocr_id,), fetch="one")
    assert held["legal_hold"] and held["scan_status"] != "purged", held
    assert document_privacy.release_legal_hold(
        ocr_id, "Integration test completed", it_admin["id"])
    document_privacy.purge_due()
    assert db.query("SELECT scan_status FROM request_documents WHERE id=%s",
                    (ocr_id,), fetch="one")["scan_status"] == "purged"

    print("PASS  image-only document was OCR-redacted with confidence metadata")
    print("PASS  OCR below the configured confidence threshold failed closed")
    print("PASS  unreadable document entered the privacy-review queue")
    print("PASS  replacement request kept HR approval blocked")
    print("PASS  non-owning Requestor could not replace the document")
    print("PASS  owning Requestor replacement was rescanned and redacted")
    print("PASS  safe replacement automatically unblocked approval")
    print("PASS  IT Admin retry returned the document to protected scanning")
    print("PASS  justified removal deleted the unsafe copy and kept approval blocked")
    print("PASS  approval unblocked only after the removed copy was replaced and scanned")
    print("PASS  retention policy update was validated and restored")
    print("PASS  legal hold prevented purge until its audited release")
finally:
    if request_id is not None:
        db.query("DELETE FROM requests WHERE id=%s", (request_id,), fetch=None)
