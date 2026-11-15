"""Offline checks for the first time-bound access iteration."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import access_lifecycle, core
from app.api.requests import parse_access_expiry


class ExpiryParsingTests(unittest.TestCase):
    def test_blank_means_ongoing_access(self):
        self.assertIsNone(parse_access_expiry(""))

    def test_browser_time_is_interpreted_as_singapore_time(self):
        future = datetime.now(timezone.utc) + timedelta(days=2)
        local = future.astimezone(timezone(timedelta(hours=8)))
        parsed = parse_access_expiry(local.strftime("%Y-%m-%dT%H:%M:%S"))
        self.assertEqual(parsed.utcoffset(), timedelta(0))
        self.assertLess(abs((parsed - future.replace(microsecond=0)).total_seconds()), 1)

    def test_past_expiry_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "future"):
            parse_access_expiry("2020-01-01T09:00")


class ExpiryWorkerTests(unittest.TestCase):
    def setUp(self):
        self.due = datetime.now(timezone.utc) - timedelta(seconds=1)

    @patch("app.access_lifecycle.service_by_type")
    @patch("app.access_lifecycle.create_collection_tasks")
    @patch("app.access_lifecycle.revoke_access", return_value=True)
    @patch("app.access_lifecycle.db.query")
    def test_laptop_expiry_leaves_account_alone(self, query, revoke, collect, service_by_type):
        query.return_value = [{"employee_id": 7, "service_id": 10,
                               "expires_at": self.due, "resource_type": "laptop"}]
        self.assertEqual(access_lifecycle.expire_due(), 1)
        service_by_type.assert_not_called()
        self.assertEqual(revoke.call_count, 1)
        self.assertEqual(revoke.call_args.kwargs["audit_action"], "entitlement_expired")
        self.assertEqual(revoke.call_args.kwargs["due_at"], self.due)
        collect.assert_called_once_with(7, 10)

    @patch("app.access_lifecycle.service_by_type", return_value={"id": 10})
    @patch("app.access_lifecycle.create_collection_tasks")
    @patch("app.access_lifecycle.revoke_access", side_effect=[True, True])
    @patch("app.access_lifecycle.db.query")
    def test_account_expiry_also_removes_dependent_laptop(self, query, revoke, collect, _service_by_type):
        query.return_value = [{"employee_id": 7, "service_id": 20,
                               "expires_at": self.due, "resource_type": "account"}]
        self.assertEqual(access_lifecycle.expire_due(), 2)
        self.assertEqual(revoke.call_count, 2)
        self.assertEqual(revoke.call_args_list[1].args, (7, 10))
        self.assertEqual(revoke.call_args_list[1].kwargs["audit_action"], "entitlement_expired")
        self.assertIn("dependent", revoke.call_args_list[1].kwargs["audit_details"]["reason"])
        collect.assert_called_once_with(7, 20)

    @patch("app.access_lifecycle.create_collection_tasks")
    @patch("app.access_lifecycle.revoke_access", return_value=False)
    @patch("app.access_lifecycle.db.query")
    def test_stale_worker_read_is_idempotent(self, query, revoke, collect):
        query.return_value = [{"employee_id": 7, "service_id": 10,
                               "expires_at": self.due, "resource_type": "laptop"}]
        self.assertEqual(access_lifecycle.expire_due(), 0)
        revoke.assert_called_once()
        collect.assert_not_called()


class AssetCollectionTests(unittest.TestCase):
    @patch("app.access_lifecycle.audit.record")
    @patch("app.access_lifecycle.held_assets")
    @patch("app.access_lifecycle.employee_asset_kinds_needed", return_value={"Account"})
    @patch("app.access_lifecycle.db.query", return_value={"id": 42})
    def test_expiry_queues_only_unneeded_held_assets(self, query, _needed, held, audit):
        held.return_value = [
            {"id": 1, "asset_tag": "ACC-1", "kind": "Account"},
            {"id": 2, "asset_tag": "LAP-1", "kind": "Laptop"},
        ]
        self.assertEqual(access_lifecycle.create_collection_tasks(7, 10), 1)
        self.assertEqual(query.call_count, 1)
        self.assertEqual(query.call_args.args[1][:3], (2, 7, 10))
        self.assertEqual(audit.call_args.args[1], "asset_collection_requested")

    @patch("app.access_lifecycle.audit.record")
    @patch("app.access_lifecycle.employee_asset_kinds_needed", return_value={"Laptop"})
    @patch("app.access_lifecycle.db.query")
    def test_regrant_cancels_pending_collection(self, query, _needed, audit):
        query.return_value = [{"id": 42, "asset_tag": "LAP-1", "kind": "Laptop"}]
        self.assertEqual(access_lifecycle.cancel_resolved_collections(7, actor={"id": 3}), 1)
        self.assertIn("status='cancelled'", query.call_args.args[0])
        self.assertEqual(audit.call_args.args[1], "asset_collection_cancelled")

    @patch("app.access_lifecycle.add_asset_event")
    @patch("app.access_lifecycle.employee_asset_kinds_needed", return_value=set())
    @patch("app.access_lifecycle.db.query")
    def test_euss_collection_releases_asset(self, query, _needed, add_event):
        task = {"id": 42, "asset_id": 2, "employee_id": 7, "status": "pending",
                "asset_tag": "LAP-1", "kind": "Laptop", "asset_status": "assigned",
                "assigned_to": 7}
        query.side_effect = [task, None, None]
        result, error = access_lifecycle.collect_asset(42, {"id": 9})
        self.assertIsNone(error)
        self.assertEqual(result, task)
        self.assertIn("status='available'", query.call_args_list[1].args[0])
        add_event.assert_called_once_with(2, 7, "expiry_retrieved", None)

    @patch("app.access_lifecycle.cancel_resolved_collections")
    @patch("app.access_lifecycle.employee_asset_kinds_needed", return_value={"Laptop"})
    @patch("app.access_lifecycle.db.query")
    def test_collection_is_blocked_after_regrant(self, query, _needed, cancel):
        query.return_value = {"id": 42, "asset_id": 2, "employee_id": 7, "status": "pending",
                              "asset_tag": "LAP-1", "kind": "Laptop", "asset_status": "assigned",
                              "assigned_to": 7}
        result, error = access_lifecycle.collect_asset(42, {"id": 9})
        self.assertIsNone(result)
        self.assertIn("needs the asset again", error)
        cancel.assert_called_once()


class EntitlementPersistenceTests(unittest.TestCase):
    @patch("app.core._audit_entitlement")
    @patch("app.core.db.query", return_value={"employee_id": 7})
    def test_grant_persists_expiry_and_request_evidence(self, query, audit):
        expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.assertTrue(core.grant_access(7, 10, expires_at=expiry, request_id=99))
        self.assertIn("expires_at", query.call_args.args[0])
        self.assertEqual(query.call_args.args[1], (7, 10, expiry))
        self.assertEqual(audit.call_args.args[0], "entitlement_granted")
        self.assertEqual(audit.call_args.args[3]["request_id"], 99)

    @patch("app.core._audit_entitlement")
    @patch("app.core.db.query", return_value=None)
    def test_due_revoke_does_not_audit_a_stale_or_extended_row(self, query, audit):
        observed = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertFalse(core.revoke_access(7, 10, audit_action="entitlement_expired",
                                            due_at=observed))
        self.assertIn("expires_at=%s AND expires_at<=now()", query.call_args.args[0])
        audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
