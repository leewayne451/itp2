import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.api.requests import _my_request_statuses


class ChatbotRequestStatusTests(unittest.TestCase):
    def test_status_query_is_limited_to_requests_involving_the_user(self):
        row = {
            "id": 17,
            "type": "grant",
            "status": "pending_hr",
            "target_name": "Alice Employee",
            "service_name": "Personal Laptop",
            "target_department_name": "Engineering",
            "new_department_name": None,
            "source_department_name": None,
            "updated_at": datetime(2026, 11, 12, tzinfo=timezone.utc),
            "justification": "not returned to chat",
        }
        with patch("app.api.requests.db.query", return_value=[row]) as query:
            result = _my_request_statuses(42)

        sql, params = query.call_args.args
        self.assertIn("r.requestor_id = %s OR r.target_employee_id = %s", sql)
        self.assertEqual(params, (42, 42))
        self.assertEqual(result[0]["id"], 17)
        self.assertNotIn("justification", result[0])

    def test_specific_request_id_is_an_additional_filter(self):
        with patch("app.api.requests.db.query", return_value=[]) as query:
            self.assertEqual(_my_request_statuses(42, 17), [])

        sql, params = query.call_args.args
        self.assertIn("AND r.id = %s", sql)
        self.assertEqual(params, (42, 42, 17))


if __name__ == "__main__":
    unittest.main()
