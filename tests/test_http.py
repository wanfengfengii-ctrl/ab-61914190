"""HTTP-level tests for the audit API (in-process server on an ephemeral port)."""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app.main import AuditHandler


def _request(method, url, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


class TestHttpApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), AuditHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_health(self):
        status, body = _request("GET", self.base + "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_unknown_get_404(self):
        status, body = _request("GET", self.base + "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["reason_code"], "NOT_FOUND")

    def test_post_wrong_path_404(self):
        status, _ = _request("POST", self.base + "/api/blends", body={})
        self.assertEqual(status, 404)

    def test_malformed_json_400(self):
        status, body = _request("POST", self.base + "/api/blends/audit", raw=b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["reason_code"], "INVALID_SCHEMA")

    def test_schema_error_400(self):
        status, body = _request("POST", self.base + "/api/blends/audit", body={"sources": [], "steps": []})
        self.assertEqual(status, 400)
        self.assertEqual(body["reason_code"], "INVALID_SCHEMA")
        self.assertEqual(body["status"], "rejected")

    def test_domain_error_422_with_stable_envelope(self):
        payload = {
            "sources": [{"id": "S1", "mass_ug": 10, "analytes": {"A": 1}}],
            "steps": [
                {
                    "id": "m",
                    "type": "merge",
                    "inputs": ["S1", "GHOST"],
                    "output": {"id": "M", "mass_ug": 10, "analytes": {"A": 1}},
                }
            ],
        }
        status, body = _request("POST", self.base + "/api/blends/audit", body=payload)
        self.assertEqual(status, 422)
        self.assertEqual(body["reason_code"], "UNKNOWN_BATCH")
        self.assertEqual(body["batch_ids"], ["GHOST"])
        self.assertEqual(body["step_ids"], ["m"])
        self.assertNotIn("terminal_batches", body)

    def test_success_200(self):
        payload = {
            "sources": [{"id": "S1", "mass_ug": 8, "analytes": {"A": 4}}],
            "steps": [
                {
                    "id": "sp",
                    "type": "split",
                    "input": "S1",
                    "outputs": [
                        {"id": "a", "mass_ug": 4, "analytes": {"A": 2}},
                        {"id": "b", "mass_ug": 4, "analytes": {"A": 2}},
                    ],
                }
            ],
        }
        status, body = _request("POST", self.base + "/api/blends/audit", body=payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual([b["id"] for b in body["terminal_batches"]], ["a", "b"])
        self.assertEqual(body["source_totals"], {"mass_ug": 8, "analytes": {"A": 4}})


if __name__ == "__main__":
    unittest.main()
