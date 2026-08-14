from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from roster.alerts import calculate_alerts
from roster.database import RosterDatabase
from roster.formatter import format_summary
from roster.inbound_review import (
    CandidateMismatchError,
    InboundAttachment,
    InboundAuthorizationError,
    InboundReviewService,
    NoAttachmentError,
    NoPendingCandidateError,
    ReviewExpiredError,
    ReviewEvaluation,
    ReviewInactiveError,
    ReviewRequest,
    UnresolvedReviewError,
)
from roster.models import ReviewIssue

from helpers import build, fly


GROUP_ID = "test-group"
OWNER_ID = "test-owner"
OTHER_GROUP_ID = "test-other-group"
UNAUTHORIZED_ID = "test-intruder"
REVIEW_ONLY_ID = "test-review-only"
NOW = datetime(2037, 8, 1, 0, 0, tzinfo=UTC)


def attachment(
    filename: str = "roster.png",
    *,
    content: bytes = b"synthetic roster image",
    content_type: str = "image/png",
) -> InboundAttachment:
    return InboundAttachment(
        filename=filename,
        content=content,
        content_type=content_type,
    )


class InboundReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.temporary = Path(self.directory.name)
        self.inbox = self.temporary / "runtime" / "private" / "inbox"
        self.database = RosterDatabase(self.temporary / "runtime" / "state.db")
        self.roster = build(
            [fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630")]
        )
        self.current_time = NOW
        self.evaluator = Mock(return_value=self.evaluation(self.roster))
        self.service = self.make_service(self.evaluator)

    def tearDown(self) -> None:
        self.directory.cleanup()

    @staticmethod
    def evaluation(roster) -> ReviewEvaluation:
        alerts = tuple(calculate_alerts(roster, now=NOW))
        return ReviewEvaluation(
            roster=roster,
            summary=format_summary(roster),
            alerts=alerts,
            issues=tuple(roster.issues),
        )

    def make_service(
        self,
        evaluator,
        *,
        allowed_sender_ids: frozenset[str] | None = None,
        allowed_state_change_sender_ids: frozenset[str] | None = None,
    ) -> InboundReviewService:
        return InboundReviewService(
            inbox_root=self.inbox,
            database=self.database,
            allowed_group_id=GROUP_ID,
            allowed_sender_ids=allowed_sender_ids or frozenset({OWNER_ID}),
            allowed_state_change_sender_ids=allowed_state_change_sender_ids,
            evaluator=evaluator,
            now=lambda: self.current_time,
        )

    def request(
        self,
        *attachments: InboundAttachment,
        group_id: str = GROUP_ID,
        sender_id: str = OWNER_ID,
        mentioned: bool = True,
    ) -> ReviewRequest:
        return ReviewRequest(
            group_id=group_id,
            sender_id=sender_id,
            mentioned=mentioned,
            attachments=tuple(attachments),
        )

    def review_one(self):
        return self.service.review(self.request(attachment()))

    def test_wrong_group_or_unauthorized_sender_is_rejected(self):
        for group_id, sender_id in (
            (OTHER_GROUP_ID, OWNER_ID),
            (GROUP_ID, UNAUTHORIZED_ID),
        ):
            with self.subTest(group_id=group_id, sender_id=sender_id):
                with self.assertRaises(InboundAuthorizationError):
                    self.service.review(
                        self.request(
                            attachment(),
                            group_id=group_id,
                            sender_id=sender_id,
                        )
                    )
        self.evaluator.assert_not_called()
        self.assertFalse(self.database.exists())

    def test_review_authorized_sender_cannot_approve_or_revise_without_state_permission(self):
        service = self.make_service(
            self.evaluator,
            allowed_sender_ids=frozenset({OWNER_ID, REVIEW_ONLY_ID}),
            allowed_state_change_sender_ids=frozenset({OWNER_ID}),
        )
        reviewed = service.review(
            self.request(attachment(), sender_id=REVIEW_ONLY_ID)
        )

        for action in (
            lambda: service.approve(
                group_id=GROUP_ID,
                sender_id=REVIEW_ONLY_ID,
                review_id=reviewed.review_id,
            ),
            lambda: service.revise(
                group_id=GROUP_ID,
                sender_id=REVIEW_ONLY_ID,
                review_id=reviewed.review_id,
            ),
        ):
            with self.subTest(action=action):
                with self.assertRaises(InboundAuthorizationError):
                    action()

        state = json.loads(reviewed.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "pending")
        self.assertFalse(self.database.exists())

    def test_authorized_owner_without_mention_does_not_trigger(self):
        result = self.service.review(
            self.request(attachment(), mentioned=False)
        )

        self.assertIsNone(result)
        self.evaluator.assert_not_called()
        self.assertFalse(self.inbox.exists())
        self.assertFalse(self.database.exists())

    def test_authorized_owner_with_mention_triggers_review(self):
        result = self.review_one()

        self.assertIsNotNone(result)
        self.evaluator.assert_called_once()
        self.assertEqual(result.group_id, GROUP_ID)
        self.assertEqual(result.sender_id, OWNER_ID)
        self.assertEqual(result.duty_count, 1)
        self.assertEqual(result.flight_count, 1)
        self.assertEqual(result.future_alert_count, 3)
        self.assertRegex(result.review_id, r"^[A-Za-z0-9_-]{16}$")
        self.assertEqual(result.expires_at, NOW + timedelta(hours=24))
        self.assertTrue(result.approvable)

        state = json.loads(result.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(state["status"], "pending")
        self.assertEqual(state["review_id"], result.review_id)
        self.assertEqual(state["ingestion_id"], result.ingestion_id)
        self.assertEqual(state["group_id"], GROUP_ID)
        self.assertEqual(state["sender_id"], OWNER_ID)
        self.assertEqual(
            datetime.fromisoformat(state["expires_at"].replace("Z", "+00:00")),
            NOW + timedelta(hours=24),
        )

    def test_no_attachment_returns_controlled_instruction(self):
        with self.assertRaisesRegex(
            NoAttachmentError,
            "Please attach the roster screenshot or PDF",
        ):
            self.service.review(self.request())

        self.evaluator.assert_not_called()
        self.assertFalse(self.database.exists())

    def test_jpg_jpeg_png_and_pdf_are_accepted(self):
        cases = (
            ("roster.jpg", "image/jpeg"),
            ("roster.JPEG", "image/jpeg"),
            ("roster.png", "image/png"),
            ("roster.pdf", "application/pdf"),
        )
        for filename, content_type in cases:
            with self.subTest(filename=filename):
                directory = self.temporary / filename.lower().replace(".", "-")
                evaluator = Mock(return_value=self.evaluation(self.roster))
                service = InboundReviewService(
                    inbox_root=directory,
                    database=RosterDatabase(directory / "state.db"),
                    allowed_group_id=GROUP_ID,
                    allowed_sender_ids=frozenset({OWNER_ID}),
                    evaluator=evaluator,
                    now=lambda: NOW,
                )
                result = service.review(
                    self.request(attachment(filename, content_type=content_type))
                )

                evaluator.assert_called_once()
                self.assertEqual(len(result.source_paths), 1)
                self.assertEqual(
                    result.source_paths[0].suffix.lower(),
                    Path(filename).suffix.lower(),
                )

    def test_several_images_are_evaluated_as_one_candidate(self):
        result = self.service.review(
            self.request(
                attachment(
                    "page-1.jpg",
                    content=b"page one",
                    content_type="image/jpeg",
                ),
                attachment("page-2.png", content=b"page two"),
                attachment(
                    "page-3.jpeg",
                    content=b"page three",
                    content_type="image/jpeg",
                ),
            )
        )

        self.evaluator.assert_called_once()
        evaluated_paths = tuple(self.evaluator.call_args.args[0])
        self.assertEqual(evaluated_paths, result.source_paths)
        self.assertEqual(len(evaluated_paths), 3)
        self.assertEqual(
            {path.read_bytes() for path in evaluated_paths},
            {b"page one", b"page two", b"page three"},
        )

    def test_sources_are_saved_in_a_private_unique_path_with_safe_names(self):
        result = self.service.review(
            self.request(attachment("../../outside/roster.png"))
        )

        inbox = self.inbox.resolve()
        ingestion = result.ingestion_directory.resolve()
        saved = result.source_paths[0].resolve()
        self.assertTrue(ingestion.is_relative_to(inbox))
        self.assertTrue(saved.is_relative_to(ingestion))
        self.assertEqual(saved.parent, ingestion)
        self.assertNotIn("..", saved.name)
        self.assertNotIn("/", saved.name)
        self.assertEqual(
            stat.S_IMODE(result.ingestion_directory.stat().st_mode),
            0o700,
        )
        self.assertEqual(stat.S_IMODE(result.source_paths[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(result.state_path.stat().st_mode), 0o600)
        self.assertFalse((self.temporary / "outside" / "roster.png").exists())

    def test_dry_run_never_initializes_or_applies_to_database(self):
        with patch.object(
            self.database,
            "apply_roster",
            wraps=self.database.apply_roster,
        ) as apply_roster:
            result = self.review_one()

        self.assertIsNotNone(result)
        apply_roster.assert_not_called()
        self.assertFalse(self.database.exists())
        self.assertIsNone(self.database.active_roster())

    def test_approval_without_pending_candidate_is_rejected(self):
        with self.assertRaisesRegex(
            NoPendingCandidateError,
            "No roster awaiting approval",
        ):
            self.service.approve(group_id=GROUP_ID, sender_id=OWNER_ID)

        self.assertFalse(self.database.exists())

    def test_exact_pending_candidate_can_be_cancelled_without_database_changes(self):
        reviewed = self.review_one()

        cancelled = self.service.cancel_pending(
            group_id=GROUP_ID,
            sender_id=OWNER_ID,
            ingestion_id=reviewed.ingestion_id,
        )

        self.assertEqual(cancelled, reviewed.ingestion_id)
        state = json.loads(reviewed.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "superseded")
        self.assertIn("superseded_at", state)
        self.assertTrue(reviewed.source_paths[0].is_file())
        self.assertFalse(self.database.exists())
        with self.assertRaises(NoPendingCandidateError):
            self.service.approve(group_id=GROUP_ID, sender_id=OWNER_ID)

    def test_cancel_rejects_a_different_candidate_identity(self):
        reviewed = self.review_one()

        with self.assertRaises(CandidateMismatchError):
            self.service.cancel_pending(
                group_id=GROUP_ID,
                sender_id=OWNER_ID,
                ingestion_id="different-ingestion",
            )

        state = json.loads(reviewed.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "pending")
        self.assertFalse(self.database.exists())

    def test_approval_with_unresolved_needs_review_is_rejected(self):
        roster = build(
            [fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630")]
        )
        roster.issues.append(
            ReviewIssue("synthetic_issue", "Synthetic field needs review")
        )
        evaluator = Mock(return_value=self.evaluation(roster))
        service = self.make_service(evaluator)
        reviewed = service.review(self.request(attachment()))

        self.assertFalse(reviewed.approvable)
        state = json.loads(reviewed.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "pending")

        with self.assertRaises(UnresolvedReviewError):
            service.approve(
                group_id=GROUP_ID,
                sender_id=OWNER_ID,
                review_id=reviewed.review_id,
            )

        self.assertFalse(self.database.exists())
        self.assertIsNone(self.database.active_roster())

    def test_approval_rejects_changed_source_hash(self):
        reviewed = self.review_one()
        reviewed.source_paths[0].write_bytes(b"changed after review")

        with self.assertRaises(CandidateMismatchError):
            self.service.approve(
                group_id=GROUP_ID,
                sender_id=OWNER_ID,
                review_id=reviewed.review_id,
            )

        self.assertFalse(self.database.exists())
        state = json.loads(reviewed.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "superseded")

    def test_approval_rejects_changed_reviewed_content_hash(self):
        reviewed = self.review_one()
        state = reviewed.state_path.read_text(encoding="utf-8")
        self.assertIn(self.roster.content_hash, state)
        reviewed.state_path.write_text(
            state.replace(self.roster.content_hash, "0" * 64, 1),
            encoding="utf-8",
        )

        with self.assertRaises(CandidateMismatchError):
            self.service.approve(
                group_id=GROUP_ID,
                sender_id=OWNER_ID,
                review_id=reviewed.review_id,
            )

        self.assertFalse(self.database.exists())
        state = json.loads(reviewed.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "superseded")

    def test_approval_rejects_tampered_transcription_evidence(self):
        evidence = {
            "schema_version": 1,
            "report_header": {"period_from": "01Aug37"},
            "rows": [],
        }
        evaluation = ReviewEvaluation(
            roster=self.roster,
            summary=format_summary(self.roster),
            alerts=tuple(calculate_alerts(self.roster, now=NOW)),
            issues=tuple(self.roster.issues),
            evidence=evidence,
        )
        evaluator = Mock(return_value=evaluation)
        service = self.make_service(evaluator)
        reviewed = service.review(self.request(attachment()))
        transcription_path = reviewed.ingestion_directory / "transcription.json"
        transcription_path.write_text('{"tampered":true}\n', encoding="utf-8")

        with self.assertRaises(CandidateMismatchError):
            service.approve(
                group_id=GROUP_ID,
                sender_id=OWNER_ID,
                review_id=reviewed.review_id,
            )

        evaluator.assert_called_once()
        self.assertFalse(self.database.exists())
        state = json.loads(reviewed.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "superseded")

    def test_successful_approval_applies_exactly_once(self):
        reviewed = self.review_one()

        with patch.object(
            self.database,
            "apply_roster",
            wraps=self.database.apply_roster,
        ) as apply_roster:
            result = self.service.approve(
                group_id=GROUP_ID,
                sender_id=OWNER_ID,
                review_id=reviewed.review_id,
            )

        apply_roster.assert_called_once()
        self.assertEqual(result.review_id, reviewed.review_id)
        self.assertTrue(result.activated)
        self.assertFalse(result.updated)
        self.assertFalse(result.unchanged)
        self.assertEqual(result.flight_count, 1)
        self.assertEqual(result.future_alert_count, 3)
        self.assertEqual(self.database.status_counts()["roster_versions"], 1)
        self.assertEqual(self.database.status_counts()["pending_alerts"], 3)
        self.assertEqual(
            self.database.active_roster().content_hash,
            self.roster.content_hash,
        )
        state = json.loads(reviewed.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "approved")
        self.assertEqual(state["approved_by"], OWNER_ID)

    def test_duplicate_approval_cannot_duplicate_alerts(self):
        reviewed = self.review_one()
        self.service.approve(
            group_id=GROUP_ID,
            sender_id=OWNER_ID,
            review_id=reviewed.review_id,
        )
        first_keys = [alert.event_key for alert in self.database.pending_alerts()]

        with self.assertRaises(ReviewInactiveError):
            self.service.approve(
                group_id=GROUP_ID,
                sender_id=OWNER_ID,
                review_id=reviewed.review_id,
            )

        final_keys = [alert.event_key for alert in self.database.pending_alerts()]
        self.assertEqual(final_keys, first_keys)
        self.assertEqual(len(final_keys), len(set(final_keys)))
        with sqlite3.connect(self.database.path) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM roster_versions").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM alerts").fetchone()[0],
                3,
            )

    def test_new_review_supersedes_old_exact_id_and_fallback_approves_current(self):
        older_roster = build(
            [fly("06Aug37", "ZX410", "SIN-TPE", "0945", "1145", "1630")]
        )
        latest_roster = build(
            [fly("07Aug37", "ZX411", "TPE-SIN", "1645", "1745", "2215")]
        )

        def evaluate(paths):
            roster = (
                older_roster
                if paths[0].read_bytes() == b"older candidate"
                else latest_roster
            )
            return self.evaluation(roster)

        service = self.make_service(evaluate)
        older = service.review(
            self.request(attachment(content=b"older candidate"))
        )
        latest = service.review(
            self.request(attachment(content=b"latest candidate"))
        )

        older_state = json.loads(older.state_path.read_text(encoding="utf-8"))
        latest_state = json.loads(latest.state_path.read_text(encoding="utf-8"))
        self.assertEqual(older_state["status"], "superseded")
        self.assertEqual(latest_state["status"], "pending")
        with self.assertRaises(ReviewInactiveError):
            service.approve(
                group_id=GROUP_ID,
                sender_id=OWNER_ID,
                review_id=older.review_id,
            )
        with self.assertRaises(ReviewInactiveError):
            service.revise(
                group_id=GROUP_ID,
                sender_id=OWNER_ID,
                review_id=older.review_id,
            )

        approved = service.approve(group_id=GROUP_ID, sender_id=OWNER_ID)
        self.assertEqual(approved.review_id, latest.review_id)
        self.assertEqual(
            self.database.active_roster().content_hash,
            latest_roster.content_hash,
        )

        with self.assertRaisesRegex(
            NoPendingCandidateError,
            "No roster awaiting approval",
        ):
            service.approve(group_id=GROUP_ID, sender_id=OWNER_ID)

        self.assertEqual(
            self.database.active_roster().content_hash,
            latest_roster.content_hash,
        )

    def test_review_is_valid_until_but_not_at_24_hour_boundary(self):
        reviewed = self.review_one()
        self.current_time = NOW + timedelta(hours=24) - timedelta(microseconds=1)

        approved = self.service.approve(
            group_id=GROUP_ID,
            sender_id=OWNER_ID,
            review_id=reviewed.review_id,
        )

        self.assertEqual(approved.review_id, reviewed.review_id)

    def test_review_expires_at_24_hour_boundary(self):
        reviewed = self.review_one()
        self.current_time = NOW + timedelta(hours=24)

        with self.assertRaises(ReviewExpiredError):
            self.service.approve(
                group_id=GROUP_ID,
                sender_id=OWNER_ID,
                review_id=reviewed.review_id,
            )

        state = json.loads(reviewed.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "expired")
        self.assertFalse(self.database.exists())

    def test_revise_is_one_time_and_approve_after_revise_is_inactive(self):
        reviewed = self.review_one()

        result = self.service.revise(
            group_id=GROUP_ID,
            sender_id=OWNER_ID,
            review_id=reviewed.review_id,
        )

        self.assertEqual(result.review_id, reviewed.review_id)
        self.assertEqual(result.status, "revision_requested")
        state = json.loads(reviewed.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "revision_requested")
        self.assertEqual(state["revision_requested_by"], OWNER_ID)
        for action in (
            lambda: self.service.revise(
                group_id=GROUP_ID,
                sender_id=OWNER_ID,
                review_id=reviewed.review_id,
            ),
            lambda: self.service.approve(
                group_id=GROUP_ID,
                sender_id=OWNER_ID,
                review_id=reviewed.review_id,
            ),
        ):
            with self.subTest(action=action):
                with self.assertRaises(ReviewInactiveError):
                    action()
        self.assertFalse(self.database.exists())

    def test_revise_after_approve_is_inactive_and_does_not_change_database(self):
        reviewed = self.review_one()
        self.service.approve(
            group_id=GROUP_ID,
            sender_id=OWNER_ID,
            review_id=reviewed.review_id,
        )
        keys_before = [alert.event_key for alert in self.database.pending_alerts()]

        with self.assertRaises(ReviewInactiveError):
            self.service.revise(
                group_id=GROUP_ID,
                sender_id=OWNER_ID,
                review_id=reviewed.review_id,
            )

        state = json.loads(reviewed.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "approved")
        self.assertEqual(
            [alert.event_key for alert in self.database.pending_alerts()],
            keys_before,
        )


if __name__ == "__main__":
    unittest.main()
