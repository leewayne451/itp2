import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("SECRET_KEY", "test-secret-" * 4)
os.environ.setdefault("CLIENT_SECRET", "test-client-" * 4)
os.environ.setdefault("OIDC_CLIENT_SECRET", "test-oidc-" * 4)

from app.chatbot import action_for, guidance_for, intent_for, request_id_for, status_reply
from app.demo_policy import mock_policy_answer
from app.views.chatbot import _status_rows, _status_sources
from app import create_app


class ChatbotIntentTests(unittest.TestCase):
    def test_common_workflow_questions_are_recognized(self):
        self.assertEqual(intent_for("hi"), "greeting")
        self.assertEqual(intent_for("Hello there!"), "greeting")
        self.assertEqual(intent_for("What is the status of my access request?"), "status")
        self.assertEqual(intent_for("Where is the laptop request?"), "status")
        self.assertEqual(intent_for("How do I request access to Service One?"), "grant")
        self.assertEqual(intent_for("What do I need to onboard a new hire?"), "onboard")
        self.assertEqual(intent_for("How do I revoke access?"), "return")
        self.assertEqual(intent_for("How do I request a return?"), "return")
        self.assertEqual(intent_for("How do I remove access?"), "return")

    def test_greeting_intent_returns_a_welcome(self):
        self.assertTrue(guidance_for(intent_for("hi"), "employee").startswith("Hi!"))

    def test_policy_questions_are_not_answered_as_workflow_facts(self):
        answer = guidance_for(intent_for("What is the MND HR policy for onboarding?"), "employee")
        self.assertIn("policy documents", answer)

    def test_only_requestors_get_links_to_submission_forms(self):
        url = "/"
        self.assertEqual(action_for("return", "requestor", url)[0]["url"], "/#return-request")
        self.assertEqual(action_for("return", "employee", url), [])

    def test_staff_status_lookups_use_only_their_permitted_views(self):
        self.assertEqual(
            _status_sources("it_admin"),
            [("/api/requests/my-status", None), ("/api/requests/queue", None),
             ("/api/requests/history", None)],
        )
        self.assertEqual(
            _status_sources("admin"),
            [("/api/requests/my-status", None), ("/api/requests", None)],
        )
        self.assertEqual(_status_sources("employee"), [("/api/requests/my-status", None)])

    def test_staff_can_query_a_specific_request_through_backend_authorization(self):
        private_request = {"id": 17, "type": "grant", "status": "pending_it"}
        own_status = Mock(ok=True)
        own_status.json.return_value = []
        detail = Mock(ok=True)
        detail.json.return_value = private_request
        with patch("app.views.chatbot.call", side_effect=[own_status, detail]) as call:
            rows, error = _status_rows("it_admin", 17)

        self.assertIsNone(error)
        self.assertEqual(rows, [private_request])
        self.assertEqual(call.call_args_list[0].args, ("GET", "/api/requests/my-status"))
        self.assertEqual(call.call_args_list[1].args, ("GET", "/api/requests/17"))

    def test_employee_cannot_query_a_request_not_linked_to_them(self):
        own_status = Mock(ok=True)
        own_status.json.return_value = []
        with patch("app.views.chatbot.call", return_value=own_status) as call:
            rows, error = _status_rows("employee", 17)

        self.assertEqual(rows, [])
        self.assertIsNone(error)
        call.assert_called_once_with("GET", "/api/requests/my-status", params={"request_id": 17})

    def test_status_reply_filters_a_specific_request_and_limits_details(self):
        rows = [{
            "id": 12,
            "type": "grant",
            "status": "pending_hr",
            "target_name": "Alice",
            "service_name": "Personal Laptop",
            "target_department_name": None,
            "new_department_name": None,
            "source_department_name": None,
        }]
        self.assertEqual(request_id_for("status of request #12"), 12)
        reply = status_reply(rows, 12)
        self.assertIn("Request #12", reply)
        self.assertIn("Pending HR approval", reply)
        self.assertNotIn("justification", reply)
        self.assertIn("couldn't find request #13", status_reply(rows, 13))


class ChatbotPreviewTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, CHATBOT_PREVIEW_ENABLED=True)
        self.client = self.app.test_client()

    def test_preview_page_is_available_without_sign_in(self):
        response = self.client.get("/preview")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Hi!", response.data)
        self.assertIn(b"Local demo only", response.data)
        self.assertIn(b"fictional ExampleCo policy", response.data)
        self.assertIn(b"/chat/preview", response.data)

    def test_preview_uses_fictional_policy_snippets_with_a_source(self):
        reply = mock_policy_answer("What information do I need to onboard a new hire?")

        self.assertIn("fictional ExampleCo policy", reply)
        self.assertIn("full name, start date, department, and job role", reply)
        self.assertIn("Source: ExampleCo New Hire Setup", reply)
        self.assertIn("no real model", reply)

    def test_unknown_preview_policy_question_does_not_invent_an_answer(self):
        reply = mock_policy_answer("What is the ExampleCo travel policy?")

        self.assertIn("couldn't find that topic", reply)
        self.assertIn("not official HR guidance", reply)

    def test_mnd_policy_questions_do_not_receive_fictional_exampleco_answers(self):
        reply = mock_policy_answer("What is the MND onboarding policy?")

        self.assertIn("don't have MND policy documents", reply)
        self.assertIn("fictional", reply)

    def test_signed_in_chat_can_use_fictional_policy_without_calling_backend(self):
        with self.client.session_transaction() as session:
            session["user"] = {"full_name": "Alice", "role": "requestor"}
            session["handle"] = "test-handle"

        with patch("app.token_store.get", return_value={"access": "test-token"}), \
                patch("app.views.chatbot.call") as backend_call:
            response = self.client.post(
                "/chat",
                json={"message": "What is the fictional ExampleCo policy for onboarding a new hire?"},
                headers={"Origin": "http://localhost"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("fictional ExampleCo policy", response.get_json()["reply"])
        backend_call.assert_not_called()

    def test_signed_in_chat_keeps_regular_onboarding_guidance(self):
        with self.client.session_transaction() as session:
            session["user"] = {"full_name": "Alice", "role": "requestor"}
            session["handle"] = "test-handle"

        with patch("app.token_store.get", return_value={"access": "test-token"}):
            response = self.client.post(
                "/chat",
                json={"message": "What do I need to onboard a new hire?"},
                headers={"Origin": "http://localhost"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Onboard form", response.get_json()["reply"])
        self.assertNotIn("ExampleCo", response.get_json()["reply"])

    def test_preview_policy_answer_does_not_call_backend(self):
        with patch("app.views.chatbot.call") as backend_call:
            response = self.client.post(
                "/chat/preview",
                json={"message": "What information do I need to onboard a new hire?"},
                headers={"Origin": "http://localhost"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("fictional ExampleCo policy", response.get_json()["reply"])
        backend_call.assert_not_called()

    def test_preview_status_never_queries_live_backend(self):
        with patch("app.views.chatbot.call") as backend_call:
            response = self.client.post(
                "/chat/preview",
                json={"message": "What is the status of my access request?"},
                headers={"Origin": "http://localhost"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Demo only", response.get_json()["reply"])
        backend_call.assert_not_called()

    def test_preview_routes_are_disabled_when_feature_flag_is_off(self):
        self.app.config["CHATBOT_PREVIEW_ENABLED"] = False

        self.assertEqual(self.client.get("/preview").status_code, 404)
        self.assertEqual(
            self.client.post("/chat/preview", headers={"Origin": "http://localhost"}).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
