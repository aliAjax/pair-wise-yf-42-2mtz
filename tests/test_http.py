import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class HttpCoordinationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo = SQLiteRepository(Path(cls.tmp.name) / "http.db")
        service = DomainService(repo, RuleEngine())
        cls.server = create_server("127.0.0.1", 0, service, RuleEngine(), "static")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def _request(self, method, path, body=None):
        url = "http://127.0.0.1:%s%s" % (self.port, path)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-User-Id", "admin")
        req.add_header("X-Role", "admin")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_page_flow_approve_and_see_invalidation(self):
        status, sire = self._request(
            "POST", "/api/animals", {"name": "M-http", "sex": "male"}
        )
        self.assertEqual(status, 201)
        status, dam = self._request(
            "POST", "/api/animals", {"name": "F-http", "sex": "female"}
        )
        self.assertEqual(status, 201)
        status, pairing = self._request(
            "POST", "/api/pairings", {"proposed_by": "coordinator"}
        )
        self.assertEqual(status, 201)

        # Submit approval with expected_version=1.
        status, approved = self._request(
            "POST",
            "/api/entities/%s/actions" % pairing["id"],
            {
                "action": "approve",
                "data": {
                    "sire_id": sire["id"],
                    "dam_id": dam["id"],
                    "approvals": ["vet-1"],
                },
                "expected_version": 1,
            },
        )
        self.assertEqual(status, 200, approved)
        self.assertEqual(approved["status"], "approved")

        # A second approval with the same stale version must conflict.
        status, conflict = self._request(
            "POST",
            "/api/entities/%s/actions" % pairing["id"],
            {
                "action": "approve",
                "data": {
                    "sire_id": sire["id"],
                    "dam_id": dam["id"],
                    "approvals": ["vet-2"],
                },
                "expected_version": 1,
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["type"], "ConflictError")

        # Quarantine the sire: the approval must become invalidated.
        status, _ = self._request(
            "POST",
            "/api/entities/%s/actions" % sire["id"],
            {"action": "quarantine_animal", "data": {"reason": "health check"}},
        )
        self.assertEqual(status, 200)

        status, view = self._request("GET", "/api/entities/%s" % pairing["id"])
        self.assertEqual(status, 200)
        self.assertEqual(view["status"], "invalidated")
        self.assertIn("quarantined", view["data"]["invalidation_reason"])

    def test_page_flow_transport_retry(self):
        status, sire = self._request(
            "POST", "/api/animals", {"name": "M-tr", "sex": "male"}
        )
        self.assertEqual(status, 201)
        status, transfer = self._request(
            "POST",
            "/api/transfers",
            {
                "animal_id": sire["id"],
                "from_institution": "Zoo-A",
                "to_institution": "Zoo-B",
            },
        )
        self.assertEqual(status, 201)

        status, _ = self._request(
            "POST",
            "/api/entities/%s/actions" % transfer["id"],
            {"action": "authorize", "data": {"permit_id": "P-1"}},
        )
        self.assertEqual(status, 200)

        # Simulated failure: stays at authorized.
        status, failed = self._request(
            "POST",
            "/api/entities/%s/actions" % transfer["id"],
            {
                "action": "ship",
                "data": {"transport_id": "T-1", "simulate_failure": True},
            },
        )
        self.assertEqual(status, 400)
        status, view = self._request("GET", "/api/entities/%s" % transfer["id"])
        self.assertEqual(view["status"], "authorized")

        # Retry ship: advances to in_transit and occupies once.
        status, shipped = self._request(
            "POST",
            "/api/entities/%s/actions" % transfer["id"],
            {"action": "ship", "data": {"transport_id": "T-1"}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(shipped["status"], "in_transit")
        self.assertTrue(shipped["data"]["occupied"])

        # Repeated ship is idempotent.
        status, retry = self._request(
            "POST",
            "/api/entities/%s/actions" % transfer["id"],
            {"action": "ship", "data": {"transport_id": "T-1"}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(retry["status"], "in_transit")

    def test_health_and_index(self):
        status, body = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        # The demo page is served.
        req = urllib.request.Request("http://127.0.0.1:%s/" % self.port)
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("提交配对批准", html)
        self.assertIn("失效原因", html)


if __name__ == "__main__":
    unittest.main()
