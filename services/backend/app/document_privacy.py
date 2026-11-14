"""Personal-data detection, true PDF redaction and retention (CHANGE 012).

Only sanitised content is released to workflow users. Text PDFs use native PDF
redaction. Pages containing images are OCR-scanned, redacted by coordinate,
flattened, and OCR-verified; uncertain results fail closed to manual review.
"""
import base64
import hashlib
from io import BytesIO
import json
import os
import re
import threading
import time

from . import audit, db


SCAN_INTERVAL = max(1, int(os.environ.get("DOCUMENT_SCAN_INTERVAL_SECONDS", "3")))
MIN_QUARANTINE_SECONDS = max(0, int(os.environ.get("DOCUMENT_MIN_QUARANTINE_SECONDS", "0")))
RETENTION_SECONDS = max(60, int(os.environ.get("DOCUMENT_RETENTION_SECONDS", "2592000")))
PURGE_INTERVAL = max(5, int(os.environ.get("DOCUMENT_PURGE_INTERVAL_SECONDS", "30")))
OCR_DPI = max(150, int(os.environ.get("DOCUMENT_OCR_DPI", "200")))
OCR_MIN_CONFIDENCE = min(100, max(0, int(os.environ.get("DOCUMENT_OCR_MIN_CONFIDENCE", "60"))))
SAFE_STATUSES = ("clean", "redacted")
APPROVAL_READY_STATUSES = SAFE_STATUSES
REVIEW_STATUSES = ("review_required", "failed", "replacement_requested")

NRIC_RE = re.compile(r"\b[STFG][\s-]?\d[\s-]?\d[\s-]?\d[\s-]?\d[\s-]?\d[\s-]?\d[\s-]?\d[\s-]?[A-Z]\b", re.I)
PHONE_RE = re.compile(r"(?<!\d)(?:\+65[\s-]?)?[689]\d{3}[\s-]?\d{4}(?!\d)")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
ADDRESS_RE = re.compile(
    r"\b\d{1,4}\s+[A-Z0-9][A-Z0-9 .,'#/-]{2,70}\s+"
    r"(?:ROAD|RD|STREET|ST|AVENUE|AVE|LANE|LN|DRIVE|DR|CRESCENT|CLOSE|WAY|WALK|"
    r"TERRACE|PLACE|BOULEVARD|JALAN|LORONG)\b(?:[^\n]{0,45})?"
    r"(?:SINGAPORE\s+\d{6}|\b\d{6}\b)?", re.I)

_CHECKSUM = {"S": "JZIHGFEDCBA", "F": "JZIHGFEDCBA",
             "T": "GFEDCBAJZIH", "G": "GFEDCBAJZIH"}
_WEIGHTS = (2, 7, 6, 5, 4, 3, 2)


class PrivacyError(Exception):
    pass


def compact_identifier(value):
    return re.sub(r"[\s-]", "", value).upper()


def valid_nric_fin(value):
    value = compact_identifier(value)
    if not re.fullmatch(r"[STFG]\d{7}[A-Z]", value):
        return False
    total = sum(int(d) * w for d, w in zip(value[1:8], _WEIGHTS))
    return _CHECKSUM[value[0]][total % 11] == value[-1]


def find_matches(text):
    """Return classified matches. Raw values are used only in memory for locating
    redaction rectangles and must never be logged or stored as scan metadata.
    """
    found = []
    for match in NRIC_RE.finditer(text or ""):
        if valid_nric_fin(match.group()):
            found.append({"kind": "nric_fin", "value": match.group()})
    for kind, pattern in (("phone", PHONE_RE), ("email", EMAIL_RE), ("address", ADDRESS_RE)):
        found.extend({"kind": kind, "value": m.group().strip()} for m in pattern.finditer(text or ""))
    # A deterministic order makes tests and audit counts reproducible.
    return sorted(found, key=lambda x: (x["kind"], x["value"]))


def summary_of(matches):
    summary = {}
    for item in matches:
        summary[item["kind"]] = summary.get(item["kind"], 0) + 1
    return summary


def _ocr_image(image):
    """Return redaction boxes, classified matches, confidence and OCR text.

    Raw OCR text and matched values remain in memory only. Persisted metadata is
    limited to counts and aggregate confidence.
    """
    import pytesseract
    from pytesseract import Output

    data = pytesseract.image_to_data(image, output_type=Output.DICT, config="--psm 6")
    lines = {}
    confidences = []
    for i, raw_word in enumerate(data["text"]):
        word = (raw_word or "").strip()
        try:
            confidence = float(data["conf"][i])
        except (TypeError, ValueError):
            confidence = -1
        if not word or confidence < 0:
            continue
        confidences.append(confidence)
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append({
            "text": word, "confidence": confidence,
            "box": (data["left"][i], data["top"][i],
                    data["left"][i] + data["width"][i],
                    data["top"][i] + data["height"][i]),
        })

    boxes, matches, extracted = [], [], []
    for words in lines.values():
        words.sort(key=lambda item: item["box"][0])
        line, spans = "", []
        for word in words:
            if line:
                line += " "
            start = len(line)
            line += word["text"]
            spans.append((start, len(line), word))
        extracted.append(line)
        for match in find_matches(line):
            start = line.find(match["value"])
            end = start + len(match["value"])
            selected = [word for left, right, word in spans if left < end and right > start]
            if not selected:
                continue
            x0 = min(word["box"][0] for word in selected)
            y0 = min(word["box"][1] for word in selected)
            x1 = max(word["box"][2] for word in selected)
            y1 = max(word["box"][3] for word in selected)
            boxes.append((max(0, x0 - 4), max(0, y0 - 4), x1 + 4, y1 + 4))
            matches.append(match)
    average = round(sum(confidences) / len(confidences), 2) if confidences else None
    return boxes, matches, average, "\n".join(extracted)


def scan_pdf(raw):
    """Return ``(status, protected_bytes, summary, error, scan_metadata)``.

    PyMuPDF's redaction annotations remove the underlying text; they are not
    cosmetic black overlays. OCR pages are flattened after drawing the redaction
    boxes so the original image pixels are absent from the protected derivative.
    """
    import fitz  # lazy: pure detector tests do not require the PDF dependency
    from PIL import Image, ImageDraw

    try:
        document = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:
        raise PrivacyError(f"PDF could not be opened: {type(exc).__name__}") from exc
    if document.page_count == 0:
        raise PrivacyError("PDF contains no pages")

    all_matches, unresolved, low_confidence_pages = [], [], []
    ocr_images, ocr_confidences = {}, []
    scale = OCR_DPI / 72
    for page_index, page in enumerate(document):
        if page.get_images(full=True):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            image = Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGB")
            boxes, matches, confidence, _ = _ocr_image(image)
            if confidence is not None:
                ocr_confidences.append(confidence)
            if confidence is None or confidence < OCR_MIN_CONFIDENCE:
                low_confidence_pages.append(page_index + 1)
            draw = ImageDraw.Draw(image)
            for box in boxes:
                draw.rectangle(box, fill="black")
            ocr_images[page_index] = image
            all_matches.extend(matches)
        else:
            text = page.get_text("text") or ""
            matches = find_matches(text)
            all_matches.extend(matches)
            for item in matches:
                rectangles = page.search_for(item["value"])
                if not rectangles:
                    unresolved.append(item["kind"])
                    continue
                for rectangle in rectangles:
                    page.add_redact_annot(rectangle, fill=(0, 0, 0))

    summary = summary_of(all_matches)
    confidence = (round(sum(ocr_confidences) / len(ocr_confidences), 2)
                  if ocr_confidences else None)
    metadata = {"method": "ocr" if ocr_images else "text",
                "ocr_confidence": confidence, "ocr_pages": len(ocr_images)}
    if low_confidence_pages:
        document.close()
        pages = ", ".join(map(str, low_confidence_pages))
        return ("review_required", None, summary,
                f"OCR confidence is below {OCR_MIN_CONFIDENCE}% on page(s) {pages}", metadata)
    if unresolved:
        document.close()
        return ("review_required", None, summary,
                "Some detected values could not be safely located for redaction", metadata)

    if all_matches and not ocr_images:
        for page in document:
            page.apply_redactions()
    if ocr_images:
        # Apply any native-text redactions first, then rebuild OCR pages from the
        # redacted raster. This permanently removes the source image pixels.
        for page in document:
            page.apply_redactions()
        flattened = fitz.open()
        for page_index, page in enumerate(document):
            if page_index not in ocr_images:
                flattened.insert_pdf(document, from_page=page_index, to_page=page_index)
                continue
            image_bytes = BytesIO()
            ocr_images[page_index].save(image_bytes, format="PNG")
            new_page = flattened.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, stream=image_bytes.getvalue())
        protected = flattened.tobytes(garbage=4, deflate=True, clean=True)
        flattened.close()
    elif all_matches:
        protected = document.tobytes(garbage=4, deflate=True, clean=True)
    else:
        protected = raw
    if all_matches or ocr_images:
        document.close()
        # Verify both the extractable text and every rasterised page.
        check = fitz.open(stream=protected, filetype="pdf")
        remaining = find_matches("\n".join(page.get_text("text") or "" for page in check))
        for page in check:
            if page.get_images(full=True):
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                image = Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGB")
                _, ocr_remaining, _, _ = _ocr_image(image)
                remaining.extend(ocr_remaining)
        check.close()
        if remaining:
            return ("review_required", None, summary,
                    "Post-redaction verification found recoverable personal data", metadata)
        return ("redacted" if all_matches else "clean", protected, summary, None, metadata)

    document.close()
    return "clean", raw, {}, None, metadata


def _audit(action, doc_id, request_id, details=None, outcome="success"):
    audit.record("audit", action, outcome=outcome, actor={"username": "privacy-worker", "role": "system"},
                 target_type="document", target_id=doc_id, target_label=f"document #{doc_id}",
                 details={"request_id": request_id, **(details or {})})


def process_document(doc_id):
    claimed = db.query(
        """UPDATE request_documents SET scan_status='scanning', scan_error=NULL
             WHERE id=%s AND scan_status='pending'
             RETURNING id, request_id, content_base64""", (doc_id,), fetch="one")
    if not claimed:
        return False
    _audit("document_scan_started", doc_id, claimed["request_id"])
    try:
        raw = base64.b64decode(claimed["content_base64"] or "", validate=True)
        source_hash = hashlib.sha256(raw).hexdigest()
        status, protected, summary, error, metadata = scan_pdf(raw)
        if status == "review_required":
            db.query(
                """UPDATE request_documents SET scan_status=%s, pii_summary=%s::jsonb,
                     source_sha256=%s, scan_error=%s, scanned_at=now(), retention_seconds=%s,
                     scan_method=%s,ocr_confidence=%s
                   WHERE id=%s""",
                (status, json.dumps(summary), source_hash, error, RETENTION_SECONDS,
                 metadata["method"], metadata["ocr_confidence"], doc_id), fetch=None)
            _audit("document_review_required", doc_id, claimed["request_id"],
                   {"detected_types": summary, "reason": error,
                    "scan_method": metadata["method"],
                    "ocr_confidence": metadata["ocr_confidence"]}, outcome="warning")
            return True
        encoded = base64.b64encode(protected).decode("ascii")
        content_hash = hashlib.sha256(protected).hexdigest()
        db.query(
            """UPDATE request_documents SET content_base64=%s, scan_status=%s,
                 pii_summary=%s::jsonb, source_sha256=%s, content_sha256=%s,
                 scan_error=NULL, scanned_at=now(), retention_seconds=%s,
                 scan_method=%s,ocr_confidence=%s WHERE id=%s""",
            (encoded, status, json.dumps(summary), source_hash, content_hash,
             RETENTION_SECONDS, metadata["method"], metadata["ocr_confidence"], doc_id), fetch=None)
        _audit("document_redacted" if status == "redacted" else "document_clean",
               doc_id, claimed["request_id"],
               {"detected_types": summary, "content_sha256": content_hash,
                "scan_method": metadata["method"],
                "ocr_confidence": metadata["ocr_confidence"]})
        return True
    except Exception as exc:  # fail closed; never release the unscanned original
        message = f"{type(exc).__name__}: {str(exc)[:300]}"
        db.query(
            """UPDATE request_documents SET scan_status='failed', scan_error=%s,
                 scanned_at=now(), retention_seconds=%s WHERE id=%s""",
            (message, RETENTION_SECONDS, doc_id), fetch=None)
        _audit("document_scan_failed", doc_id, claimed["request_id"], {"error": message}, outcome="failure")
        return True


def process_pending(limit=10):
    rows = db.query(
        """SELECT id FROM request_documents
            WHERE scan_status='pending'
              AND uploaded_at <= now() - (%s * interval '1 second')
            ORDER BY id LIMIT %s""", (MIN_QUARANTINE_SECONDS, limit))
    for row in rows:
        process_document(row["id"])
    return len(rows)


def schedule_retention():
    return db.query(
        """UPDATE request_documents d SET
             retention_seconds=p.retention_seconds,
             retain_until=r.closed_at + (p.retention_seconds * interval '1 second')
            FROM requests r JOIN document_retention_policies p ON p.outcome=r.status
           WHERE d.request_id=r.id AND r.closed_at IS NOT NULL
             AND d.retain_until IS NULL AND d.purged_at IS NULL
           RETURNING d.id""", fetch="all")


def purge_due():
    rows = db.query(
        """SELECT d.id, d.request_id FROM request_documents d
            WHERE d.retain_until IS NOT NULL AND d.retain_until <= now()
              AND d.legal_hold=FALSE AND d.purged_at IS NULL""")
    for row in rows:
        db.query(
            """UPDATE request_documents SET content_base64=NULL, filename='purged-document.pdf',
                 scan_status='purged', purged_at=now(), scan_error=NULL WHERE id=%s""",
            (row["id"],), fetch=None)
        _audit("document_purged", row["id"], row["request_id"])
    return len(rows)


def privacy_dashboard():
    stats = db.query(
        """SELECT COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE scan_status='redacted') AS redacted,
                  COUNT(*) FILTER (WHERE scan_status='clean') AS clean,
                  COUNT(*) FILTER (WHERE scan_status = ANY(%s)) AS awaiting_review,
                  COUNT(*) FILTER (WHERE scan_status='replacement_requested') AS replacements,
                  COUNT(*) FILTER (WHERE scan_status='purged') AS purged,
                  COUNT(*) FILTER (WHERE legal_hold) AS legal_holds,
                  COUNT(*) FILTER (WHERE retain_until<=now() AND purged_at IS NULL
                                     AND legal_hold=FALSE) AS overdue
             FROM request_documents""", (list(REVIEW_STATUSES),), fetch="one")
    policies = db.query(
        """SELECT p.outcome,p.label,p.retention_seconds,p.updated_at,e.full_name AS updated_by_name
             FROM document_retention_policies p
             LEFT JOIN employees e ON e.id=p.updated_by_id ORDER BY p.outcome""")
    retention = db.query(
        """SELECT d.id,d.request_id,d.filename,d.scan_status,d.retain_until,d.purged_at,
                  d.legal_hold,d.legal_hold_reason,d.legal_hold_at,
                  d.legal_hold_released_at,d.legal_hold_release_reason,
                  r.status AS request_status,target.full_name AS target_name
             FROM request_documents d JOIN requests r ON r.id=d.request_id
             JOIN employees target ON target.id=r.target_employee_id
            WHERE r.closed_at IS NOT NULL OR d.legal_hold=TRUE
            ORDER BY COALESCE(d.purged_at,d.retain_until,d.uploaded_at) DESC LIMIT 50""")
    for row in policies:
        row["updated_at"] = row["updated_at"].isoformat() if row["updated_at"] else None
    for row in retention:
        for key in ("retain_until", "purged_at", "legal_hold_at", "legal_hold_released_at"):
            row[key] = row[key].isoformat() if row[key] else None
    return {"stats": stats, "policies": policies, "retention": retention}


def update_retention_policy(outcome, seconds, actor_id):
    if outcome not in ("completed", "rejected") or seconds < 60 or seconds > 315360000:
        return None
    row = db.query(
        """UPDATE document_retention_policies SET retention_seconds=%s,updated_at=now(),
                  updated_by_id=%s WHERE outcome=%s
              RETURNING outcome,label,retention_seconds""",
        (seconds, actor_id, outcome), fetch="one")
    if row:
        audit.record("config", "document_retention_policy_updated",
                     target_type="retention_policy", target_id=outcome,
                     target_label=row["label"], details={"retention_seconds": seconds})
    return row


def apply_legal_hold(doc_id, reason, actor_id):
    row = db.query(
        """UPDATE request_documents SET legal_hold=TRUE,legal_hold_reason=%s,
                  legal_hold_at=now(),legal_hold_by_id=%s,
                  legal_hold_released_at=NULL,legal_hold_release_reason=NULL,
                  legal_hold_released_by_id=NULL
            WHERE id=%s AND purged_at IS NULL
          RETURNING id,request_id""", (reason, actor_id, doc_id), fetch="one")
    if row:
        audit.record("audit", "document_legal_hold_applied", target_type="document",
                     target_id=doc_id, target_label=f"document #{doc_id}",
                     details={"request_id": row["request_id"], "reason": reason})
    return row


def release_legal_hold(doc_id, reason, actor_id):
    row = db.query(
        """UPDATE request_documents SET legal_hold=FALSE,legal_hold_released_at=now(),
                  legal_hold_release_reason=%s,legal_hold_released_by_id=%s
            WHERE id=%s AND legal_hold=TRUE AND purged_at IS NULL
          RETURNING id,request_id""", (reason, actor_id, doc_id), fetch="one")
    if row:
        audit.record("audit", "document_legal_hold_released", target_type="document",
                     target_id=doc_id, target_label=f"document #{doc_id}",
                     details={"request_id": row["request_id"], "reason": reason})
    return row


def approval_blocker(request_id):
    rows = db.query(
        """SELECT scan_status, COUNT(*) AS count FROM request_documents
            WHERE request_id=%s AND NOT (scan_status = ANY(%s))
            GROUP BY scan_status ORDER BY scan_status""", (request_id, list(APPROVAL_READY_STATUSES)))
    if not rows:
        return None
    detail = ", ".join(f"{r['count']} {r['scan_status'].replace('_', ' ')}" for r in rows)
    return f"Supporting documents are not ready for approval ({detail}). Check the request details."


def review_items():
    """Documents that need an IT privacy decision. Raw content is never returned."""
    rows = db.query(
        """SELECT d.id, d.request_id, d.filename, d.scan_status, d.pii_summary,
                  d.scan_error, d.uploaded_at, d.scanned_at, d.review_note,
                  d.review_action_at, d.removed_at,d.scan_method,d.ocr_confidence,
                  (d.content_base64 IS NOT NULL) AS has_content,
                  r.type AS request_type, r.status AS request_status,
                  target.full_name AS target_name, target.username AS target_username,
                  owner.full_name AS requestor_name, s.name AS service_name
             FROM request_documents d
             JOIN requests r ON r.id=d.request_id
             JOIN employees target ON target.id=r.target_employee_id
             JOIN employees owner ON owner.id=r.requestor_id
             LEFT JOIN services s ON s.id=r.service_id
            WHERE d.scan_status = ANY(%s)
              AND r.status NOT IN ('completed','rejected')
            ORDER BY COALESCE(d.review_action_at,d.scanned_at,d.uploaded_at),d.id""",
        (list(REVIEW_STATUSES),))
    for row in rows:
        for key in ("uploaded_at", "scanned_at", "review_action_at", "removed_at"):
            row[key] = row[key].isoformat() if row[key] else None
        row["pii_summary"] = row["pii_summary"] or {}
        row["ocr_confidence"] = (float(row["ocr_confidence"])
                                 if row["ocr_confidence"] is not None else None)
    return rows


def review_document(doc_id, allowed_statuses=REVIEW_STATUSES):
    return db.query(
        """SELECT d.*, r.status AS request_status, r.requestor_id
             FROM request_documents d JOIN requests r ON r.id=d.request_id
            WHERE d.id=%s AND d.scan_status = ANY(%s)""",
        (doc_id, list(allowed_statuses)), fetch="one")


def retry_document(doc_id, actor_id):
    row = db.query(
        """UPDATE request_documents d SET scan_status='pending', uploaded_at=now(),
                  pii_summary='{}'::jsonb, source_sha256=NULL, content_sha256=NULL,
                  scanned_at=NULL, scan_error=NULL, scan_method=NULL,ocr_confidence=NULL,
                  review_note=NULL,
                  review_action_at=now(), reviewed_by_id=%s
             FROM requests r
            WHERE d.id=%s AND r.id=d.request_id
              AND d.scan_status = ANY(%s)
              AND r.status NOT IN ('completed','rejected')
              AND d.content_base64 IS NOT NULL
          RETURNING d.id,d.request_id""",
        (actor_id, doc_id, list(REVIEW_STATUSES)), fetch="one")
    if row:
        audit.record("audit", "document_scan_retried", target_type="document", target_id=doc_id,
                     target_label=f"document #{doc_id}", details={"request_id": row["request_id"]})
    return row


def request_replacement(doc_id, actor_id, reason):
    row = db.query(
        """UPDATE request_documents d SET scan_status='replacement_requested',
                  review_note=%s, review_action_at=now(), reviewed_by_id=%s
             FROM requests r
            WHERE d.id=%s AND r.id=d.request_id
              AND d.scan_status IN ('review_required','failed')
              AND r.status NOT IN ('completed','rejected')
          RETURNING d.id,d.request_id""", (reason, actor_id, doc_id), fetch="one")
    if row:
        audit.record("audit", "document_replacement_requested", target_type="document", target_id=doc_id,
                     target_label=f"document #{doc_id}",
                     details={"request_id": row["request_id"], "reason": reason})
    return row


def replace_document(doc_id, actor, document):
    allowed = REVIEW_STATUSES if actor["role"] == "it_admin" else ("replacement_requested",)
    existing = review_document(doc_id, allowed)
    if not existing or existing["request_status"] in ("completed", "rejected"):
        return None
    if actor["role"] == "requestor" and existing["requestor_id"] != actor["id"]:
        return None
    row = db.query(
        """UPDATE request_documents SET filename=%s,content_base64=%s,uploaded_at=now(),
                  scan_status='pending',pii_summary='{}'::jsonb,source_sha256=NULL,
                  content_sha256=NULL,scanned_at=NULL,scan_error=NULL,retain_until=NULL,
                  scan_method=NULL,ocr_confidence=NULL,purged_at=NULL,removed_at=NULL,
                  review_note=NULL,review_action_at=now(),
                  reviewed_by_id=%s
            WHERE id=%s RETURNING id,request_id""",
        (document["filename"], document["content_base64"], actor["id"], doc_id), fetch="one")
    audit.record("audit", "document_replaced", target_type="document", target_id=doc_id,
                 target_label=f"document #{doc_id}",
                 details={"request_id": row["request_id"], "filename": document["filename"]})
    return row


def remove_document(doc_id, actor_id, reason):
    row = db.query(
        """UPDATE request_documents d SET content_base64=NULL,scan_status='replacement_requested',
                  scan_error=NULL,review_note=%s,review_action_at=now(),reviewed_by_id=%s,
                  removed_at=now()
             FROM requests r
            WHERE d.id=%s AND r.id=d.request_id
              AND d.scan_status = ANY(%s)
              AND r.status NOT IN ('completed','rejected')
          RETURNING d.id,d.request_id""",
        (reason, actor_id, doc_id, list(REVIEW_STATUSES)), fetch="one")
    if row:
        audit.record("audit", "document_removed", target_type="document", target_id=doc_id,
                     target_label=f"document #{doc_id}",
                     details={"request_id": row["request_id"], "reason": reason})
    return row


def start_background():
    print(
        "[privacy] worker config: "
        f"poll={SCAN_INTERVAL}s, minimum_quarantine={MIN_QUARANTINE_SECONDS}s, "
        f"fallback_retention={RETENTION_SECONDS}s, purge_poll={PURGE_INTERVAL}s, "
        f"ocr_dpi={OCR_DPI}, ocr_min_confidence={OCR_MIN_CONFIDENCE}%",
        flush=True,
    )

    def loop():
        last_purge = 0.0
        while True:
            try:
                process_pending()
                now = time.monotonic()
                if now - last_purge >= PURGE_INTERVAL:
                    schedule_retention()
                    purge_due()
                    last_purge = now
            except Exception as exc:  # never let the worker die
                print(f"[privacy] background error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(SCAN_INTERVAL)

    threading.Thread(target=loop, name="document-privacy", daemon=True).start()
