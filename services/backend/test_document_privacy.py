"""Offline detector checks for personal-data document protection (CHANGE 012).

    cd services/backend && python test_document_privacy.py

PDF rendering is integration-tested inside the backend container; these checks
exercise the deterministic personal-data classifiers without a database.
"""
import importlib.util
import pathlib
import sys
import types


package = types.ModuleType("app")
package.__path__ = []
sys.modules["app"] = package
sys.modules["app.audit"] = types.ModuleType("app.audit")
sys.modules["app.db"] = types.ModuleType("app.db")

path = pathlib.Path(__file__).parent / "app" / "document_privacy.py"
spec = importlib.util.spec_from_file_location("app.document_privacy", path)
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)

failed = []


def check(name, ok, detail=None):
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        if detail is not None:
            print("      ", detail)
        failed.append(name)


check("valid synthetic NRIC accepted", privacy.valid_nric_fin("S1234567D"))
check("spaces in valid NRIC accepted", privacy.valid_nric_fin("S 1 2 3 4 5 6 7 D"))
check("wrong NRIC checksum refused", not privacy.valid_nric_fin("S1234567A"))

sample = ("Officer: Synthetic Test Person\nNRIC: S1234567D\n"
          "Phone: +65 9123 4567\nEmail: glenn.test@example.gov.sg\n"
          "Address: 123 Example Road, Singapore 123456\n")
matches = privacy.find_matches(sample)
summary = privacy.summary_of(matches)
check("all four personal-data types detected",
      summary == {"address": 1, "email": 1, "nric_fin": 1, "phone": 1}, summary)
check("scan metadata contains counts, not raw values",
      "S1234567D" not in str(summary) and "+65 9123 4567" not in str(summary), summary)
check("ordinary reference number not treated as NRIC",
      not privacy.find_matches("Request reference ABC-123456 and invoice 12345678."))

print("\nFAILED: " + ", ".join(failed) if failed else "\nAll checks passed.")
sys.exit(1 if failed else 0)
