from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ApprovalInvalidatedError,
    ConflictError,
    NotFoundError,
)
from .repository import utcnow
from .rules import ANIMAL_BASIS_ACTIONS, RuleEngine, evaluate_approval


STEP_KEYS = {
    ("pairing", "complete"): "pairing:complete",
    ("transfer", "authorize"): "transfer:authorize",
    ("transfer", "ship"): "transfer:ship",
    ("transfer", "arrive"): "transfer:arrive",
}


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _animal(self, animal_id):
        return self.repository.get_entity(animal_id) if animal_id else None

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    # -- coordinated transitions ----------------------------------------

    def transition(self, actor, entity_id, action, data=None,
                   expected_version=None, idempotency_key=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        payload = dict(data or {})

        # Permission is enforced first so a replayed step cannot bypass it.
        allowed_roles = self.rules.ROLE_ACTIONS.get(
            (kind, action), self.rules.ROLE_ACTIONS.get(action, ("admin",))
        )
        self.rules._ensure_role(actor, allowed_roles)

        step_key = STEP_KEYS.get((kind, action))
        existing_step = (
            self.repository.get_step(entity_id, step_key) if step_key else None
        )
        if existing_step:
            # Already executed: replay must never re-register offspring or
            # re-occupy the animal. A caller submitting with a stale base
            # version still sees the conflict, but a genuine retry of the
            # same executed step (same idempotency key, or no base version)
            # gets the existing result back.
            if (
                expected_version is not None
                and int(expected_version) < entity["version"]
                and existing_step.get("idem_key") != idempotency_key
            ):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, entity["version"]),
                    details={
                        "expected_version": int(expected_version),
                        "current_version": entity["version"],
                    },
                )
            return entity

        if kind == "pairing" and action == "complete" and entity["status"] == "invalidated":
            reason = entity["data"].get("invalidated") or {
                "code": "approval_invalidated",
                "message": "pairing approval was invalidated",
            }
            raise ApprovalInvalidatedError(
                "approval for pairing %s is invalid: %s"
                % (entity_id, reason.get("message", "")),
                details={"pairing_id": entity_id, "reason": reason},
            )

        # Surface optimistic-version mismatch before the state-machine check
        # so a latecomer always sees "version conflict" (and re-reads)
        # rather than an unrelated transition error.
        if (
            expected_version is not None
            and int(expected_version) != entity["version"]
        ):
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, entity["version"]),
                details={
                    "expected_version": int(expected_version),
                    "current_version": entity["version"],
                },
            )

        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        now = utcnow()

        creates = []
        occupancy = []
        assertions = []
        invalidate = []
        step = None
        step_result = {}

        if kind == "pairing" and action == "complete":
            # Lazy safety net: even if eager invalidation was bypassed, a
            # stale approval cannot be completed.
            sire = self._animal(entity["data"].get("sire_id"))
            dam = self._animal(entity["data"].get("dam_id"))
            reason = evaluate_approval(entity, sire, dam)
            if reason:
                stale = dict(entity)
                stale["data"] = merged
                self._stamp_invalidation(stale, reason, invalidate, now)
                self.repository.invalidate_approved_pairings(actor, invalidate, now=now)
                raise ApprovalInvalidatedError(
                    "approval for pairing %s is invalid: %s"
                    % (entity_id, reason["message"]),
                    details={"pairing_id": entity_id, "reason": reason},
                )
            creates, step_result = self._build_offspring(entity, merged, actor)

        if kind == "transfer":
            self._gate_transfer(entity, action, actor, assertions, invalidate, now)
            if action == "ship":
                occupancy.append(
                    {"animal_id": entity["data"]["animal_id"],
                     "transfer_id": entity["id"], "release": False}
                )
                step_result = {"occupied_animal": entity["data"]["animal_id"]}
            elif action == "arrive":
                occupancy.append(
                    {"animal_id": entity["data"]["animal_id"],
                     "transfer_id": entity["id"], "release": True}
                )
                step_result = {"released_animal": entity["data"]["animal_id"]}
            elif action == "authorize":
                step_result = {"permit_id": merged.get("permit_id")}

        if step_key:
            step = {
                "key": step_key,
                "status": "completed",
                "result": step_result,
                "idem_key": idempotency_key,
            }

        expected = (
            int(expected_version) if expected_version is not None else entity["version"]
        )
        plan = {
            "parent": {
                "id": entity_id,
                "expected_version": expected,
                "status": next_status,
                "data": merged,
            },
            "expected_from_status": entity["status"],
            "actor": actor,
            "action": action,
            "now": now,
            "create": creates,
            "occupancy": occupancy,
            "assert_entities": assertions,
            "invalidate": invalidate,
            "step": step,
            "audits": [
                {
                    "entity_id": entity_id,
                    "actor_id": actor.user_id,
                    "actor_role": actor.role,
                    "action": action,
                    "from_status": entity["status"],
                    "to_status": next_status,
                    "detail": {"patch": patch},
                }
            ],
        }
        updated = self.repository.submit_transition(plan)

        # After the individual has changed, previously approved pairings that
        # rested on it are invalidated eagerly. Completed pairings are left
        # untouched ("已完成步骤保留").
        if kind == "animal" and action in ANIMAL_BASIS_ACTIONS:
            self._invalidate_stale_pairings(updated, actor, now)

        return updated

    # -- helpers ----------------------------------------------------------

    def _build_offspring(self, pairing, merged, actor):
        specs = merged.pop("offspring", None) or []
        offspring_ids = list(merged.get("offspring_ids") or [])
        creates = []
        if specs:
            if len(specs) != len(offspring_ids):
                raise ConflictError("offspring specification count mismatch")
            for offspring_id, spec in zip(offspring_ids, specs):
                data = {
                    "name": spec.get("name", offspring_id),
                    "sex": spec.get("sex", "unknown"),
                    "sire_id": merged.get("sire_id"),
                    "dam_id": merged.get("dam_id"),
                    "birth_pairing_id": pairing["id"],
                }
                creates.append(
                    {"id": offspring_id, "kind": "animal",
                     "status": "active", "data": data}
                )
        return creates, {"offspring_ids": offspring_ids}

    def _gate_transfer(self, transfer, action, actor, assertions, invalidate, now):
        """Tie the transport flow back to the pairing approval.

        A transfer linked to a not-yet-consumed approval may only proceed
        while that approval is still valid; otherwise the stale approval is
        invalidated (lazily) and the transport step is refused.
        """
        pairing_id = transfer["data"].get("pairing_id")
        if not pairing_id:
            return
        pairing = self.repository.get_entity(pairing_id)
        if not pairing:
            raise NotFoundError("linked pairing not found: " + pairing_id)
        if pairing["status"] == "completed":
            # Approval already consumed by the breeding flow; the transport
            # keeps its independent remaining steps ("从已完成步骤重试").
            return
        if pairing["status"] != "approved":
            from .domain import InvalidTransition

            if pairing["status"] == "invalidated":
                reason = pairing["data"].get("invalidated") or {
                    "code": "approval_invalidated",
                    "message": "pairing approval was invalidated",
                }
                raise ApprovalInvalidatedError(
                    "approval for pairing %s is invalid: %s"
                    % (pairing_id, reason.get("message", "")),
                    details={"pairing_id": pairing_id, "reason": reason},
                )
            raise InvalidTransition(
                "linked pairing %s is %s, approval required"
                % (pairing_id, pairing["status"])
            )
        sire = self._animal(pairing["data"].get("sire_id"))
        dam = self._animal(pairing["data"].get("dam_id"))
        reason = evaluate_approval(pairing, sire, dam)
        if reason:
            # Persist the lazy invalidation (status/version guarded, so a
            # concurrent completion wins) before refusing the step.
            self._stamp_invalidation(pairing, reason, invalidate, now)
            self.repository.invalidate_approved_pairings(actor, invalidate, now=now)
            raise ApprovalInvalidatedError(
                "approval for pairing %s is invalid: %s"
                % (pairing_id, reason["message"]),
                details={"pairing_id": pairing_id, "reason": reason},
            )
        assertions.append(
            {"id": pairing_id, "expected_version": pairing["version"],
             "status_in": ("approved", "completed")}
        )

    @staticmethod
    def _stamp_invalidation(pairing, reason, invalidate, now):
        data = dict(pairing["data"])
        data["invalidated"] = dict(reason)
        data["invalidated_at"] = now
        invalidate.append(
            {"pairing_id": pairing["id"], "version": pairing["version"],
             "data": data, "reason": reason}
        )

    def _invalidate_stale_pairings(self, animal, actor, now):
        proposals = []
        for pairing in self.repository.list_entities(kind="pairing", status="approved"):
            data = pairing["data"]
            if animal["id"] not in (data.get("sire_id"), data.get("dam_id")):
                continue
            sire = self._animal(data.get("sire_id"))
            dam = self._animal(data.get("dam_id"))
            reason = evaluate_approval(pairing, sire, dam)
            if reason:
                self._stamp_invalidation(pairing, reason, proposals, now)
        if proposals:
            self.repository.invalidate_approved_pairings(actor, proposals, now=now)

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    def steps(self, entity_id=None):
        return self.repository.list_steps(entity_id=entity_id)
