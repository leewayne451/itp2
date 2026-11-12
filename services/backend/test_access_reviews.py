"""Offline checks for access-review finding classification (CHANGE 010).

    cd services/backend && python test_access_reviews.py

No database, Keycloak or network is needed.
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
core = types.ModuleType("app.core")
for name in ("add_asset_event", "department_effective_services", "employee_asset_kinds_needed",
             "held_assets", "revoke_with_dependents", "service_names"):
    setattr(core, name, lambda *args, **kwargs: None)
sys.modules["app.core"] = core

path = pathlib.Path(__file__).parent / "app" / "access_reviews.py"
spec = importlib.util.spec_from_file_location("app.access_reviews", path)
reviews = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reviews)

failed = []


def check(name, actual, expected):
    ok = actual == expected
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        print(f"      expected {expected!r}; got {actual!r}")
        failed.append(name)


check("aligned approved access", reviews.classify("active", True, True, False, 12),
      (["aligned"], "aligned", "low"))
check("missing expected access", reviews.classify("active", False, True, False, None),
      (["missing_expected"], "missing_expected", "medium"))
check("access outside department standard", reviews.classify("active", True, False, False, 12),
      (["outside_standard"], "outside_standard", "medium"))
check("orphan entitlement", reviews.classify("active", True, True, False, None),
      (["orphan_entitlement"], "orphan_entitlement", "high"))
check("asset without entitlement", reviews.classify("active", False, False, True, None),
      (["asset_without_access"], "asset_without_access", "high"))
check("inactive access is critical and wins risk ordering",
      reviews.classify("inactive", True, False, False, None),
      (["inactive_access", "orphan_entitlement", "outside_standard"],
       "inactive_access", "critical"))
check("asset and missing access retain both findings",
      reviews.classify("active", False, True, True, None),
      (["asset_without_access", "missing_expected"], "asset_without_access", "high"))

print("\nFAILED: " + ", ".join(failed) if failed else "\nAll checks passed.")
sys.exit(1 if failed else 0)
