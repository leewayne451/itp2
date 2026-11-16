"""End-to-end checks for the IT Admin demand-forecast API on a running demo stack.

It signs in as IT Admin and AA, so it resets those demo credentials. It does not
change quotas, inventory, HR events, or workflow requests.
"""
import sys

from _e2e import check, get, session_for, summary


def main():
    it = session_for("itadmin")[1]
    aa = session_for("aa")[1]
    code, report = get("/api/demand-forecast", it)
    check("IT Admin can read the demand forecast", code == 200, report)
    if code != 200:
        return summary()
    check("forecast states its 90-day lookback and horizon",
          report.get("lookback_days") == 90 and report.get("horizon_days") == 90, report)
    check("seeded demo trends are labelled", report.get("demo_data_included") and report.get("demo_event_count", 0) > 0,
          report)
    engineering = next((d for d in report.get("departments", []) if d["department_name"] == "Engineering"), {})
    laptop = next((r for r in engineering.get("resources", []) if r["resource_type"] == "laptop"), {})
    check("Engineering trend identifies a laptop department-capacity gap", laptop.get("quota_gap", 0) > 0, laptop)
    check("forecast is IT Admin-only", get("/api/demand-forecast", aa)[0] == 403)
    return summary()


if __name__ == "__main__":
    sys.exit(main())
