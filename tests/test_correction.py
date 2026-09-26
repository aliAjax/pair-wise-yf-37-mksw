import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
    WorkflowBlocked,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def make_confirmed_case(service, person_id="P-1"):
    case = service.create(
        Actor("admin", "admin"),
        "case",
        {
            "person_id": person_id,
            "onset_date": "2026-09-01",
            "location": "District-A",
            "symptoms": ["fever"],
        },
    )
    service.transition(Actor("dr-a", "clinician"), case["id"], "triage", {"clinician": "dr-a"})
    service.transition(
        Actor("lab-1", "lab"),
        case["id"],
        "lab_positive",
        {"lab_id": "L-1", "result": "positive"},
    )
    return service.get(case["id"])


def make_following_contact(service, case_id, person_id="P-2"):
    contact = service.create(
        Actor("inv-1", "investigator"),
        "contact",
        {"case_id": case_id, "person_id": person_id, "exposure_start": "2026-08-28"},
    )
    service.transition(
        Actor("inv-1", "investigator"),
        contact["id"],
        "begin_followup",
        {"followup_start": "2026-09-02", "due_at": "2026-09-16"},
    )
    return service.get(contact["id"])


CORRECTION = {
    "correction_id": "CORR-001",
    "lab_id": "L-2",
    "reason": "original sample contaminated; retest negative",
    "new_conclusion": "negative - awaiting final review",
}


class LabCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def test_correction_returns_confirmed_case_to_investigation(self):
        case = make_confirmed_case(self.service)
        original_lab = dict(case["data"])

        corrected = self.service.transition(
            Actor("lab-1", "lab"), case["id"], "correct_lab_result", CORRECTION
        )

        self.assertEqual(corrected["status"], "investigating")
        self.assertEqual(corrected["version"], case["version"] + 1)
        # The correction keeps the same case record, with prior confirmation
        # snapshotted inside the correction entry.
        self.assertEqual(corrected["id"], case["id"])
        record = corrected["data"]["corrections"][0]
        self.assertEqual(record["correction_id"], "CORR-001")
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["corrected_by"], "lab-1")
        self.assertEqual(record["prior"]["lab_id"], "L-1")
        self.assertEqual(record["prior"]["result"], "positive")
        self.assertEqual(record["prior"]["confirmed_by"], "lab-1")
        # Original confirmation fields are preserved: the correction keeps
        # the prior confirmation in its snapshot; audit history keeps the rest.
        self.assertEqual(corrected["data"]["lab_id"], "L-2")
        self.assertEqual(record["prior"]["lab_id"], original_lab["lab_id"])

    def test_confirmation_audit_and_contact_links_are_preserved(self):
        case = make_confirmed_case(self.service)
        contact = make_following_contact(self.service, case["id"])
        before_audit = self.service.audit_log(case["id"])
        actions_before = [item["action"] for item in before_audit]
        self.assertEqual(
            actions_before, ["create", "triage", "lab_positive"]
        )

        self.service.transition(
            Actor("lab-1", "lab"), case["id"], "correct_lab_result", CORRECTION
        )

        # Audit history is append-only: the original confirmation remains.
        actions = [item["action"] for item in self.service.audit_log(case["id"])]
        self.assertEqual(actions, ["create", "triage", "lab_positive", "correct_lab_result"])
        # The contact still points at the same case.
        self.assertEqual(self.service.get(contact["id"])["data"]["case_id"], case["id"])

    def test_contacts_cannot_complete_observation_while_awaiting_conclusion(self):
        case = make_confirmed_case(self.service)
        contact = make_following_contact(self.service, case["id"])

        self.service.transition(
            Actor("lab-1", "lab"), case["id"], "correct_lab_result", CORRECTION
        )

        with self.assertRaises(WorkflowBlocked) as context:
            self.service.transition(
                Actor("inv-1", "investigator"),
                contact["id"],
                "complete_followup",
                {"outcome": "no symptoms"},
            )
        message = str(context.exception)
        self.assertIn("complete_followup", message)
        self.assertIn(case["id"], message)
        # Contact state itself is untouched: still in follow-up.
        self.assertEqual(self.service.get(contact["id"])["status"], "following")

    def test_contact_observation_resumes_with_same_case_after_conclusion(self):
        case = make_confirmed_case(self.service)
        contact = make_following_contact(self.service, case["id"])
        self.service.transition(
            Actor("lab-1", "lab"), case["id"], "correct_lab_result", CORRECTION
        )

        with self.assertRaises(WorkflowBlocked):
            self.service.transition(
                Actor("inv-1", "investigator"),
                contact["id"],
                "complete_followup",
                {"outcome": "no symptoms"},
            )

        # Lab re-issues a positive conclusion on the same case record.
        reconfirmed = self.service.transition(
            Actor("lab-2", "lab"),
            case["id"],
            "lab_positive",
            {"lab_id": "L-3", "result": "positive"},
        )
        self.assertEqual(reconfirmed["id"], case["id"])
        self.assertEqual(reconfirmed["status"], "confirmed")
        record = reconfirmed["data"]["corrections"][0]
        self.assertEqual(record["status"], "resolved")
        self.assertEqual(record["resolved_via"], "lab_positive")
        self.assertTrue(record.get("resolved_at"))

        completed = self.service.transition(
            Actor("inv-1", "investigator"),
            contact["id"],
            "complete_followup",
            {"outcome": "no symptoms"},
        )
        self.assertEqual(completed["status"], "completed")

    def test_same_correction_submitted_twice_is_processed_once(self):
        case = make_confirmed_case(self.service)
        version_after_confirm = case["version"]

        first = self.service.transition(
            Actor("lab-1", "lab"), case["id"], "correct_lab_result", CORRECTION
        )
        self.assertEqual(first["version"], version_after_confirm + 1)

        # Retry / duplicate submission of the same correction document.
        second = self.service.transition(
            Actor("lab-1", "lab"), case["id"], "correct_lab_result", dict(CORRECTION)
        )
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["version"], first["version"])
        self.assertEqual(len(second["data"]["corrections"]), 1)
        audit_actions = [item["action"] for item in self.service.audit_log(case["id"])]
        self.assertEqual(audit_actions.count("correct_lab_result"), 1)

    def test_distinct_corrections_are_separate_documents(self):
        case = make_confirmed_case(self.service)
        first = self.service.transition(
            Actor("lab-1", "lab"), case["id"], "correct_lab_result", CORRECTION
        )
        reconfirmed = self.service.transition(
            Actor("lab-1", "lab"),
            case["id"],
            "lab_positive",
            {"lab_id": "L-3", "result": "positive"},
        )
        second_data = dict(CORRECTION)
        second_data["correction_id"] = "CORR-002"
        second = self.service.transition(
            Actor("lab-1", "lab"), case["id"], "correct_lab_result", second_data
        )
        self.assertEqual(second["version"], reconfirmed["version"] + 1)
        self.assertEqual(len(second["data"]["corrections"]), 2)
        self.assertEqual([c["status"] for c in second["data"]["corrections"]],
                         ["resolved", "pending"])

    def test_correction_requires_lab_role_and_basis(self):
        case = make_confirmed_case(self.service)
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("inv-1", "investigator"),
                case["id"],
                "correct_lab_result",
                CORRECTION,
            )
        incomplete = {k: v for k, v in CORRECTION.items() if k != "reason"}
        with self.assertRaises(ValidationError):
            self.service.transition(
                Actor("lab-1", "lab"), case["id"], "correct_lab_result", incomplete
            )

    def test_correction_only_from_confirmed_status(self):
        case = self.service.create(
            Actor("admin", "admin"),
            "case",
            {
                "person_id": "P-9",
                "onset_date": "2026-09-01",
                "location": "A",
                "symptoms": ["fever"],
            },
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                Actor("lab-1", "lab"), case["id"], "correct_lab_result", CORRECTION
            )

    def test_replay_of_resolved_correction_does_not_reopen_case(self):
        case = make_confirmed_case(self.service)
        self.service.transition(
            Actor("lab-1", "lab"), case["id"], "correct_lab_result", CORRECTION
        )
        self.service.transition(
            Actor("lab-2", "lab"),
            case["id"],
            "lab_positive",
            {"lab_id": "L-3", "result": "positive"},
        )
        # Late duplicate delivery of the original correction must not move the
        # case back to investigation again.
        replay = self.service.transition(
            Actor("lab-1", "lab"), case["id"], "correct_lab_result", dict(CORRECTION)
        )
        self.assertEqual(replay["status"], "confirmed")
        self.assertEqual(len(replay["data"]["corrections"]), 1)


if __name__ == "__main__":
    unittest.main()
