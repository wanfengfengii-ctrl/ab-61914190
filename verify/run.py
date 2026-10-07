"""One-shot verification service.

Runs, inside the clean container environment:
  1. a byte-compile check of all sources (build sanity),
  2. the unit test suite,
  3. an API health wait,
  4. HTTP smoke tests covering conservation, step-order invariance and
     illegal lineages (duplicate consumption, ratio drift, non-conservation,
     cycles, dangling references, duplicate batches, schema violations).

Exits 0 only if every check passes; exits 1 otherwise.
"""

from __future__ import annotations

import compileall
import json
import os
import random
import sys
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.test_audit import (  # noqa: E402
    EXPECTED_CERTIFICATE,
    batch,
    merge,
    split,
    valid_payload,
)

API_URL = os.environ.get("API_URL", "http://127.0.0.1:8080").rstrip("/")

failures = []


def check(name, condition, detail=""):
    line = "[%s] %s" % ("PASS" if condition else "FAIL", name)
    if detail and not condition:
        line += " -- " + detail
    print(line, flush=True)
    if not condition:
        failures.append(name)


# ---------------------------------------------------------------------------
# 1. build / 2. unit tests
# ---------------------------------------------------------------------------

def run_build_check():
    print("== build check (byte-compile) ==", flush=True)
    ok = compileall.compile_dir(os.path.join(ROOT, "app"), quiet=1)
    ok = compileall.compile_dir(os.path.join(ROOT, "verify"), quiet=1) and ok
    ok = compileall.compile_dir(os.path.join(ROOT, "tests"), quiet=1) and ok
    check("sources byte-compile", ok)


def run_unit_tests():
    print("== unit tests ==", flush=True)
    suite = unittest.TestLoader().discover(
        start_dir=os.path.join(ROOT, "tests"), top_level_dir=ROOT)
    ok = unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful()
    check("unit tests", ok)


# ---------------------------------------------------------------------------
# 3. health wait
# ---------------------------------------------------------------------------

def wait_for_api(timeout_seconds=None):
    print("== waiting for api health ==", flush=True)
    if timeout_seconds is None:
        timeout_seconds = float(os.environ.get("API_WAIT_TIMEOUT", "60"))
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(API_URL + "/health", timeout=2) as resp:
                if resp.status == 200:
                    check("api health", True)
                    return
        except Exception:
            time.sleep(0.5)
    check("api health", False, "no healthy response from %s within %ds"
          % (API_URL, timeout_seconds))


# ---------------------------------------------------------------------------
# 4. smoke tests
# ---------------------------------------------------------------------------

def post_audit(payload):
    body = json.dumps(payload).encode("utf-8") if isinstance(payload, (dict, list)) else payload
    request = urllib.request.Request(
        API_URL + "/api/blends/audit", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        return 0, str(exc).encode("utf-8", "replace")


def duplicate_consumption_payload():
    return {
        "sources": [batch("A", 1000, {"Cu": 30}), batch("B", 2000, {"Cu": 60})],
        "steps": [
            merge("m1", ["A", "B"], batch("M", 3000, {"Cu": 90})),
            split("s1", "M", [batch("P1", 1200, {"Cu": 36}), batch("P2", 1800, {"Cu": 54})]),
            merge("m2", ["P1", "P2"], batch("X", 3000, {"Cu": 90})),
            merge("m3", ["X", "M"], batch("Y", 6000, {"Cu": 180})),  # M consumed twice
        ],
    }


def ratio_drift_payload():
    p = valid_payload()
    p["steps"][1]["outputs"][0]["analytes"]["Cu"] = 37  # totals still sum to 90
    p["steps"][1]["outputs"][1]["analytes"]["Cu"] = 53
    p["steps"][2]["output"]["analytes"]["Cu"] = 47  # keep downstream merge conserved
    return p


def merge_not_conserved_payload():
    p = valid_payload()
    p["steps"][0]["output"]["mass_ug"] = 3001
    return p


def split_not_conserved_payload():
    p = valid_payload()
    p["steps"][1]["outputs"][1]["mass_ug"] = 1801
    return p


def cycle_payload():
    return {
        "sources": [batch("A", 1000, {"Cu": 30})],
        "steps": [
            split("p", "Q", [batch("X", 100, {"Cu": 3}), batch("R", 100, {"Cu": 3})]),
            split("q", "X", [batch("Q", 50, {"Cu": 1}), batch("S", 50, {"Cu": 2})]),
        ],
    }


def dangling_payload():
    p = valid_payload()
    p["steps"][0]["inputs"] = ["A", "GHOST"]
    return p


def duplicate_batch_payload():
    p = valid_payload()
    p["steps"].append(merge("m3", ["P2", "C"], batch("F", 2300, {"Cu": 64, "Zn": 23})))
    return p


def schema_violation_payload():
    p = valid_payload()
    p["sources"] = []
    return p


def smoke():
    print("== smoke tests ==", flush=True)

    try:
        with urllib.request.urlopen(API_URL + "/health", timeout=5) as resp:
            body = json.loads(resp.read())
            check("health endpoint", resp.status == 200 and body.get("status") == "ok")
    except Exception as exc:  # noqa: BLE001 - report and continue
        check("health endpoint", False, str(exc))

    # conservation: a valid lineage is accepted and yields the expected certificate
    status, body = post_audit(valid_payload())
    check("conservation: valid lineage accepted", status == 200,
          "status=%s body=%s" % (status, body[:200]))
    canonical = None
    if status == 200:
        try:
            canonical = body
            check("conservation: certificate matches expectation",
                  json.loads(body) == EXPECTED_CERTIFICATE, body.decode()[:400])
        except ValueError:
            check("conservation: certificate matches expectation", False, "invalid JSON")

    # shuffled: any permutation of steps/sources yields the identical certificate
    if canonical is not None:
        rng = random.Random(20261007)
        shuffled_ok = True
        detail = ""
        for _ in range(8):
            p = valid_payload()
            rng.shuffle(p["steps"])
            rng.shuffle(p["sources"])
            status2, body2 = post_audit(p)
            if status2 != 200 or body2 != canonical:
                shuffled_ok = False
                detail = "status=%s body=%s" % (status2, body2[:200])
                break
        check("shuffled: step/source order yields identical certificate",
              shuffled_ok, detail)

    # illegal lineages: rejected wholesale with stable reason codes, no partial result
    illegal_cases = [
        ("duplicate consumption", duplicate_consumption_payload(), 422, "DUPLICATE_CONSUMPTION", "M"),
        ("ratio drift", ratio_drift_payload(), 422, "RATIO_MISMATCH", "P1"),
        ("merge not conserved", merge_not_conserved_payload(), 422, "MERGE_NOT_CONSERVED", "M"),
        ("split not conserved", split_not_conserved_payload(), 422, "SPLIT_NOT_CONSERVED", "P2"),
        ("cycle", cycle_payload(), 422, "CYCLE", None),
        ("dangling reference", dangling_payload(), 422, "UNKNOWN_BATCH", "GHOST"),
        ("duplicate batch", duplicate_batch_payload(), 422, "DUPLICATE_BATCH", "F"),
        ("schema violation", schema_violation_payload(), 400, "INVALID_SCHEMA", None),
    ]
    for name, payload, want_status, want_code, want_batch in illegal_cases:
        status, body = post_audit(payload)
        ok = status == want_status
        detail = "status=%s body=%s" % (status, body[:300])
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
            ok = False
        if parsed is not None:
            error = parsed.get("error") or {}
            ok = (ok
                  and parsed.get("status") == "error"
                  and error.get("code") == want_code
                  and "terminal_batches" not in parsed
                  and "source_totals" not in parsed)
            if want_batch is not None:
                ok = ok and want_batch in (error.get("batch_ids") or [])
        check("illegal lineage rejected: %s" % name, ok, detail)

    status, _ = post_audit(b'{"sources":')
    check("malformed JSON rejected", status == 400, "status=%s" % status)

    # no state corruption: the valid lineage still passes after all the rejections
    status, body = post_audit(valid_payload())
    ok = status == 200
    if ok:
        try:
            ok = json.loads(body) == EXPECTED_CERTIFICATE
        except ValueError:
            ok = False
    check("api unaffected by rejected lineages", ok, "status=%s" % status)


def main():
    run_build_check()
    run_unit_tests()
    wait_for_api()
    smoke()
    if failures:
        print("\nVERIFY FAILED: %d check(s) failed: %s"
              % (len(failures), ", ".join(failures)), flush=True)
        return 1
    print("\nVERIFY OK: all checks passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
