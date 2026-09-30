from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, DomainError, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

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

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])

        # Optimistic concurrency: act on stale data -> version conflict before
        # any status validation. The authoritative race check lives in
        # update_entity; this makes the conflict surface deterministically.
        if expected_version is not None and int(expected_version) != entity["version"]:
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, entity["version"])
            )

        # Retry from the completed step: if the entity is already at the
        # target status, re-running the action is a no-op success so side
        # effects (offspring registration, occupation) are not duplicated.
        if self.rules.is_idempotent(entity, action):
            self.rules._ensure_role(actor, self.rules._allowed_roles(kind, action))
            return entity

        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )

        # Failure hook for transport records: raise before committing so the
        # step does not advance; a retry resumes from the completed steps.
        if kind == "transfer" and data and data.get("simulate_failure"):
            raise DomainError(
                "transport record failure (simulated) before %s" % action
            )

        expected = int(expected_version) if expected_version is not None else entity["version"]
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )

        # Connect animal updates to pairing approval validity: an approved
        # pairing that has not been completed is invalidated when the animal's
        # status or pedigree changes. Completed pairings keep their steps.
        if kind == "animal":
            self._invalidate_approved_pairings(entity, action, data, actor)

        return updated

    def _invalidate_approved_pairings(self, animal, action, data, actor):
        animal_id = animal["id"]
        reason = self._invalidation_reason(animal, action, data)
        invalidated = self.repository.invalidate_approved_pairings(animal_id, reason)
        for pairing_id in invalidated:
            self.audit.record(
                pairing_id,
                actor,
                "invalidate",
                "approved",
                "invalidated",
                {"reason": reason, "trigger": action, "animal_id": animal_id},
            )

    def _invalidation_reason(self, animal, action, data):
        data = data or {}
        if action == "mark_deceased":
            reason = "animal marked deceased"
            if data.get("cause"):
                reason += " (cause: %s)" % data["cause"]
            return reason
        if action == "quarantine_animal":
            reason = "animal quarantined"
            if data.get("reason"):
                reason += " (reason: %s)" % data["reason"]
            return reason
        if action == "release_quarantine":
            return "animal released from quarantine"
        if action == "update_pedigree":
            changes = []
            if "sire_id" in data and data["sire_id"] != animal["data"].get("sire_id"):
                changes.append(
                    "sire_id: %s -> %s"
                    % (animal["data"].get("sire_id"), data["sire_id"])
                )
            if "dam_id" in data and data["dam_id"] != animal["data"].get("dam_id"):
                changes.append(
                    "dam_id: %s -> %s"
                    % (animal["data"].get("dam_id"), data["dam_id"])
                )
            if changes:
                return "animal pedigree updated (" + ", ".join(changes) + ")"
            return "animal pedigree reconfirmed"
        return "animal status changed via %s" % action

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
