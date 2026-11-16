"""Offline checks for the pure demand-forecast calculations.

    cd services/backend && python test_demand_forecast.py
"""
import importlib.util
import json
import pathlib
import sys
import types

package = types.ModuleType("app")
package.__path__ = []
sys.modules["app"] = package
sys.modules["app.db"] = types.ModuleType("app.db")

path = pathlib.Path(__file__).parent / "app" / "demand_forecast.py"
spec = importlib.util.spec_from_file_location("app.demand_forecast", path)
forecast = importlib.util.module_from_spec(spec)
spec.loader.exec_module(forecast)

failed = []


def check(name, actual, expected):
    ok = actual == expected
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        print(f"      expected {expected!r}; got {actual!r}")
        failed.append(name)


check("hires and transfers produce net department movement", forecast.event_deltas([
    {"type": "hire", "department_id": 1},
    {"type": "transfer", "department_id": 1, "source_department_id": 2},
    {"type": "transfer", "department_id": 3, "source_department_id": 1},
]), {1: 1, 2: -1, 3: 1})
forecast.db.query = lambda *_args, **_kwargs: [{"event_id": "hr-0001", "event_type": "transfer",
                                                  "payload": json.dumps({"department": "Engineering"}),
                                                  "source_department_id": 2}]
check("real HR transfer retains destination and source departments",
      forecast._real_events({"engineering": 1}),
      [{"type": "transfer", "department_id": 1, "source_department_id": 2}])
check("projected demand identifies a quota gap", forecast.resource_projection(1, 3, 2),
      {"projected_demand": 4, "quota_gap": 2, "recommended_quota": 4, "procurement_needed": None})
check("available stock offsets procurement", forecast.resource_projection(2, 4, 5, 3),
      {"projected_demand": 6, "quota_gap": 1, "recommended_quota": 6, "procurement_needed": 1})
check("negative trend never projects below zero", forecast.resource_projection(1, -4, 2),
      {"projected_demand": 0, "quota_gap": 0, "recommended_quota": 2, "procurement_needed": None})

print("\nFAILED: " + ", ".join(failed) if failed else "\nAll checks passed.")
sys.exit(1 if failed else 0)
