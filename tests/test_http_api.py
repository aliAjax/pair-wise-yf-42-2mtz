import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _request(method, url, body=None, headers=None, port=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        "http://127.0.0.1:%s%s" % (port, url),
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "http.db"), RuleEngine()
        )
        static_dir = str(Path(__file__).resolve().parent.parent / "static")
        self.server = create_server("127.0.0.1", 0, service, RuleEngine(), static_dir)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        admin = {"X-User-Id": "admin", "X-Role": "admin"}
        _, sire = _request("POST", "/api/animals",
                           {"name": "S", "sex": "male"}, admin, self.port)
        _, dam = _request("POST", "/api/animals",
                          {"name": "D", "sex": "female"}, admin, self.port)
        _, pairing = _request(
            "POST", "/api/pairings", {"proposed_by": "c"},
            {"X-User-Id": "c1", "X-Role": "coordinator"}, self.port,
        )
        self.sire_id, self.dam_id, self.pairing_id = sire["id"], dam["id"], pairing["id"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def _approve(self, user, expected_version):
        return _request(
            "POST",
            "/api/entities/%s/actions" % self.pairing_id,
            {"action": "approve",
             "data": {"sire_id": self.sire_id, "dam_id": self.dam_id,
                      "approvals": ["vet-1"]},
             "expected_version": expected_version},
            {"X-User-Id": user, "X-Role": "coordinator"},
            self.port,
        )

    def test_concurrent_approvals_one_conflict(self):
        results = {}

        def approve(user):
            results[user] = self._approve(user, 1)

        threads = [threading.Thread(target=approve, args=(u,))
                   for u in ("u1", "u2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        statuses = sorted(status for status, _ in results.values())
        self.assertEqual(statuses, [200, 409])
        conflict = next(
            payload for status, payload in results.values() if status == 409
        )
        self.assertEqual(conflict["type"], "ConflictError")
        self.assertEqual(conflict["details"]["current_version"], 2)

    def test_invalidated_approval_returns_reason(self):
        status, _ = self._approve("u1", 1)
        self.assertEqual(status, 200)
        status, _ = _request(
            "POST", "/api/entities/%s/actions" % self.dam_id,
            {"action": "quarantine_animal", "data": {"reason": "illness"}},
            {"X-User-Id": "admin", "X-Role": "admin"},
            self.port,
        )
        self.assertEqual(status, 200)
        status, pairing = _request(
            "GET", "/api/entities/%s" % self.pairing_id, port=self.port
        )
        self.assertEqual(pairing["status"], "invalidated")
        self.assertTrue(pairing["data"]["invalidated"]["message"])
        status, payload = _request(
            "POST", "/api/entities/%s/actions" % self.pairing_id,
            {"action": "complete", "data": {"offspring_ids": ["o-1"]}},
            {"X-User-Id": "c1", "X-Role": "coordinator"},
            self.port,
        )
        self.assertEqual(status, 422)
        self.assertEqual(payload["type"], "ApprovalInvalidatedError")
        self.assertEqual(
            payload["details"]["reason"]["code"], "animal_status_changed"
        )

    def test_steps_endpoint_lists_process_steps(self):
        self._approve("u1", 1)
        _, transfer = _request(
            "POST", "/api/transfers",
            {"animal_id": self.sire_id, "from_institution": "A",
             "to_institution": "B", "pairing_id": self.pairing_id},
            {"X-User-Id": "r1", "X-Role": "registrar"},
            self.port,
        )
        _request("POST", "/api/entities/%s/actions" % transfer["id"],
                 {"action": "authorize", "data": {"permit_id": "P"}},
                 {"X-User-Id": "r1", "X-Role": "registrar"}, self.port)
        status, payload = _request(
            "GET", "/api/entities/%s/steps" % transfer["id"], port=self.port
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["step_key"] for item in payload["items"]],
            ["transfer:authorize"],
        )


if __name__ == "__main__":
    unittest.main()
