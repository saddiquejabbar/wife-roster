from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from roster.database import RosterDatabase
from roster.inbound_cli import run_inbound_command
from roster.inbound_cli import InboundOperationalError
from roster.inbound_cli import InboundPreflightError


GROUP_ID = "test-group"
OWNER_ID = "test-owner"
UNAUTHORIZED_ID = "test-intruder"
REVIEW_ONLY_ID = "test-review-only"
NOW = "2037-08-01T00:00:00Z"


def valid_transcription() -> dict[str, object]:
    return {
        "schema_version": 1,
        "coverage": "FULL",
        "report_header": {
            "period_from": "01Aug37",
            "period_to": "01Oct37",
            "port_local_notice_present": True,
        },
        "rows": [
            {
                "source_index": 0,
                "row_index": 0,
                "start_date": "06Aug37",
                "day": "Thu",
                "flight_number": "ZX410",
                "sector": "SIN-TPE",
                "duty": "FLY",
                "rpt": "0945",
                "std": "1145",
                "sta": "1630",
                "flight_time": None,
                "remarks": None,
                "unreadable": [],
            }
        ],
    }


def needs_review_transcription() -> dict[str, object]:
    transcription = json.loads(json.dumps(valid_transcription()))
    transcription["rows"][0]["rpt"] = "1300"
    return transcription


EXPECTED_REVIEW_REPLY = "\n".join(
    (
        "ROSTER",
        "",
        "AUGUST",
        "",
        "6 Thu",
        "",
        "ZX410",
        "Singapore → Taipei, Taiwan",
        "RPT 0945",
        "DEP 1145",
        "ARR 1630 (1630 SG)",
        "",
        "Duties: 1",
        "Flights: 1",
        "Future alerts: 3",
        "",
        "Needs review: none",
    )
)


class InboundCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.temporary = Path(self.directory.name)
        self.inbox = self.temporary / "runtime" / "private" / "inbox"
        self.database = RosterDatabase(self.temporary / "runtime" / "state.db")
        self.source = self.temporary / "roster.png"
        self.source.write_bytes(b"synthetic roster image")
        self.environment = {
            "WIFE_ROSTER_INBOUND_GROUP_ID": GROUP_ID,
            "WIFE_ROSTER_INBOUND_ALLOWED_SENDERS": OWNER_ID,
            "WIFE_ROSTER_INBOX": str(self.inbox),
        }

    def tearDown(self) -> None:
        self.directory.cleanup()

    def review_request(self, **overrides: object) -> dict[str, object]:
        request: dict[str, object] = {
            "group_id": GROUP_ID,
            "sender_id": OWNER_ID,
            "mentioned": True,
            "attachments": [
                {
                    "path": str(self.source),
                    "filename": "roster.png",
                    "content_type": "image/png",
                }
            ],
            "transcription": valid_transcription(),
            "now": NOW,
        }
        request.update(overrides)
        return request

    def run_command(
        self,
        command: str,
        request: dict[str, object],
        *,
        runtime_verifier=None,
        runtime_preflight=None,
        environment: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        output = StringIO()
        exit_code = run_inbound_command(
            command,
            database=self.database,
            stdin=StringIO(json.dumps(request)),
            stdout=output,
            environment=self.environment if environment is None else environment,
            runtime_verifier=runtime_verifier,
            runtime_preflight=runtime_preflight,
        )
        return exit_code, json.loads(output.getvalue())

    def test_review_machine_json_contains_exact_reply_sections(self):
        exit_code, response = self.run_command("review", self.review_request())

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            set(response),
            {
                "duty_count",
                "flight_count",
                "future_alert_count",
                "handled",
                "ingestion_id",
                "review_id",
                "can_approve",
                "issue_count",
                "ok",
                "reply",
            },
        )
        self.assertTrue(response["handled"])
        self.assertTrue(response["ok"])
        self.assertTrue(response["can_approve"])
        self.assertRegex(response["review_id"], r"^[A-Za-z0-9_-]{16}$")
        self.assertEqual(response["duty_count"], 1)
        self.assertEqual(response["flight_count"], 1)
        self.assertEqual(response["future_alert_count"], 3)
        self.assertEqual(response["issue_count"], 0)
        self.assertEqual(response["reply"], EXPECTED_REVIEW_REPLY)

    def test_review_without_attachment_returns_exact_instruction(self):
        exit_code, response = self.run_command(
            "review",
            self.review_request(attachments=[]),
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            response,
            {
                "handled": True,
                "ok": False,
                "reply": "Please attach the roster screenshot or PDF.",
            },
        )
        self.assertFalse(self.database.exists())

    def test_review_authorization_failure_is_controlled_and_non_specific(self):
        with patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("attachment read before authorization"),
        ):
            exit_code, response = self.run_command(
                "review",
                self.review_request(sender_id=UNAUTHORIZED_ID),
            )

        self.assertEqual(exit_code, 3)
        self.assertEqual(
            response,
            {
                "error_code": "unauthorized",
                "handled": True,
                "ok": False,
                "reply": "Not authorized.",
            },
        )
        self.assertNotIn(UNAUTHORIZED_ID, json.dumps(response))
        self.assertFalse(self.database.exists())

    def test_review_only_sender_cannot_approve_or_revise(self):
        environment = {
            **self.environment,
            "WIFE_ROSTER_INBOUND_ALLOWED_SENDERS": f"{OWNER_ID},{REVIEW_ONLY_ID}",
            "WIFE_ROSTER_STATE_CHANGE_ALLOWED_SENDERS": OWNER_ID,
        }
        review_exit, review = self.run_command(
            "review",
            self.review_request(sender_id=REVIEW_ONLY_ID),
            environment=environment,
        )
        self.assertEqual(review_exit, 0)

        for command in ("approve", "revise"):
            with self.subTest(command=command):
                exit_code, response = self.run_command(
                    command,
                    {
                        "group_id": GROUP_ID,
                        "sender_id": REVIEW_ONLY_ID,
                        "review_id": review["review_id"],
                        "now": NOW,
                    },
                    environment=environment,
                )
                self.assertEqual(exit_code, 3)
                self.assertEqual(response["error_code"], "unauthorized")
                self.assertEqual(response["reply"], "Not authorized.")

        state_path = next(self.inbox.glob("*/review.json"))
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8"))["status"],
            "pending",
        )
        self.assertFalse(self.database.exists())

    def test_review_without_mention_does_not_read_attachment_or_trigger(self):
        with patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("attachment read without mention"),
        ):
            exit_code, response = self.run_command(
                "review",
                self.review_request(mentioned=False),
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(response, {"handled": False})
        self.assertFalse(self.inbox.exists())
        self.assertFalse(self.database.exists())

    def test_review_is_dry_run_and_does_not_create_database_state(self):
        exit_code, response = self.run_command("review", self.review_request())

        self.assertEqual(exit_code, 0)
        self.assertTrue(response["ok"])
        self.assertFalse(self.database.exists())
        self.assertIsNone(self.database.active_roster())
        self.assertEqual(self.database.pending_alerts(), [])
        self.assertEqual(len(tuple(self.inbox.glob("*/review.json"))), 1)

    def test_needs_review_candidate_is_not_approvable(self):
        exit_code, review = self.run_command(
            "review",
            self.review_request(transcription=needs_review_transcription()),
        )

        self.assertEqual(exit_code, 0)
        self.assertFalse(review["can_approve"])
        self.assertGreater(review["issue_count"], 0)
        self.assertIn("NEEDS REVIEW", review["reply"])

        approve_exit, approval = self.run_command(
            "approve",
            {
                "group_id": GROUP_ID,
                "sender_id": OWNER_ID,
                "review_id": review["review_id"],
                "now": NOW,
            },
        )
        self.assertEqual(approve_exit, 2)
        self.assertEqual(approval["error_code"], "not_approvable")
        self.assertNotIn("terminal", approval)
        self.assertFalse(self.database.exists())

    def test_approve_without_pending_candidate_is_rejected(self):
        exit_code, response = self.run_command(
            "approve",
            {"group_id": GROUP_ID, "sender_id": OWNER_ID, "now": NOW},
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            response,
            {
                "error_code": "no_pending",
                "handled": True,
                "ok": False,
                "reply": "No roster awaiting approval.",
                "terminal": True,
            },
        )
        self.assertFalse(self.database.exists())

    def test_internal_cancel_invalidates_only_the_reviewed_candidate(self):
        review_exit, reviewed = self.run_command("review", self.review_request())
        self.assertEqual(review_exit, 0)

        cancel_exit, cancelled = self.run_command(
            "cancel",
            {
                "group_id": GROUP_ID,
                "sender_id": OWNER_ID,
                "ingestion_id": reviewed["ingestion_id"],
                "now": NOW,
            },
        )

        self.assertEqual(cancel_exit, 0)
        self.assertEqual(
            cancelled,
            {
                "handled": True,
                "ok": True,
                "reply": "Roster review cancelled.",
            },
        )
        approve_exit, approval = self.run_command(
            "approve",
            {"group_id": GROUP_ID, "sender_id": OWNER_ID, "now": NOW},
        )
        self.assertEqual(approve_exit, 2)
        self.assertEqual(approval["reply"], "No roster awaiting approval.")
        self.assertFalse(self.database.exists())

    def test_valid_review_can_be_approved_once(self):
        review_exit, review = self.run_command("review", self.review_request())
        self.assertEqual(review_exit, 0)
        self.assertTrue(review["ok"])

        approve_exit, approved = self.run_command(
            "approve",
            {
                "group_id": GROUP_ID,
                "sender_id": OWNER_ID,
                "review_id": review["review_id"],
                "now": NOW,
            },
        )

        self.assertEqual(approve_exit, 0)
        self.assertEqual(
            set(approved),
            {
                "activated",
                "flight_count",
                "future_alert_count",
                "handled",
                "ok",
                "reply",
                "review_id",
                "unchanged",
                "updated",
            },
        )
        self.assertTrue(approved["handled"])
        self.assertTrue(approved["ok"])
        self.assertEqual(approved["review_id"], review["review_id"])
        self.assertTrue(approved["activated"])
        self.assertFalse(approved["updated"])
        self.assertFalse(approved["unchanged"])
        self.assertEqual(approved["flight_count"], 1)
        self.assertEqual(approved["future_alert_count"], 3)
        self.assertEqual(
            approved["reply"],
            "\n".join(
                (
                    "Roster activated",
                    "",
                    "Flights: 1",
                    "Future alerts: 3",
                    "Next alert: 2037-08-05 2145 SG",
                )
            ),
        )
        self.assertIsNotNone(self.database.active_roster())
        self.assertEqual(len(self.database.pending_alerts()), 3)

    def test_exact_review_double_approve_is_inactive_and_idempotent(self):
        _, review = self.run_command("review", self.review_request())
        request = {
            "group_id": GROUP_ID,
            "sender_id": OWNER_ID,
            "review_id": review["review_id"],
            "now": NOW,
        }
        first_exit, _ = self.run_command("approve", request)
        keys_before = [alert.event_key for alert in self.database.pending_alerts()]

        second_exit, second = self.run_command("approve", request)

        self.assertEqual(first_exit, 0)
        self.assertEqual(second_exit, 2)
        self.assertEqual(second["error_code"], "inactive")
        self.assertTrue(second["terminal"])
        self.assertEqual(
            [alert.event_key for alert in self.database.pending_alerts()],
            keys_before,
        )

    def test_revise_exact_review_returns_instruction_and_disables_both_actions(self):
        _, review = self.run_command("review", self.review_request())
        request = {
            "group_id": GROUP_ID,
            "sender_id": OWNER_ID,
            "review_id": review["review_id"],
            "now": NOW,
        }

        revise_exit, revised = self.run_command("revise", request)

        self.assertEqual(revise_exit, 0)
        self.assertEqual(
            revised,
            {
                "handled": True,
                "ok": True,
                "reply": "\n".join(
                    (
                        "Roster not activated.",
                        "",
                        "Send the corrected/updated roster with:",
                        "/run_wife_roster@RosterDemoBot",
                    )
                ),
                "review_id": review["review_id"],
                "status": "revision_requested",
            },
        )
        for command in ("approve", "revise"):
            with self.subTest(command=command):
                exit_code, response = self.run_command(command, request)
                self.assertEqual(exit_code, 2)
                self.assertEqual(response["error_code"], "inactive")
                self.assertTrue(response["terminal"])
        self.assertFalse(self.database.exists())

    def test_review_expires_at_24_hours(self):
        _, review = self.run_command("review", self.review_request())

        exit_code, response = self.run_command(
            "approve",
            {
                "group_id": GROUP_ID,
                "sender_id": OWNER_ID,
                "review_id": review["review_id"],
                "now": "2037-08-02T00:00:00Z",
            },
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(response["error_code"], "expired")
        self.assertTrue(response["terminal"])
        self.assertEqual(
            response["reply"],
            "This roster review has expired. Please run the roster again.",
        )
        self.assertFalse(self.database.exists())

    def test_slash_fallback_approves_only_current_review(self):
        _, older = self.run_command("review", self.review_request())
        self.source.write_bytes(b"newer synthetic roster image")
        _, current = self.run_command("review", self.review_request())

        old_exit, old_response = self.run_command(
            "approve",
            {
                "group_id": GROUP_ID,
                "sender_id": OWNER_ID,
                "review_id": older["review_id"],
                "now": NOW,
            },
        )
        fallback_exit, fallback = self.run_command(
            "approve",
            {"group_id": GROUP_ID, "sender_id": OWNER_ID, "now": NOW},
        )

        self.assertEqual(old_exit, 2)
        self.assertEqual(old_response["error_code"], "inactive")
        self.assertEqual(fallback_exit, 0)
        self.assertEqual(fallback["review_id"], current["review_id"])
        states = {
            state["review_id"]: state["status"]
            for state in (
                json.loads(path.read_text(encoding="utf-8"))
                for path in self.inbox.glob("*/review.json")
            )
        }
        self.assertEqual(states[older["review_id"]], "superseded")
        self.assertEqual(states[current["review_id"]], "approved")

    def test_preflight_failure_is_non_terminal_and_leaves_candidate_pending(self):
        _, review = self.run_command("review", self.review_request())

        def fail_preflight():
            raise InboundPreflightError()

        exit_code, response = self.run_command(
            "approve",
            {
                "group_id": GROUP_ID,
                "sender_id": OWNER_ID,
                "review_id": review["review_id"],
                "now": NOW,
            },
            runtime_preflight=fail_preflight,
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            response,
            {
                "error_code": "preflight",
                "handled": True,
                "ok": False,
                "reply": "Roster not activated because production checks failed.",
            },
        )
        state_path = next(self.inbox.glob("*/review.json"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "pending")
        self.assertFalse(self.database.exists())

    def test_successful_approval_runs_the_command_boundary_verifier(self):
        review_exit, review = self.run_command("review", self.review_request())
        self.assertEqual(review_exit, 0)
        verified = []

        def verify(result):
            verified.append(result)
            self.assertEqual(
                self.database.active_roster().content_hash,
                result.content_hash,
            )

        approve_exit, approved = self.run_command(
            "approve",
            {
                "group_id": GROUP_ID,
                "sender_id": OWNER_ID,
                "review_id": review["review_id"],
                "now": NOW,
            },
            runtime_verifier=verify,
        )

        self.assertEqual(approve_exit, 0)
        self.assertTrue(approved["activated"])
        self.assertEqual(len(verified), 1)

    def test_post_apply_verification_failure_is_controlled(self):
        review_exit, review = self.run_command("review", self.review_request())
        self.assertEqual(review_exit, 0)

        def fail_verification(_result):
            raise InboundOperationalError()

        approve_exit, approved = self.run_command(
            "approve",
            {
                "group_id": GROUP_ID,
                "sender_id": OWNER_ID,
                "review_id": review["review_id"],
                "now": NOW,
            },
            runtime_verifier=fail_verification,
        )

        self.assertEqual(approve_exit, 2)
        self.assertEqual(
            approved,
            {
                "error_code": "operational_verification",
                "handled": True,
                "ok": False,
                "reply": "Roster activated, but operational verification failed.",
                "terminal": True,
            },
        )
        self.assertIsNotNone(self.database.active_roster())

        duplicate_exit, duplicate = self.run_command(
            "approve",
            {"group_id": GROUP_ID, "sender_id": OWNER_ID, "now": NOW},
        )
        self.assertEqual(duplicate_exit, 2)
        self.assertEqual(duplicate["reply"], "No roster awaiting approval.")
        self.assertEqual(len(self.database.pending_alerts()), 3)


if __name__ == "__main__":
    unittest.main()
