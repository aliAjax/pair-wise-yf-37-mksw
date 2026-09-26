import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class CorrectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.lab = Actor("lab-1", "lab")
        self.investigator = Actor("inv-1", "investigator")

    def tearDown(self):
        self.tmp.cleanup()

    def _confirmed_case_with_contact(self):
        case = self.service.create(
            self.admin,
            "case",
            {"person_id": "P-1", "onset_date": "2026-03-01", "location": "District-A", "symptoms": ["fever"]},
        )
        self.service.transition(self.admin, case["id"], "triage", {"clinician": "C-1"})
        case = self.service.transition(
            self.lab, case["id"], "lab_positive", {"lab_id": "L-1", "result": "positive"}
        )
        contact = self.service.create(
            self.admin,
            "contact",
            {"case_id": case["id"], "person_id": "P-2", "exposure_start": "2026-03-02"},
        )
        self.service.transition(
            self.investigator,
            contact["id"],
            "begin_followup",
            {"followup_start": "2026-03-02", "due_at": "2026-03-16"},
        )
        return case, contact

    def _correct(self, case_id, correction_id="CORR-1"):
        return self.service.transition(
            self.lab,
            case_id,
            "correct_lab_result",
            {"correction_id": correction_id, "reason": "样本污染，复检阴性", "corrected_result": "negative"},
        )

    def test_correction_reopens_case_and_preserves_records(self):
        case, contact = self._confirmed_case_with_contact()
        corrected = self._correct(case["id"])
        self.assertEqual(corrected["status"], "investigating")
        self.assertEqual(corrected["data"]["result"], "positive")
        self.assertEqual(corrected["data"]["confirmed_by"], "lab-1")
        correction = corrected["data"]["correction"]
        self.assertEqual(correction["status"], "pending")
        self.assertEqual(correction["reason"], "样本污染，复检阴性")
        self.assertEqual(correction["corrected_result"], "negative")
        self.assertEqual(correction["previous_confirmation"]["result"], "positive")
        self.assertEqual(correction["previous_confirmation"]["lab_id"], "L-1")
        stored_contact = self.service.get(contact["id"])
        self.assertEqual(stored_contact["data"]["case_id"], case["id"])
        actions = [row["action"] for row in self.service.audit_log(case["id"])]
        self.assertEqual(actions, ["create", "triage", "lab_positive", "correct_lab_result"])

    def test_contact_cannot_complete_followup_while_correction_pending(self):
        case, contact = self._confirmed_case_with_contact()
        self._correct(case["id"])
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                self.investigator, contact["id"], "complete_followup", {"outcome": "no symptoms"}
            )
        message = str(ctx.exception)
        self.assertIn("complete_followup", message)
        self.assertIn("CORR-1", message)
        self.assertIn(case["id"], message)

    def test_conclusion_restored_on_same_case_unblocks_contact(self):
        case, contact = self._confirmed_case_with_contact()
        self._correct(case["id"])
        restored = self.service.transition(
            self.lab, case["id"], "lab_positive", {"lab_id": "L-2", "result": "positive"}
        )
        self.assertEqual(restored["id"], case["id"])
        self.assertEqual(restored["status"], "confirmed")
        self.assertEqual(restored["data"]["correction"]["status"], "resolved")
        self.assertEqual(restored["data"]["correction"]["resolution"], "confirmed")
        done = self.service.transition(
            self.investigator, contact["id"], "complete_followup", {"outcome": "no symptoms"}
        )
        self.assertEqual(done["status"], "completed")

    def test_probable_conclusion_also_resolves_correction(self):
        case, contact = self._confirmed_case_with_contact()
        self._correct(case["id"])
        restored = self.service.transition(
            self.investigator, case["id"], "mark_probable", {"epi_link": "cluster-7"}
        )
        self.assertEqual(restored["status"], "probable")
        self.assertEqual(restored["data"]["correction"]["status"], "resolved")
        self.assertEqual(restored["data"]["correction"]["resolution"], "probable")
        done = self.service.transition(
            self.investigator, contact["id"], "complete_followup", {"outcome": "no symptoms"}
        )
        self.assertEqual(done["status"], "completed")

    def test_same_correction_submitted_twice_processed_once(self):
        case, _ = self._confirmed_case_with_contact()
        first = self._correct(case["id"])
        second = self._correct(case["id"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["version"], first["version"])
        audits = [
            row for row in self.service.audit_log(case["id"]) if row["action"] == "correct_lab_result"
        ]
        self.assertEqual(len(audits), 1)
        self.service.transition(
            self.lab, case["id"], "lab_positive", {"lab_id": "L-2", "result": "positive"}
        )
        third = self._correct(case["id"])
        self.assertEqual(third["status"], "confirmed")
        audits = [
            row for row in self.service.audit_log(case["id"]) if row["action"] == "correct_lab_result"
        ]
        self.assertEqual(len(audits), 1)

    def test_different_correction_while_pending_rejected(self):
        case, _ = self._confirmed_case_with_contact()
        self._correct(case["id"])
        with self.assertRaises(InvalidTransition):
            self._correct(case["id"], correction_id="CORR-2")

    def test_correction_requires_confirmed_case(self):
        case = self.service.create(
            self.admin,
            "case",
            {"person_id": "P-1", "onset_date": "2026-03-01", "location": "District-A", "symptoms": ["fever"]},
        )
        with self.assertRaises(InvalidTransition):
            self._correct(case["id"])

    def test_correction_requires_lab_role(self):
        case, _ = self._confirmed_case_with_contact()
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("c-1", "clinician"),
                case["id"],
                "correct_lab_result",
                {"correction_id": "CORR-1", "reason": "复检阴性", "corrected_result": "negative"},
            )

    def test_correction_requires_reason_and_result(self):
        case, _ = self._confirmed_case_with_contact()
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.lab, case["id"], "correct_lab_result", {"correction_id": "CORR-1"}
            )


if __name__ == "__main__":
    unittest.main()
