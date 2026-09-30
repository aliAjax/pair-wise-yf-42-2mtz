from .domain import (
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

INBREEDING_LIMIT = 0.125

# Statuses that keep an approved pairing usable. Any other animal status
# (quarantined / deceased) invalidates the stale approval.
BREEDING_STATUSES = ("active",)


def inbreeding_coefficient(sire, dam):
    if not sire or not dam:
        return 1.0
    sire_id = sire.get("id")
    dam_id = dam.get("id")
    if sire_id is None or dam_id is None:
        return 0.0
    if sire_id == dam_id:
        return 0.5
    if sire.get("sire_id") == dam_id or dam.get("sire_id") == sire_id:
        return 0.25
    return 0.0


def _validate_animal(actor, data, lookup):
    if data.get("sex") not in ("male", "female", "unknown"):
        raise ValidationError("sex must be male, female or unknown")


def _validate_pedigree_update(actor, entity, data, lookup):
    if "sire_id" not in data and "dam_id" not in data:
        raise ValidationError("update_pedigree requires sire_id or dam_id")
    for field in ("sire_id", "dam_id"):
        parent_id = data.get(field)
        if parent_id in (None, ""):
            continue
        if parent_id == entity["id"]:
            raise ValidationError("animal cannot be its own ancestor")
        parent = _find_one(lookup, "animal", "id", parent_id)
        if not parent:
            raise ValidationError("unknown %s: %s" % (field, parent_id))
    return {}


def _breeding_animals(data, lookup):
    sire = _find_one(lookup, "animal", "id", data.get("sire_id"))
    dam = _find_one(lookup, "animal", "id", data.get("dam_id"))
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    return sire, dam


def build_approval_basis(sire, dam, approvals):
    """Snapshot the basis an approval rests on.

    Later animal status / pedigree changes are compared against this
    snapshot so stale approvals can be detected and invalidated.
    """
    sire_data, dam_data = sire["data"], dam["data"]
    return {
        "sire_id": sire["id"],
        "dam_id": dam["id"],
        "sire": _animal_basis(sire),
        "dam": _animal_basis(dam),
        "inbreeding": inbreeding_coefficient(sire_data, dam_data),
        "approvals": list(approvals or []),
    }


def _animal_basis(entity):
    data = entity["data"]
    return {
        "status": entity["status"],
        "sire_id": data.get("sire_id"),
        "dam_id": data.get("dam_id"),
        "sex": data.get("sex"),
    }


def evaluate_approval(pairing, sire, dam):
    """Return an invalidation reason dict, or None when the approval holds.

    Used both eagerly (when an animal changes) and lazily (before the
    completion/transport flow consumes an approval).
    """
    basis = (pairing.get("data") or {}).get("approval_basis")
    if not basis:
        return {"code": "no_approval", "message": "pairing was never approved"}
    if not sire or not dam:
        return {
            "code": "animal_missing",
            "message": "a breeding animal no longer exists",
        }
    current = {"sire": sire, "dam": dam}
    for role, entity in current.items():
        snapshot = basis.get(role) or {}
        if entity["status"] not in BREEDING_STATUSES:
            return {
                "code": "animal_status_changed",
                "message": "%s is now %s and no longer breedable"
                % (role, entity["status"]),
                "animal_id": entity["id"],
                "status": entity["status"],
            }
        for field in ("sire_id", "dam_id", "sex"):
            if snapshot.get(field) != entity["data"].get(field):
                return {
                    "code": "pedigree_changed",
                    "message": "%s pedigree field %s changed after approval"
                    % (role, field),
                    "animal_id": entity["id"],
                    "field": field,
                }
    coefficient = inbreeding_coefficient(sire["data"], dam["data"])
    if coefficient > INBREEDING_LIMIT:
        return {
            "code": "inbreeding_exceeded",
            "message": "inbreeding coefficient %s exceeds %s"
            % (coefficient, INBREEDING_LIMIT),
            "inbreeding": coefficient,
        }
    if coefficient != basis.get("inbreeding"):
        return {
            "code": "kinship_changed",
            "message": "kinship changed after approval: %s -> %s"
            % (basis.get("inbreeding"), coefficient),
            "inbreeding": coefficient,
        }
    return None


def _approve_pairing(actor, entity, data, lookup):
    sire, dam = _breeding_animals(data, lookup)
    if sire["status"] not in BREEDING_STATUSES or dam["status"] not in BREEDING_STATUSES:
        raise ValidationError("pairing animals must be active")
    coefficient = inbreeding_coefficient(sire["data"], dam["data"])
    if coefficient > INBREEDING_LIMIT:
        raise ValidationError("pairing exceeds inbreeding threshold")
    return {
        "sire_id": sire["id"],
        "dam_id": dam["id"],
        "approved_by": actor.user_id,
        "approval_basis": build_approval_basis(sire, dam, data.get("approvals") or []),
    }


def _reapprove_pairing(actor, entity, data, lookup):
    stored = entity["data"]
    sire_id = data.get("sire_id") or stored.get("sire_id")
    dam_id = data.get("dam_id") or stored.get("dam_id")
    if not sire_id or not dam_id:
        raise ValidationError("reapprove requires sire_id and dam_id")
    sire, dam = _breeding_animals(
        {"sire_id": sire_id, "dam_id": dam_id}, lookup
    )
    if sire["status"] not in BREEDING_STATUSES or dam["status"] not in BREEDING_STATUSES:
        raise ValidationError("pairing animals must be active")
    coefficient = inbreeding_coefficient(sire["data"], dam["data"])
    if coefficient > INBREEDING_LIMIT:
        raise ValidationError("pairing exceeds inbreeding threshold")
    approvals = data.get("approvals")
    if approvals is None:
        approvals = (stored.get("approval_basis") or {}).get("approvals") or []
    return {
        "sire_id": sire["id"],
        "dam_id": dam["id"],
        "approved_by": actor.user_id,
        "approval_basis": build_approval_basis(sire, dam, approvals),
        "invalidated": None,
    }


def _complete_pairing(actor, entity, data, lookup):
    offspring = data.get("offspring")
    offspring_ids = data.get("offspring_ids")
    if not offspring and not offspring_ids:
        raise ValidationError(
            "complete requires offspring_ids or offspring specifications"
        )
    patch = {}
    if offspring:
        patch["offspring"] = offspring
        if not offspring_ids:
            patch["offspring_ids"] = [
                "%s-offspring-%d" % (entity["id"], index + 1)
                for index in range(len(offspring))
            ]
    return patch


def _validate_transfer_create(actor, data, lookup):
    animal = _find_one(lookup, "animal", "id", data.get("animal_id"))
    if not animal:
        raise ValidationError("transfer requires an existing animal")
    pairing_id = data.get("pairing_id")
    if pairing_id:
        pairing = _find_one(lookup, "pairing", "id", pairing_id)
        if not pairing:
            raise ValidationError("unknown pairing_id: " + str(pairing_id))
        if pairing["status"] not in ("approved", "completed"):
            raise ValidationError(
                "linked pairing must be approved, not %s" % pairing["status"]
            )


CUSTOM_CREATE = {"animal": _validate_animal, "transfer": _validate_transfer_create}
CUSTOM_TRANSITIONS = {
    ("animal", "update_pedigree"): _validate_pedigree_update,
    ("pairing", "approve"): _approve_pairing,
    ("pairing", "reapprove"): _reapprove_pairing,
    ("pairing", "complete"): _complete_pairing,
}

# Animal actions whose effects can invalidate a stale pairing approval.
ANIMAL_BASIS_ACTIONS = (
    "mark_deceased",
    "quarantine_animal",
    "release_quarantine",
    "update_pedigree",
)


class RuleEngine:
    ALIASES = {"animals": "animal", "pairings": "pairing", "transfers": "transfer"}
    INITIAL_STATUS = {"animal": "active", "pairing": "proposed", "transfer": "planned"}
    TRANSITIONS = {
        "animal": {
            "mark_deceased": (("active", "quarantined"), "deceased"),
            "quarantine_animal": (("active",), "quarantined"),
            "release_quarantine": (("quarantined",), "active"),
            "update_pedigree": (
                ("active", "quarantined", "deceased"),
                None,
            ),
        },
        "pairing": {
            "approve": (("proposed", "rejected"), "approved"),
            "reject": (("proposed",), "rejected"),
            "reapprove": (("invalidated",), "approved"),
            "complete": (("approved",), "completed"),
        },
        "transfer": {
            "authorize": (("planned",), "authorized"),
            "ship": (("authorized",), "in_transit"),
            "arrive": (("in_transit",), "completed"),
        },
    }
    CREATE_REQUIRED = {
        "animal": ("name", "sex"),
        "pairing": ("proposed_by",),
        "transfer": ("animal_id", "from_institution", "to_institution"),
    }
    ACTION_REQUIRED = {
        ("animal", "mark_deceased"): ("cause",),
        ("animal", "quarantine_animal"): ("reason",),
        ("animal", "update_pedigree"): (),
        ("pairing", "approve"): ("sire_id", "dam_id"),
        ("pairing", "reject"): ("reason",),
        ("pairing", "reapprove"): (),
        ("pairing", "complete"): (),
        ("transfer", "authorize"): ("permit_id",),
        ("transfer", "ship"): ("transport_id",),
        ("transfer", "arrive"): ("arrival_date",),
    }
    CREATE_ROLES = {
        "animal": ("admin", "registrar"),
        "pairing": ("admin", "coordinator"),
        "transfer": ("admin", "registrar"),
    }
    ROLE_ACTIONS = {
        "mark_deceased": ("admin", "veterinarian"),
        "quarantine_animal": ("admin", "veterinarian"),
        "release_quarantine": ("admin", "veterinarian"),
        "update_pedigree": ("admin", "registrar", "veterinarian"),
        "approve": ("admin", "coordinator"),
        "reject": ("admin", "coordinator"),
        "reapprove": ("admin", "coordinator"),
        "complete": ("admin", "coordinator"),
        "authorize": ("admin", "registrar"),
        "ship": ("admin", "registrar"),
        "arrive": ("admin", "registrar"),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        # ``None`` next status means the action updates data but keeps the
        # entity in its current status (pedigree correction).
        resolved_status = next_status if next_status is not None else entity["status"]
        return resolved_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None
