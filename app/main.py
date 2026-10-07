"""HTTP API for the blend/aliquot genealogy audit service.

Endpoints:
    GET  /health              liveness probe used by Docker/Compose healthchecks
    POST /api/blends/audit    audit one genealogy payload, return a certificate

Standard library only, so the image builds with no network access.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .audit import INVALID_SCHEMA, AuditRejection, audit

MAX_BODY_BYTES = 4 * 1024 * 1024
AUDIT_PATH = "/api/blends/audit"
HEALTH_PATHS = ("/health", "/healthz")


def _rejection(reason_code, detail, step_ids=(), batch_ids=()):
    return {
        "status": "rejected",
        "reason_code": reason_code,
        "step_ids": sorted(step_ids),
        "batch_ids": sorted(batch_ids),
        "detail": detail,
    }


class AuditHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BlendAudit/1.0"

    # -- helpers --------------------------------------------------------------
    def _send_json(self, status_code, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _path(self):
        return urlsplit(self.path).path

    # -- routing ----------------------------------------------------------------
    def do_GET(self):
        if self._path() in HEALTH_PATHS:
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, _rejection("NOT_FOUND", f"unknown endpoint: GET {self._path()}"))

    def do_POST(self):
        if self._path() != AUDIT_PATH:
            self._send_json(404, _rejection("NOT_FOUND", f"unknown endpoint: POST {self._path()}"))
            return
        content_length = self.headers.get("Content-Length")
        try:
            length = int(content_length) if content_length is not None else 0
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, _rejection(INVALID_SCHEMA, "request body is missing"))
            return
        if length > MAX_BODY_BYTES:
            self._send_json(
                413,
                _rejection("PAYLOAD_TOO_LARGE", f"request body exceeds {MAX_BODY_BYTES} bytes"),
            )
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send_json(400, _rejection(INVALID_SCHEMA, "request body is not valid JSON"))
            return
        try:
            certificate = audit(payload)
        except AuditRejection as rejection:
            status_code = 400 if rejection.reason_code == INVALID_SCHEMA else 422
            self._send_json(
                status_code,
                _rejection(rejection.reason_code, rejection.detail, rejection.step_ids, rejection.batch_ids),
            )
            return
        self._send_json(200, certificate)

    def do_PUT(self):
        self._send_json(405, _rejection("METHOD_NOT_ALLOWED", "use POST " + AUDIT_PATH))

    def do_DELETE(self):
        self._send_json(405, _rejection("METHOD_NOT_ALLOWED", "use POST " + AUDIT_PATH))

    def log_message(self, fmt, *args):  # noqa: A003 - keep stdlib signature
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), AuditHandler)
    print(f"blend-audit API listening on {host}:{port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
