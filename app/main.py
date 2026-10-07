"""HTTP entry point for the blend-lineage audit service (stdlib only).

Routes:
    GET  /health              -> liveness probe used by the health check
    POST /api/blends/audit    -> lineage audit
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from app.audit import INVALID_SCHEMA, AuditError, audit_payload

MAX_BODY_BYTES = 8 * 1024 * 1024
AUDIT_PATH = "/api/blends/audit"
HEALTH_PATH = "/health"


def _error_payload(code, message):
    return {"status": "error", "error": {"code": code, "message": message}}


class AuditHandler(BaseHTTPRequestHandler):
    server_version = "BlendAudit/1.0"
    protocol_version = "HTTP/1.1"
    timeout = 30

    def _send_json(self, status, payload, close=False):
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        if close:
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == HEALTH_PATH:
            self._send_json(200, {"status": "ok"})
        elif path == AUDIT_PATH:
            self._send_json(405, _error_payload("METHOD_NOT_ALLOWED", "use POST"))
        else:
            self._send_json(404, _error_payload("NOT_FOUND", "unknown route"))

    def do_POST(self):
        path = urlsplit(self.path).path
        if path != AUDIT_PATH:
            self._send_json(404, _error_payload("NOT_FOUND", "unknown route"), close=True)
            return
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header)
        except (TypeError, ValueError):
            self._send_json(411, _error_payload(
                "LENGTH_REQUIRED", "a Content-Length header is required"), close=True)
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(413, _error_payload(
                "PAYLOAD_TOO_LARGE",
                "request body must be at most %d bytes" % MAX_BODY_BYTES), close=True)
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            self._send_json(400, _error_payload(
                INVALID_SCHEMA, "request body is not valid JSON"))
            return
        try:
            certificate = audit_payload(payload)
        except AuditError as exc:
            status = 400 if exc.code == INVALID_SCHEMA else 422
            self._send_json(status, {"status": "error", "error": exc.to_dict()})
            return
        self._send_json(200, certificate)

    def log_message(self, format, *args):  # keep container logs clean
        pass


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), AuditHandler)
    print("blend-audit listening on 0.0.0.0:%d" % port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
