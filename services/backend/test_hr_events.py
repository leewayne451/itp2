"""Offline checks for the HR webhook's signature and payload validation (CHANGE 008).

    cd services/backend && python test_hr_events.py

No database, Keycloak or network needed: hr_events.py is pure. (app/__init__.py is bypassed
by loading the file directly, so the backend's required secrets are not needed either.)
"""
import importlib.util
import pathlib
import sys

spec = importlib.util.spec_from_file_location("hr_events", pathlib.Path(__file__).parent / "app" / "hr_events.py")
hr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hr)

SECRET = "s" * 48
BODY = b'{"event_id":"hr-1234abcd","type":"resign","employee":{"username":"alice"}}'
NOW = 1_800_000_000
failed = []


def check(name, ok):
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        failed.append(name)


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


sig = hr.sign(SECRET, str(NOW), BODY)
check("valid signature accepted", raises(hr.SignatureError, hr.verify_signature, SECRET, str(NOW), sig, BODY, now=NOW) is False
      and hr.verify_signature(SECRET, str(NOW), sig, BODY, now=NOW) is None)
check("signature has the sha256= prefix", sig.startswith("sha256=") and len(sig) == 7 + 64)
check("tampered body rejected", raises(hr.SignatureError, hr.verify_signature, SECRET, str(NOW), sig, BODY + b" ", now=NOW))
check("wrong secret rejected", raises(hr.SignatureError, hr.verify_signature, "x" * 48, str(NOW), sig, BODY, now=NOW))
check("timestamp is covered by the MAC", raises(hr.SignatureError, hr.verify_signature, SECRET, str(NOW + 1), sig, BODY, now=NOW))
check("stale timestamp rejected (replay)", raises(hr.SignatureError, hr.verify_signature, SECRET, str(NOW), sig, BODY, now=NOW + 301))
check("future timestamp rejected", raises(hr.SignatureError, hr.verify_signature, SECRET, str(NOW), sig, BODY, now=NOW - 301))
check("within the window accepted", hr.verify_signature(SECRET, str(NOW), sig, BODY, now=NOW + 299) is None)
check("missing headers rejected", raises(hr.SignatureError, hr.verify_signature, SECRET, None, None, BODY, now=NOW))
check("non-numeric timestamp rejected", raises(hr.SignatureError, hr.verify_signature, SECRET, "abc", sig, BODY, now=NOW))

ok = {"event_id": "hr-1234abcd", "type": "hire", "employee": {"username": "Nina", "full_name": "Nina New", "email": "n@x.io"},
      "department": "Sales"}
v = hr.validate_event(ok)
check("hire validates and username is lower-cased", v["employee"]["username"] == "nina" and v["department"] == "Sales")
check("resign needs no department or full name",
      hr.validate_event({"event_id": "hr-1234abcd", "type": "resign", "employee": {"username": "alice"}})["department"] is None)
check("transfer needs a department", raises(hr.EventError, hr.validate_event,
      {"event_id": "hr-1234abcd", "type": "transfer", "employee": {"username": "alice"}}))
check("hire needs a full name", raises(hr.EventError, hr.validate_event,
      {"event_id": "hr-1234abcd", "type": "hire", "employee": {"username": "alice"}, "department": "Sales"}))
check("unknown type rejected", raises(hr.EventError, hr.validate_event, dict(ok, type="promote")))
check("short event id rejected", raises(hr.EventError, hr.validate_event, dict(ok, event_id="abc")))
check("event id with odd characters rejected", raises(hr.EventError, hr.validate_event, dict(ok, event_id="hr-1234 abcd; drop")))
check("bad username rejected", raises(hr.EventError, hr.validate_event, dict(ok, employee={"username": "a b", "full_name": "x"})))
check("bad e-mail rejected", raises(hr.EventError, hr.validate_event, dict(ok, employee={"username": "nina", "full_name": "x", "email": "nope"})))
check("non-object body rejected", raises(hr.EventError, hr.validate_event, ["x"]))
check("non-text field rejected", raises(hr.EventError, hr.validate_event, dict(ok, department=5)))
check("over-long reason rejected", raises(hr.EventError, hr.validate_event, dict(ok, reason="x" * 501)))

print("\nFAILED: " + ", ".join(failed) if failed else "\nAll checks passed.")
sys.exit(1 if failed else 0)
