import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, DomainError, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class CoordinationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")
        self.registrar = Actor("reg", "registrar")
        self.vet = Actor("vet", "veterinarian")

    def tearDown(self):
        self.tmp.cleanup()

    def _animal(self, name, sex, **extra):
        data = {"name": name, "sex": sex}
        data.update(extra)
        return self.service.create(self.actor, "animal", data)

    def _pairing(self, sire_id, dam_id):
        pairing = self.service.create(
            self.actor, "pairing", {"proposed_by": "coordinator"}
        )
        return pairing

    def _approve(self, pairing_id, sire_id, dam_id, expected_version=1, actor=None):
        return self.service.transition(
            actor or self.actor,
            pairing_id,
            "approve",
            {"sire_id": sire_id, "dam_id": dam_id, "approvals": ["vet-1"]},
            expected_version,
        )

    # ---- optimistic concurrency on approval -------------------------------

    def test_concurrent_approval_only_one_succeeds(self):
        sire = self._animal("M-1", "male")
        dam = self._animal("F-1", "female")
        pairing = self._pairing(sire["id"], dam["id"])

        barrier = threading.Barrier(2)
        outcomes = []

        def submit():
            barrier.wait()
            try:
                result = self._approve(pairing["id"], sire["id"], dam["id"], 1)
                outcomes.append(("ok", result))
            except Exception as exc:  # noqa: BLE001 - capture any failure
                outcomes.append(("err", exc))

        first = threading.Thread(target=submit)
        second = threading.Thread(target=submit)
        first.start()
        second.start()
        first.join()
        second.join()

        oks = [item for item in outcomes if item[0] == "ok"]
        errs = [item for item in outcomes if item[0] == "err"]
        self.assertEqual(len(oks), 1, "exactly one approval should succeed")
        self.assertEqual(len(errs), 1, "the late approval should fail")
        self.assertIsInstance(
            errs[0][1], ConflictError, "late approval must see a version conflict"
        )
        current = self.service.get(pairing["id"])
        self.assertEqual(current["status"], "approved")
        self.assertEqual(current["version"], 2)

    def test_stale_expected_version_conflicts(self):
        sire = self._animal("M-2", "male")
        dam = self._animal("F-2", "female")
        pairing = self._pairing(sire["id"], dam["id"])
        # Both readers observe version 1; the first commit wins.
        self._approve(pairing["id"], sire["id"], dam["id"], 1)
        with self.assertRaises(ConflictError):
            self._approve(pairing["id"], sire["id"], dam["id"], 1)

    # ---- approval invalidation on animal changes --------------------------

    def test_quarantine_invalidates_unexecuted_approval(self):
        sire = self._animal("M-3", "male")
        dam = self._animal("F-3", "female")
        pairing = self._pairing(sire["id"], dam["id"])
        self._approve(pairing["id"], sire["id"], dam["id"])

        self.service.transition(
            self.vet, sire["id"], "quarantine_animal", {"reason": "health check"}
        )

        current = self.service.get(pairing["id"])
        self.assertEqual(current["status"], "invalidated")
        self.assertIn("quarantined", current["data"]["invalidation_reason"])
        self.assertIn("health check", current["data"]["invalidation_reason"])
        self.assertIn("invalidated_at", current["data"])

    def test_pedigree_update_invalidates_unexecuted_approval(self):
        sire = self._animal("M-4", "male")
        dam = self._animal("F-4", "female")
        pairing = self._pairing(sire["id"], dam["id"])
        self._approve(pairing["id"], sire["id"], dam["id"])

        self.service.transition(
            self.actor,
            sire["id"],
            "update_pedigree",
            {"sire_id": "new-sire-99"},
        )

        current = self.service.get(pairing["id"])
        self.assertEqual(current["status"], "invalidated")
        reason = current["data"]["invalidation_reason"]
        self.assertIn("pedigree", reason)
        self.assertIn("sire_id", reason)

    def test_death_invalidates_unexecuted_approval(self):
        sire = self._animal("M-5", "male")
        dam = self._animal("F-5", "female")
        pairing = self._pairing(sire["id"], dam["id"])
        self._approve(pairing["id"], sire["id"], dam["id"])

        self.service.transition(
            self.vet, sire["id"], "mark_deceased", {"cause": "illness"}
        )

        current = self.service.get(pairing["id"])
        self.assertEqual(current["status"], "invalidated")
        self.assertIn("deceased", current["data"]["invalidation_reason"])

    def test_completed_pairing_keeps_offspring_after_invalidation(self):
        sire = self._animal("M-6", "male")
        dam = self._animal("F-6", "female")
        pairing = self._pairing(sire["id"], dam["id"])
        self._approve(pairing["id"], sire["id"], dam["id"])
        self.service.transition(
            self.actor,
            pairing["id"],
            "complete",
            {"offspring_ids": ["off-1", "off-2"]},
        )

        self.service.transition(
            self.vet, sire["id"], "quarantine_animal", {"reason": "check"}
        )

        current = self.service.get(pairing["id"])
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["data"]["offspring_ids"], ["off-1", "off-2"])
        self.assertNotIn("invalidation_reason", current["data"])

    def test_invalidated_pairing_cannot_complete(self):
        sire = self._animal("M-7", "male")
        dam = self._animal("F-7", "female")
        pairing = self._pairing(sire["id"], dam["id"])
        self._approve(pairing["id"], sire["id"], dam["id"])
        self.service.transition(
            self.vet, sire["id"], "quarantine_animal", {"reason": "check"}
        )

        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.actor,
                pairing["id"],
                "complete",
                {"offspring_ids": ["off-1"]},
            )

    def test_invalidation_audit_records_reason(self):
        sire = self._animal("M-8", "male")
        dam = self._animal("F-8", "female")
        pairing = self._pairing(sire["id"], dam["id"])
        self._approve(pairing["id"], sire["id"], dam["id"])
        self.service.transition(
            self.vet, sire["id"], "quarantine_animal", {"reason": "check"}
        )

        entries = self.service.audit_log(pairing["id"])
        invalidate_entries = [e for e in entries if e["action"] == "invalidate"]
        self.assertEqual(len(invalidate_entries), 1)
        self.assertEqual(invalidate_entries[0]["to_status"], "invalidated")
        self.assertIn("quarantined", invalidate_entries[0]["detail"]["reason"])

    # ---- transport connection and idempotent retry ------------------------

    def test_transfer_blocked_when_animal_quarantined(self):
        sire = self._animal("M-9", "male")
        transfer = self.service.create(
            self.registrar,
            "transfer",
            {
                "animal_id": sire["id"],
                "from_institution": "Zoo-A",
                "to_institution": "Zoo-B",
            },
        )
        self.service.transition(
            self.vet, sire["id"], "quarantine_animal", {"reason": "check"}
        )

        with self.assertRaises(ValidationError):
            self.service.transition(
                self.registrar, transfer["id"], "authorize", {"permit_id": "P-1"}
            )

    def test_transfer_retry_resumes_and_does_not_duplicate(self):
        sire = self._animal("M-10", "male")
        transfer = self.service.create(
            self.registrar,
            "transfer",
            {
                "animal_id": sire["id"],
                "from_institution": "Zoo-A",
                "to_institution": "Zoo-B",
            },
        )
        self.service.transition(
            self.registrar, transfer["id"], "authorize", {"permit_id": "P-1"}
        )

        # Ship fails before committing: the record stays at authorized.
        with self.assertRaises(DomainError):
            self.service.transition(
                self.registrar,
                transfer["id"],
                "ship",
                {"transport_id": "T-1", "simulate_failure": True},
            )
        after_failure = self.service.get(transfer["id"])
        self.assertEqual(after_failure["status"], "authorized")
        self.assertNotIn("occupied", after_failure["data"])

        # Retry ship: resumes from the completed authorize step.
        shipped = self.service.transition(
            self.registrar, transfer["id"], "ship", {"transport_id": "T-1"}
        )
        self.assertEqual(shipped["status"], "in_transit")
        self.assertTrue(shipped["data"]["occupied"])

        # Repeated ship is a no-op: occupation is not applied again.
        retry = self.service.transition(
            self.registrar, transfer["id"], "ship", {"transport_id": "T-1"}
        )
        self.assertEqual(retry["status"], "in_transit")
        self.assertTrue(retry["data"]["occupied"])

        # Arrive and retry arrive: no duplicate side effects.
        arrived = self.service.transition(
            self.registrar, transfer["id"], "arrive", {"arrival_date": "2026-05-01"}
        )
        self.assertEqual(arrived["status"], "completed")
        retry_arrive = self.service.transition(
            self.registrar, transfer["id"], "arrive", {"arrival_date": "2026-05-01"}
        )
        self.assertEqual(retry_arrive["status"], "completed")

    def test_transfer_with_pairing_blocked_after_invalidation(self):
        sire = self._animal("M-11", "male")
        dam = self._animal("F-11", "female")
        pairing = self._pairing(sire["id"], dam["id"])
        self._approve(pairing["id"], sire["id"], dam["id"])
        transfer = self.service.create(
            self.registrar,
            "transfer",
            {
                "animal_id": sire["id"],
                "pairing_id": pairing["id"],
                "from_institution": "Zoo-A",
                "to_institution": "Zoo-B",
            },
        )
        self.service.transition(
            self.registrar, transfer["id"], "authorize", {"permit_id": "P-1"}
        )
        # Quarantine invalidates the pairing and blocks transport.
        self.service.transition(
            self.vet, sire["id"], "quarantine_animal", {"reason": "check"}
        )

        with self.assertRaises(ValidationError):
            self.service.transition(
                self.registrar, transfer["id"], "ship", {"transport_id": "T-1"}
            )

    def test_pairing_complete_is_idempotent(self):
        sire = self._animal("M-12", "male")
        dam = self._animal("F-12", "female")
        pairing = self._pairing(sire["id"], dam["id"])
        self._approve(pairing["id"], sire["id"], dam["id"])
        self.service.transition(
            self.actor, pairing["id"], "complete", {"offspring_ids": ["off-1"]}
        )
        # Retrying complete does not re-register offspring.
        retry = self.service.transition(
            self.actor, pairing["id"], "complete", {"offspring_ids": ["off-1"]}
        )
        self.assertEqual(retry["status"], "completed")
        self.assertEqual(retry["data"]["offspring_ids"], ["off-1"])


# Actor import placed at bottom to keep the test body concise.
from src.domain import Actor  # noqa: E402

if __name__ == "__main__":
    unittest.main()
