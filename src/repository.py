import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS process_steps (
                    entity_id TEXT NOT NULL,
                    step_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT NOT NULL,
                    idem_key TEXT,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(entity_id, step_key)
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def get_step(self, entity_id, step_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM process_steps WHERE entity_id = ? AND step_key = ?",
                (entity_id, step_key),
            ).fetchone()
        if not row:
            return None
        return {
            "entity_id": row["entity_id"],
            "step_key": row["step_key"],
            "status": row["status"],
            "result": json.loads(row["result"]),
            "idem_key": row["idem_key"],
            "actor_id": row["actor_id"],
            "created_at": row["created_at"],
        }

    def list_steps(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM process_steps WHERE entity_id = ? ORDER BY rowid",
                    (entity_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM process_steps ORDER BY rowid"
                ).fetchall()
        return [
            {
                "entity_id": row["entity_id"],
                "step_key": row["step_key"],
                "status": row["status"],
                "result": json.loads(row["result"]),
                "idem_key": row["idem_key"],
                "actor_id": row["actor_id"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _insert_audit(connection, entry, now):
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, "
            "from_status, to_status, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry["entity_id"],
                entry["actor_id"],
                entry["actor_role"],
                entry["action"],
                entry.get("from_status"),
                entry["to_status"],
                json.dumps(entry.get("detail") or {}, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )

    def submit_transition(self, plan):
        """Apply one coordinated transition atomically.

        Plan keys:
          parent: {id, expected_version (None allowed), status, data}
          actor, action, now
          create: [entity dicts incl. id/kind/status/data]
          occupancy: [{animal_id, transfer_id, release: bool}]
          invalidate: [{pairing_id, version, data, reason}]
          step: {key, status, result, idem_key}
          audits: [audit entries]
        The whole plan commits together so a failure after a side effect
        never leaves half-registered offspring or dangling occupancy.
        """
        parent = plan["parent"]
        entity_id = parent["id"]
        now = plan.get("now") or utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            expected = parent.get("expected_version")
            if expected is not None and current_version != int(expected):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected, current_version),
                    details={
                        "expected_version": int(expected),
                        "current_version": current_version,
                    },
                )
            if row["status"] != plan.get("expected_from_status"):
                raise InvalidTransition(
                    "cannot %s from status %s"
                    % (plan.get("action"), row["status"])
                )

            for assertion in plan.get("assert_entities") or ():
                other = connection.execute(
                    "SELECT id, status, version FROM entities WHERE id = ?",
                    (assertion["id"],),
                ).fetchone()
                if not other:
                    raise ConflictError(
                        "referenced entity vanished: " + assertion["id"]
                    )
                if assertion.get("expected_version") is not None and int(
                    other["version"]
                ) != int(assertion["expected_version"]):
                    raise ConflictError(
                        "version conflict on %s: expected %s, found %s"
                        % (
                            assertion["id"],
                            assertion["expected_version"],
                            other["version"],
                        ),
                        details={
                            "entity_id": assertion["id"],
                            "expected_version": int(assertion["expected_version"]),
                            "current_version": int(other["version"]),
                        },
                    )
                wanted_status = assertion.get("status_in")
                if wanted_status and other["status"] not in wanted_status:
                    raise ConflictError(
                        "%s is %s, expected one of %s"
                        % (assertion["id"], other["status"], ",".join(wanted_status))
                    )

            for item in plan.get("create") or ():
                exists = connection.execute(
                    "SELECT 1 FROM entities WHERE id = ?", (item["id"],)
                ).fetchone()
                if exists:
                    # Deterministic retry: entity already created by a prior
                    # attempt of the same coordinated step.
                    continue
                connection.execute(
                    "INSERT INTO entities(id, kind, status, version, data, "
                    "created_by, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                    (
                        item["id"],
                        item["kind"],
                        item["status"],
                        json.dumps(item["data"], ensure_ascii=False, sort_keys=True),
                        plan["actor"].user_id,
                        now,
                        now,
                    ),
                )

            for occ in plan.get("occupancy") or ():
                animal = connection.execute(
                    "SELECT data FROM entities WHERE id = ? AND kind = 'animal'",
                    (occ["animal_id"],),
                ).fetchone()
                if not animal:
                    raise NotFoundError("animal not found: " + occ["animal_id"])
                data = json.loads(animal["data"])
                holder = data.get("occupied_by")
                if occ.get("release"):
                    if holder == occ["transfer_id"]:
                        data["occupied_by"] = None
                    # Releasing without a matching holder is a no-op retry.
                else:
                    if holder and holder != occ["transfer_id"]:
                        raise ConflictError(
                            "animal %s is already occupied by %s"
                            % (occ["animal_id"], holder),
                            details={"animal_id": occ["animal_id"], "occupied_by": holder},
                        )
                    data["occupied_by"] = occ["transfer_id"]
                connection.execute(
                    "UPDATE entities SET data = ?, version = version + 1, updated_at = ? "
                    "WHERE id = ?",
                    (json.dumps(data, ensure_ascii=False, sort_keys=True), now, occ["animal_id"]),
                )

            for invalid in plan.get("invalidate") or ():
                result = connection.execute(
                    "UPDATE entities SET status = 'invalidated', data = ?, "
                    "version = version + 1, updated_at = ? "
                    "WHERE id = ? AND status = 'approved' AND version = ?",
                    (
                        json.dumps(invalid["data"], ensure_ascii=False, sort_keys=True),
                        now,
                        invalid["pairing_id"],
                        invalid["version"],
                    ),
                )
                if result.rowcount:
                    self._insert_audit(
                        connection,
                        {
                            "entity_id": invalid["pairing_id"],
                            "actor_id": plan["actor"].user_id,
                            "actor_role": plan["actor"].role,
                            "action": "invalidated",
                            "from_status": "approved",
                            "to_status": "invalidated",
                            "detail": {"reason": invalid["reason"]},
                        },
                        now,
                    )

            step = plan.get("step")
            if step:
                connection.execute(
                    "INSERT INTO process_steps(entity_id, step_key, status, result, "
                    "idem_key, actor_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        entity_id,
                        step["key"],
                        step.get("status", "completed"),
                        json.dumps(step.get("result") or {}, ensure_ascii=False, sort_keys=True),
                        step.get("idem_key"),
                        plan["actor"].user_id,
                        now,
                    ),
                )

            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, "
                "updated_at = ? WHERE id = ? AND version = ?",
                (
                    parent["status"],
                    json.dumps(parent["data"], ensure_ascii=False, sort_keys=True),
                    now,
                    entity_id,
                    current_version,
                ),
            )

            for audit in plan.get("audits") or ():
                self._insert_audit(connection, audit, now)

            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def invalidate_approved_pairings(self, actor, proposals, now=None):
        """Best-effort eager invalidation, each pairing in its own tx.

        Guarded by status='approved' AND version so a pairing that completes
        concurrently is never overwritten. Returns the ids actually flipped.
        """
        flipped = []
        now = now or utcnow()
        for proposal in proposals:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                result = connection.execute(
                    "UPDATE entities SET status = 'invalidated', data = ?, "
                    "version = version + 1, updated_at = ? "
                    "WHERE id = ? AND status = 'approved' AND version = ?",
                    (
                        json.dumps(proposal["data"], ensure_ascii=False, sort_keys=True),
                        now,
                        proposal["pairing_id"],
                        proposal["version"],
                    ),
                )
                if result.rowcount:
                    self._insert_audit(
                        connection,
                        {
                            "entity_id": proposal["pairing_id"],
                            "actor_id": actor.user_id,
                            "actor_role": actor.role,
                            "action": "invalidated",
                            "from_status": "approved",
                            "to_status": "invalidated",
                            "detail": {"reason": proposal["reason"]},
                        },
                        now,
                    )
                    flipped.append(proposal["pairing_id"])
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        return flipped

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
