"""The two coordination halves (pairing approval and transport) joined:
optimistic approval, stale-approval invalidation, and idempotent retries.
"""
import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ApprovalInvalidatedError,
    ConflictError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class CoordinationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine()
        )
        self.admin = Actor("admin", "admin")
        self.coordinator = Actor("coord-1", "coordinator")
        self.registrar = Actor("reg-1", "registrar")

    def tearDown(self):
        self.tmp.cleanup()

    def _breeding_pair(self):
        sire = self.service.create(
            self.admin, "animal", {"name": "S", "sex": "male"}
        )
        dam = self.service.create(
            self.admin, "animal", {"name": "D", "sex": "female"}
        )
        return sire, dam

    def _approved_pairing(self, sire, dam):
        pairing = self.service.create(
            self.coordinator, "pairing", {"proposed_by": "coord-1"}
        )
        approved = self.service.transition(
            self.coordinator,
            pairing["id"],
            "approve",
            {"sire_id": sire["id"], "dam_id": dam["id"], "approvals": ["vet-1"]},
            expected_version=1,
        )
        self.assertEqual(approved["status"], "approved")
        return pairing

    # -- concurrent approval ---------------------------------------------

    def test_two_concurrent_approvals_only_one_succeeds(self):
        sire, dam = self._breeding_pair()
        pairing = self.service.create(
            self.coordinator, "pairing", {"proposed_by": "coord-1"}
        )
        outcomes = {}
        barrier = threading.Barrier(2)

        def approve(user_id):
            barrier.wait()
            try:
                self.service.transition(
                    Actor(user_id, "coordinator"),
                    pairing["id"],
                    "approve",
                    {"sire_id": sire["id"], "dam_id": dam["id"],
                     "approvals": ["vet-1"]},
                    expected_version=1,
                )
                outcomes[user_id] = "approved"
            except ConflictError as exc:
                outcomes[user_id] = ("conflict", exc.details["current_version"])

        threads = [
            threading.Thread(target=approve, args=("u-1",)),
            threading.Thread(target=approve, args=("u-2",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(outcomes.values(), key=str),
                         [("conflict", 2), "approved"])
        self.assertEqual(self.service.get(pairing["id"])["version"], 2)

    def test_late_approver_with_stale_version_sees_conflict(self):
        sire, dam = self._breeding_pair()
        pairing = self._approved_pairing(sire, dam)
        with self.assertRaises(ConflictError) as caught:
            self.service.transition(
                Actor("late", "coordinator"),
                pairing["id"],
                "approve",
                {"sire_id": sire["id"], "dam_id": dam["id"]},
                expected_version=1,
            )
        self.assertEqual(caught.exception.details["current_version"], 2)

    # -- stale approval invalidation --------------------------------------

    def test_quarantine_invalidates_unconsumed_approval(self):
        sire, dam = self._breeding_pair()
        pairing = self._approved_pairing(sire, dam)
        self.service.transition(
            self.admin, dam["id"], "quarantine_animal", {"reason": "screening"}
        )
        stale = self.service.get(pairing["id"])
        self.assertEqual(stale["status"], "invalidated")
        self.assertEqual(
            stale["data"]["invalidated"]["code"], "animal_status_changed"
        )
        self.assertEqual(stale["data"]["invalidated"]["animal_id"], dam["id"])
        audit = self.service.audit_log(pairing["id"])
        self.assertIn("invalidated", [row["action"] for row in audit])

    def test_pedigree_update_invalidates_approval(self):
        sire, dam = self._breeding_pair()
        pairing = self._approved_pairing(sire, dam)
        # dam becomes sire's offspring: coefficient jumps 0.0 -> 0.25
        self.service.transition(
            self.admin, dam["id"], "update_pedigree", {"sire_id": sire["id"]}
        )
        stale = self.service.get(pairing["id"])
        self.assertEqual(stale["status"], "invalidated")
        self.assertIn(stale["data"]["invalidated"]["code"],
                      ("pedigree_changed", "inbreeding_exceeded"))

    def test_completed_pairing_survives_later_animal_change(self):
        sire, dam = self._breeding_pair()
        pairing = self._approved_pairing(sire, dam)
        completed = self.service.transition(
            self.coordinator,
            pairing["id"],
            "complete",
            {"offspring": [{"name": "O-1", "sex": "unknown"}]},
            expected_version=2,
        )
        self.assertEqual(completed["status"], "completed")
        self.service.transition(
            self.admin, sire["id"], "mark_deceased", {"cause": "old age"}
        )
        self.assertEqual(self.service.get(pairing["id"])["status"], "completed")

    def test_completion_of_invalidated_pairing_fails_with_reason(self):
        sire, dam = self._breeding_pair()
        pairing = self._approved_pairing(sire, dam)
        self.service.transition(
            self.admin, dam["id"], "quarantine_animal", {"reason": "x"}
        )
        with self.assertRaises(ApprovalInvalidatedError) as caught:
            self.service.transition(
                self.coordinator,
                pairing["id"],
                "complete",
                {"offspring_ids": ["o-1"]},
            )
        self.assertEqual(
            caught.exception.details["reason"]["code"], "animal_status_changed"
        )
        self.assertEqual(caught.exception.details["pairing_id"], pairing["id"])

    def test_reapprove_restores_invalidated_pairing(self):
        sire, dam = self._breeding_pair()
        pairing = self._approved_pairing(sire, dam)
        self.service.transition(
            self.admin, dam["id"], "quarantine_animal", {"reason": "x"}
        )
        stale = self.service.get(pairing["id"])
        self.service.transition(
            self.admin, dam["id"], "release_quarantine", {}
        )
        reapproved = self.service.transition(
            self.coordinator, pairing["id"], "reapprove",
            {}, expected_version=stale["version"],
        )
        self.assertEqual(reapproved["status"], "approved")
        self.assertIsNone(reapproved["data"]["invalidated"])

    # -- transport gating + retries ---------------------------------------

    def _linked_transport(self, sire, dam, pairing):
        transfer = self.service.create(
            self.registrar,
            "transfer",
            {
                "animal_id": sire["id"],
                "from_institution": "Zoo-A",
                "to_institution": "Zoo-B",
                "pairing_id": pairing["id"],
            },
        )
        transfer = self.service.transition(
            self.registrar, transfer["id"],
            "authorize", {"permit_id": "P-1"},
        )
        return transfer

    def test_transport_step_blocked_when_approval_invalidated(self):
        sire, dam = self._breeding_pair()
        pairing = self._approved_pairing(sire, dam)
        transfer = self._linked_transport(sire, dam, pairing)
        transfer = self.service.transition(
            self.registrar, transfer["id"], "ship", {"transport_id": "T-1"}
        )
        self.assertEqual(transfer["status"], "in_transit")
        self.assertEqual(
            self.service.get(sire["id"])["data"]["occupied_by"], transfer["id"]
        )

        self.service.transition(
            self.admin, dam["id"], "quarantine_animal", {"reason": "x"}
        )

        # The failed transport record stays in_transit and the occupancy
        # stays held; only the next step is refused with a clear reason.
        with self.assertRaises(ApprovalInvalidatedError) as caught:
            self.service.transition(
                self.registrar, transfer["id"],
                "arrive", {"arrival_date": "2026-09-30"},
            )
        self.assertEqual(
            caught.exception.details["reason"]["code"], "animal_status_changed"
        )
        self.assertEqual(self.service.get(transfer["id"])["status"], "in_transit")
        self.assertEqual(
            self.service.get(sire["id"])["data"]["occupied_by"], transfer["id"]
        )

    def test_transport_retries_from_completed_step_without_double_side_effects(self):
        sire, dam = self._breeding_pair()
        pairing = self._approved_pairing(sire, dam)
        transfer = self._linked_transport(sire, dam, pairing)
        shipped = self.service.transition(
            self.registrar, transfer["id"], "ship", {"transport_id": "T-1"}
        )
        # Retrying an already-completed step does not re-occupy the animal.
        retried = self.service.transition(
            self.registrar, transfer["id"], "ship",
            {"transport_id": "T-1"}, idempotency_key="ship-attempt-2",
        )
        self.assertEqual(retried["status"], "in_transit")
        self.assertEqual(
            self.service.get(sire["id"])["data"]["occupied_by"], transfer["id"]
        )

        # Invalidation, then recovery, then resume from the completed step.
        self.service.transition(
            self.admin, dam["id"], "quarantine_animal", {"reason": "x"}
        )
        self.service.transition(self.admin, dam["id"], "release_quarantine", {})
        stale = self.service.get(pairing["id"])
        self.service.transition(
            self.coordinator, pairing["id"], "reapprove",
            {}, expected_version=stale["version"],
        )
        arrived = self.service.transition(
            self.registrar, transfer["id"],
            "arrive", {"arrival_date": "2026-09-30"},
        )
        self.assertEqual(arrived["status"], "completed")
        self.assertIsNone(self.service.get(sire["id"])["data"]["occupied_by"])
        # Duplicate arrive must not release anything twice / error out.
        again = self.service.transition(
            self.registrar, transfer["id"], "arrive",
            {"arrival_date": "2026-09-30"}, idempotency_key="arrive-attempt-2",
        )
        self.assertEqual(again["status"], "completed")
        self.assertIsNone(self.service.get(sire["id"])["data"]["occupied_by"])
        self.assertEqual(shipped["id"], transfer["id"])

    def test_complete_registers_offspring_once_across_retries(self):
        sire, dam = self._breeding_pair()
        pairing = self._approved_pairing(sire, dam)
        payload = {"offspring": [{"name": "O-1", "sex": "female"},
                                 {"name": "O-2", "sex": "male"}]}
        completed = self.service.transition(
            self.coordinator, pairing["id"], "complete", payload,
            expected_version=2,
        )
        offspring_ids = completed["data"]["offspring_ids"]
        self.assertEqual(len(offspring_ids), 2)
        # Simulated retry of the same completion request.
        retried = self.service.transition(
            self.coordinator, pairing["id"], "complete", payload,
            idempotency_key="complete-attempt-2",
        )
        self.assertEqual(retried["status"], "completed")
        animals = self.service.list("animal")
        for offspring_id in offspring_ids:
            self.assertEqual(
                sum(1 for animal in animals if animal["id"] == offspring_id),
                1,
            )
            offspring = self.service.get(offspring_id)
            self.assertEqual(offspring["kind"], "animal")
            self.assertEqual(offspring["data"]["birth_pairing_id"], pairing["id"])

    def test_transport_requires_approved_pairing(self):
        sire, dam = self._breeding_pair()
        proposed = self.service.create(
            self.coordinator, "pairing", {"proposed_by": "coord-1"}
        )
        with self.assertRaises(Exception):
            self.service.create(
                self.registrar,
                "transfer",
                {"animal_id": sire["id"], "from_institution": "A",
                 "to_institution": "B", "pairing_id": proposed["id"]},
            )

    def test_ship_refuses_when_animal_occupied_by_another_transfer(self):
        sire, dam = self._breeding_pair()
        pairing = self._approved_pairing(sire, dam)
        first = self._linked_transport(sire, dam, pairing)
        self.service.transition(
            self.registrar, first["id"], "ship", {"transport_id": "T-1"}
        )
        second = self.service.create(
            self.registrar,
            "transfer",
            {"animal_id": sire["id"], "from_institution": "A",
             "to_institution": "C"},
        )
        self.service.transition(
            self.registrar, second["id"], "authorize", {"permit_id": "P-2"}
        )
        with self.assertRaises(ConflictError) as caught:
            self.service.transition(
                self.registrar, second["id"], "ship", {"transport_id": "T-2"}
            )
        self.assertEqual(caught.exception.details["occupied_by"], first["id"])
        # The failed ship registers no process step and does not move status.
        self.assertEqual(self.service.get(second["id"])["status"], "authorized")

    def test_step_ledger_records_completed_steps(self):
        sire, dam = self._breeding_pair()
        pairing = self._approved_pairing(sire, dam)
        transfer = self._linked_transport(sire, dam, pairing)
        self.service.transition(
            self.registrar, transfer["id"], "ship", {"transport_id": "T-1"}
        )
        steps = self.service.steps(transfer["id"])
        keys = {step["step_key"]: step["status"] for step in steps}
        self.assertEqual(keys, {"transfer:authorize": "completed",
                                "transfer:ship": "completed"})


if __name__ == "__main__":
    unittest.main()
