"""End-to-end smoke tests run by the one-shot ``verify`` compose service.

Covers the three mandated smoke areas against a live API container:

* conservation  -- a legal merge/split genealogy yields the exact certificate,
                   and quantity drift is rejected;
* out-of-order  -- shuffling steps/sources yields the identical certificate;
* illegal genealogy -- duplicate consumption, ratio drift, cycles and dangling
                   references are rejected with stable reason codes and never
                   produce partial results.

Exits 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import copy
import json
import os
import random
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("API_BASE_URL", "http://api:8080").rstrip("/")

_failures = []
_checks = 0


def check(name, condition, info=""):
    global _checks
    _checks += 1
    if condition:
        print(f"PASS  {name}")
    else:
        _failures.append(name)
        print(f"FAIL  {name}  {info}")


def post_audit(payload):
    request = urllib.request.Request(
        BASE_URL + "/api/blends/audit",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def get_health():
    try:
        with urllib.request.urlopen(BASE_URL + "/health", timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.URLError as error:
        return None, str(error)


def valid_payload():
    """A legal genealogy: two merges and two splits over three sources."""
    return {
        "sources": [
            {"id": "S1", "mass_ug": 1000, "analytes": {"A": 120, "B": 40}},
            {"id": "S2", "mass_ug": 2000, "analytes": {"A": 240, "B": 80}},
            {"id": "S3", "mass_ug": 500, "analytes": {"A": 90, "C": 25}},
        ],
        "steps": [
            {
                "id": "st-01",
                "type": "merge",
                "inputs": ["S1", "S2"],
                "output": {"id": "M1", "mass_ug": 3000, "analytes": {"A": 360, "B": 120}},
            },
            {
                "id": "st-02",
                "type": "split",
                "input": "M1",
                "outputs": [
                    {"id": "P1", "mass_ug": 1500, "analytes": {"A": 180, "B": 60}},
                    {"id": "P2", "mass_ug": 1500, "analytes": {"A": 180, "B": 60}},
                ],
            },
            {
                "id": "st-03",
                "type": "merge",
                "inputs": ["P1", "S3"],
                "output": {"id": "M2", "mass_ug": 2000, "analytes": {"A": 270, "B": 60, "C": 25}},
            },
            {
                "id": "st-04",
                "type": "split",
                "input": "P2",
                "outputs": [
                    {"id": "Q1", "mass_ug": 500, "analytes": {"A": 60, "B": 20}},
                    {"id": "Q2", "mass_ug": 1000, "analytes": {"A": 120, "B": 40}},
                ],
            },
        ],
    }


EXPECTED_CERTIFICATE = {
    "status": "ok",
    "terminal_batches": [
        {
            "id": "M2",
            "mass_ug": 2000,
            "analytes": {"A": 270, "B": 60, "C": 25},
            "concentrations": {
                "A": {"numerator": 27, "denominator": 200},
                "B": {"numerator": 3, "denominator": 100},
                "C": {"numerator": 1, "denominator": 80},
            },
        },
        {
            "id": "Q1",
            "mass_ug": 500,
            "analytes": {"A": 60, "B": 20},
            "concentrations": {
                "A": {"numerator": 3, "denominator": 25},
                "B": {"numerator": 1, "denominator": 25},
            },
        },
        {
            "id": "Q2",
            "mass_ug": 1000,
            "analytes": {"A": 120, "B": 40},
            "concentrations": {
                "A": {"numerator": 3, "denominator": 25},
                "B": {"numerator": 1, "denominator": 25},
            },
        },
    ],
    "source_totals": {"mass_ug": 3500, "analytes": {"A": 450, "B": 120, "C": 25}},
}


def expect_rejection(name, payload, reason_code, step_ids=None, batch_ids=None):
    status, body = post_audit(payload)
    check(f"{name}: http 422", status == 422, f"got {status}: {body}")
    check(
        f"{name}: reason {reason_code}",
        isinstance(body, dict) and body.get("reason_code") == reason_code,
        f"got {body}",
    )
    check(
        f"{name}: no partial results",
        isinstance(body, dict) and body.get("status") == "rejected" and "terminal_batches" not in body,
        f"got {body}",
    )
    if step_ids is not None:
        check(f"{name}: step ids", isinstance(body, dict) and body.get("step_ids") == step_ids, f"got {body}")
    if batch_ids is not None:
        check(f"{name}: batch ids", isinstance(body, dict) and body.get("batch_ids") == batch_ids, f"got {body}")


def main():
    print(f"smoke-testing blend-audit API at {BASE_URL}")

    status, body = get_health()
    check("health endpoint", status == 200 and body.get("status") == "ok", f"got {status}: {body}")

    # -- conservation: legal genealogy yields the exact certificate ----------
    status, body = post_audit(valid_payload())
    check("conservation: http 200", status == 200, f"got {status}: {body}")
    check("conservation: exact certificate", body == EXPECTED_CERTIFICATE, f"got {json.dumps(body)}")

    # -- out-of-order: any permutation yields the identical certificate ------
    rng = random.Random(20261007)
    for trial in range(5):
        shuffled = valid_payload()
        rng.shuffle(shuffled["steps"])
        rng.shuffle(shuffled["sources"])
        status, body = post_audit(shuffled)
        check(f"out-of-order[{trial}]: identical certificate", status == 200 and body == EXPECTED_CERTIFICATE,
              f"got {status}: {body}")

    # -- quantity drift --------------------------------------------------------
    bad_merge = valid_payload()
    bad_merge["steps"][0]["output"]["analytes"]["A"] = 361  # 360 expected
    expect_rejection("merge drift", bad_merge, "CONSERVATION_VIOLATION", step_ids=["st-01"], batch_ids=["M1"])

    bad_split = valid_payload()
    bad_split["steps"][3]["outputs"][0]["mass_ug"] = 400  # children sum 1400 != 1500
    expect_rejection("split drift", bad_split, "CONSERVATION_VIOLATION", step_ids=["st-04"], batch_ids=["P2"])

    # -- ratio drift (totals still conserved, composition is not) --------------
    ratio_drift = valid_payload()
    ratio_drift["steps"][3]["outputs"][0]["analytes"]["A"] = 50
    ratio_drift["steps"][3]["outputs"][1]["analytes"]["A"] = 130  # total A still 180
    expect_rejection("ratio drift", ratio_drift, "RATIO_MISMATCH", step_ids=["st-04"], batch_ids=["P2", "Q1"])

    # -- duplicate consumption ---------------------------------------------------
    reconsumed = valid_payload()
    reconsumed["steps"].append(
        {
            "id": "st-05",
            "type": "merge",
            "inputs": ["M1", "S3"],
            "output": {"id": "X1", "mass_ug": 3500, "analytes": {"A": 450, "B": 120, "C": 25}},
        }
    )
    expect_rejection(
        "reconsumption", reconsumed, "BATCH_RECONSUMED",
        step_ids=["st-02", "st-03", "st-05"], batch_ids=["M1", "S3"],
    )

    # -- dangling reference -------------------------------------------------------
    dangling = valid_payload()
    dangling["steps"][0]["inputs"] = ["S1", "GHOST"]
    expect_rejection("dangling reference", dangling, "UNKNOWN_BATCH", step_ids=["st-01"], batch_ids=["GHOST"])

    # -- cycle ---------------------------------------------------------------------
    cyclic = {
        "sources": [{"id": "S1", "mass_ug": 100, "analytes": {"A": 10}}],
        "steps": [
            {
                "id": "cy-1",
                "type": "split",
                "input": "B2",
                "outputs": [
                    {"id": "B1", "mass_ug": 12, "analytes": {"A": 1}},
                    {"id": "B1x", "mass_ug": 13, "analytes": {"A": 1}},
                ],
            },
            {
                "id": "cy-2",
                "type": "split",
                "input": "B1",
                "outputs": [
                    {"id": "B2", "mass_ug": 25, "analytes": {"A": 2}},
                    {"id": "B2x", "mass_ug": 25, "analytes": {"A": 2}},
                ],
            },
        ],
    }
    expect_rejection("cycle", cyclic, "CYCLE_DETECTED", step_ids=["cy-1", "cy-2"])

    # -- duplicate output id ---------------------------------------------------------
    duplicated = valid_payload()
    duplicated["steps"][1]["outputs"][0]["id"] = "M2"  # collides with st-03 output
    expect_rejection("duplicate output", duplicated, "DUPLICATE_OUTPUT_ID", batch_ids=["M2"])

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    if _failures:
        print("failed checks: " + ", ".join(_failures))
        return 1
    print("all smoke checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
