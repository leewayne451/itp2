"""Offline checks for scheduled and emergency offboarding."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import offboarding
from app.api.offboarding import parse_effective_at


class EffectiveTimeTests(unittest.TestCase):
    def test_singapore_browser_time_becomes_utc(self):
        future = datetime.now(timezone.utc) + timedelta(days=2)
        local = future.astimezone(timezone(timedelta(hours=8)))
        parsed = parse_effective_at(local.strftime("%Y-%m-%dT%H:%M:%S"))
        self.assertEqual(parsed.utcoffset(), timedelta(0))
        self.assertLess(abs((parsed - future.replace(microsecond=0)).total_seconds()), 1)

    def test_past_schedule_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "future"):
            parse_effective_at("2020-01-01T09:00")


class OffboardingExecutionTests(unittest.TestCase):
    def setUp(self):
        self.emp = {"id": 7, "username": "erin", "role": "employee",
                    "status": "active", "offboarded_at": None}
        self.actor = {"id": 3, "username": "harper", "role": "hr"}

    @patch("app.offboarding.audit.record")
    @patch("app.offboarding._raise_return", return_value=(55, None))
    @patch("app.offboarding._close_open_requests", return_value=[11, 12])
    @patch("app.offboarding.sync.push", return_value=True)
    @patch("app.offboarding._employee")
    @patch("app.offboarding.db.query")
    def test_emergency_cutoff_keeps_evidence_and_raises_return(self, query, employee, push,
                                                               close, raise_return, audit):
        employee.return_value = self.emp
        finished = datetime.now(timezone.utc)
        query.side_effect = [None, [{"service_id": 10}, {"service_id": 20}],
                             {"offboarded_at": finished}]
        result = offboarding.execute(7, "Contract ended", self.actor)

        self.assertEqual(result["retained_service_ids"], [10, 20])
        self.assertEqual(result["closed_request_ids"], [11, 12])
        self.assertEqual(result["return_request_id"], 55)
        self.assertTrue(result["signed_out_everywhere"])
        push.assert_called_once_with(7, end_sessions=True)
        close.assert_called_once_with(7, self.actor, "Contract ended")
        raise_return.assert_called_once_with(self.emp, self.actor, "Contract ended", "emergency")
        self.assertEqual(audit.call_args.args[1], "employee_offboarded")

    @patch("app.offboarding.db.query")
    def test_existing_return_is_reused(self, query):
        query.return_value = {"id": 88}
        rid, note = offboarding._raise_return(self.emp, self.actor, "Leaving", "scheduled")
        self.assertEqual(rid, 88)
        self.assertIn("already open", note)

    @patch("app.offboarding.db.query", side_effect=[None, []])
    def test_no_return_is_created_without_held_access(self, _query):
        rid, note = offboarding._raise_return(self.emp, self.actor, "Leaving", "scheduled")
        self.assertIsNone(rid)
        self.assertIn("nothing to return", note)

    @patch("app.offboarding.sync.push")
    @patch("app.offboarding._employee")
    def test_repeat_is_idempotent(self, employee, push):
        self.emp["offboarded_at"] = datetime.now(timezone.utc)
        employee.return_value = self.emp
        result = offboarding.execute(7, "Repeated click", self.actor)
        self.assertTrue(result["already_offboarded"])
        push.assert_not_called()

    @patch("app.offboarding.audit.record")
    @patch("app.offboarding._employee")
    @patch("app.offboarding.db.query")
    def test_schedule_is_persisted_and_audited(self, query, employee, audit):
        effective = datetime.now(timezone.utc) + timedelta(days=1)
        scheduled = dict(self.emp, scheduled_offboard_at=effective)
        employee.side_effect = [self.emp, scheduled]
        result = offboarding.schedule(7, effective, "Last working day", self.actor)
        self.assertEqual(result["scheduled_offboard_at"], effective)
        self.assertIn("scheduled_offboard_at", query.call_args.args[0])
        self.assertEqual(audit.call_args.args[1], "offboarding_scheduled")

    @patch("app.offboarding.execute")
    @patch("app.offboarding.db.query")
    def test_due_worker_uses_scheduled_reason(self, query, execute):
        query.return_value = [{"id": 7, "scheduled_offboard_reason": "Last day"}]
        self.assertEqual(offboarding.execute_due(), 1)
        execute.assert_called_once_with(7, "Last day", offboarding.SYSTEM, trigger="scheduled")


if __name__ == "__main__":
    unittest.main()
