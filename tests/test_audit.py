"""Unit tests for the core genealogy audit logic."""

import copy
import random
import unittest

from app import audit as audit_mod
from app.audit import (
    BATCH_RECONSUMED,
    CONSERVATION_VIOLATION,
    CYCLE_DETECTED,
    DUPLICATE_OUTPUT_ID,
    DUPLICATE_SOURCE_ID,
    DUPLICATE_STEP_ID,
    INVALID_SCHEMA,
    RATIO_MISMATCH,
    UNKNOWN_BATCH,
    AuditRejection,
    audit,
)


def valid_payload():
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


def expect_rejection(test_case, payload, reason_code):
    with test_case.assertRaises(AuditRejection) as ctx:
        audit(payload)
    test_case.assertEqual(ctx.exception.reason_code, reason_code)
    return ctx.exception


class TestSuccessPath(unittest.TestCase):
    def test_valid_genealogy_terminals_and_totals(self):
        result = audit(valid_payload())
        self.assertEqual(result["status"], "ok")
        self.assertEqual([b["id"] for b in result["terminal_batches"]], ["M2", "Q1", "Q2"])
        self.assertEqual(
            result["source_totals"],
            {"mass_ug": 3500, "analytes": {"A": 450, "B": 120, "C": 25}},
        )

    def test_concentration_fractions_are_reduced(self):
        result = audit(valid_payload())
        terminals = {b["id"]: b for b in result["terminal_batches"]}
        self.assertEqual(terminals["Q1"]["concentrations"]["A"], {"numerator": 3, "denominator": 25})
        self.assertEqual(terminals["Q1"]["concentrations"]["B"], {"numerator": 1, "denominator": 25})
        self.assertEqual(terminals["M2"]["concentrations"]["A"], {"numerator": 27, "denominator": 200})
        self.assertEqual(terminals["M2"]["concentrations"]["C"], {"numerator": 1, "denominator": 80})

    def test_zero_analyte_yields_zero_over_one(self):
        payload = {
            "sources": [{"id": "S1", "mass_ug": 100, "analytes": {"A": 0}}],
            "steps": [
                {
                    "id": "sp",
                    "type": "split",
                    "input": "S1",
                    "outputs": [
                        {"id": "a", "mass_ug": 40, "analytes": {"A": 0}},
                        {"id": "b", "mass_ug": 60, "analytes": {"A": 0}},
                    ],
                }
            ],
        }
        result = audit(payload)
        for batch in result["terminal_batches"]:
            self.assertEqual(batch["concentrations"]["A"], {"numerator": 0, "denominator": 1})

    def test_unused_source_is_terminal(self):
        payload = valid_payload()
        payload["sources"].append({"id": "S4", "mass_ug": 7, "analytes": {"Z": 1}})
        result = audit(payload)
        self.assertIn("S4", [b["id"] for b in result["terminal_batches"]])
        self.assertEqual(result["source_totals"]["mass_ug"], 3507)
        self.assertEqual(result["source_totals"]["analytes"]["Z"], 1)

    def test_merge_analyte_union_with_extra_zero_ok(self):
        payload = {
            "sources": [
                {"id": "S1", "mass_ug": 10, "analytes": {"A": 5}},
                {"id": "S2", "mass_ug": 20, "analytes": {"B": 7}},
            ],
            "steps": [
                {
                    "id": "m",
                    "type": "merge",
                    "inputs": ["S1", "S2"],
                    "output": {"id": "M", "mass_ug": 30, "analytes": {"A": 5, "B": 7, "C": 0}},
                }
            ],
        }
        result = audit(payload)
        self.assertEqual(result["terminal_batches"][0]["analytes"], {"A": 5, "B": 7, "C": 0})

    def test_split_allows_zero_analyte_children(self):
        payload = {
            "sources": [{"id": "S1", "mass_ug": 100, "analytes": {"A": 0, "B": 10}}],
            "steps": [
                {
                    "id": "sp",
                    "type": "split",
                    "input": "S1",
                    "outputs": [
                        {"id": "a", "mass_ug": 50, "analytes": {"A": 0, "B": 5}},
                        {"id": "b", "mass_ug": 50, "analytes": {"A": 0, "B": 5}},
                    ],
                }
            ],
        }
        self.assertEqual(audit(payload)["status"], "ok")

    def test_max_boundaries_accepted(self):
        # 64 sources, exactly 256 steps, 16-way splits: merge all sources,
        # then 127 split/merge pairs, then a final 2-way split.
        sources = [{"id": f"src-{i:02d}", "mass_ug": 16, "analytes": {"A": 32}} for i in range(64)]
        steps = [
            {
                "id": "st-000",
                "type": "merge",
                "inputs": [s["id"] for s in sources],
                "output": {"id": "cur", "mass_ug": 1024, "analytes": {"A": 2048}},
            }
        ]
        current = "cur"
        for i in range(127):
            children = [{"id": f"{current}-c{j}", "mass_ug": 64, "analytes": {"A": 128}} for j in range(16)]
            steps.append({"id": f"st-{2 * i + 1:03d}", "type": "split", "input": current, "outputs": children})
            steps.append(
                {
                    "id": f"st-{2 * i + 2:03d}",
                    "type": "merge",
                    "inputs": [c["id"] for c in children],
                    "output": {"id": f"cur-{i}", "mass_ug": 1024, "analytes": {"A": 2048}},
                }
            )
            current = f"cur-{i}"
        steps.append(
            {
                "id": "st-255",
                "type": "split",
                "input": current,
                "outputs": [
                    {"id": "final-a", "mass_ug": 512, "analytes": {"A": 1024}},
                    {"id": "final-b", "mass_ug": 512, "analytes": {"A": 1024}},
                ],
            }
        )
        self.assertEqual(len(steps), 256)
        result = audit({"sources": sources, "steps": steps})
        self.assertEqual([b["id"] for b in result["terminal_batches"]], ["final-a", "final-b"])
        self.assertEqual(result["source_totals"], {"mass_ug": 1024, "analytes": {"A": 2048}})


class TestOrderIndependence(unittest.TestCase):
    def test_step_and_source_permutations_give_identical_certificate(self):
        baseline = audit(valid_payload())
        rng = random.Random(12345)
        for _ in range(25):
            payload = valid_payload()
            rng.shuffle(payload["steps"])
            rng.shuffle(payload["sources"])
            self.assertEqual(audit(payload), baseline)

    def test_fully_reversed_steps(self):
        payload = valid_payload()
        payload["steps"].reverse()
        payload["sources"].reverse()
        self.assertEqual(audit(payload), audit(valid_payload()))

    def test_error_is_stable_under_permutation(self):
        rng = random.Random(999)
        expected = None
        for _ in range(10):
            payload = valid_payload()
            payload["steps"].append(
                {
                    "id": "st-05",
                    "type": "merge",
                    "inputs": ["M1", "S3"],
                    "output": {"id": "X1", "mass_ug": 3500, "analytes": {"A": 450, "B": 120, "C": 25}},
                }
            )
            rng.shuffle(payload["steps"])
            rng.shuffle(payload["sources"])
            err = expect_rejection(self, payload, BATCH_RECONSUMED)
            signature = (err.reason_code, err.step_ids, err.batch_ids)
            if expected is None:
                expected = signature
            self.assertEqual(signature, expected)
        self.assertEqual(expected, (BATCH_RECONSUMED, ["st-02", "st-03", "st-05"], ["M1", "S3"]))


class TestSchemaValidation(unittest.TestCase):
    def test_payload_must_be_object(self):
        expect_rejection(self, [1, 2, 3], INVALID_SCHEMA)

    def test_missing_sections(self):
        expect_rejection(self, {}, INVALID_SCHEMA)
        expect_rejection(self, {"sources": valid_payload()["sources"]}, INVALID_SCHEMA)

    def test_unknown_top_level_field(self):
        payload = valid_payload()
        payload["extra"] = 1
        expect_rejection(self, payload, INVALID_SCHEMA)

    def test_source_count_bounds(self):
        payload = valid_payload()
        payload["sources"] = []
        expect_rejection(self, payload, INVALID_SCHEMA)
        payload = valid_payload()
        payload["sources"] = [
            {"id": f"s{i}", "mass_ug": 1, "analytes": {}} for i in range(audit_mod.MAX_SOURCES + 1)
        ]
        expect_rejection(self, payload, INVALID_SCHEMA)

    def test_step_count_bounds(self):
        payload = valid_payload()
        payload["steps"] = []
        expect_rejection(self, payload, INVALID_SCHEMA)
        payload = valid_payload()
        payload["steps"] = payload["steps"] * 65  # 260 steps (with duplicate ids)
        expect_rejection(self, payload, INVALID_SCHEMA)

    def test_mass_must_be_positive_integer(self):
        for bad in (0, -5, 1.5, "10", True, None):
            payload = valid_payload()
            payload["sources"][0]["mass_ug"] = bad
            expect_rejection(self, payload, INVALID_SCHEMA)

    def test_analyte_must_be_non_negative_integer(self):
        for bad in (-1, 2.5, "3", False, None):
            payload = valid_payload()
            payload["sources"][0]["analytes"]["A"] = bad
            expect_rejection(self, payload, INVALID_SCHEMA)

    def test_ids_must_be_non_empty_strings(self):
        for bad in ("", 5, None, ["x"]):
            payload = valid_payload()
            payload["sources"][0]["id"] = bad
            expect_rejection(self, payload, INVALID_SCHEMA)

    def test_merge_needs_at_least_two_inputs(self):
        payload = valid_payload()
        payload["steps"][0]["inputs"] = ["S1"]
        payload["steps"][0]["output"] = {"id": "M1", "mass_ug": 1000, "analytes": {"A": 120, "B": 40}}
        expect_rejection(self, payload, INVALID_SCHEMA)

    def test_split_output_count_bounds(self):
        payload = valid_payload()
        payload["steps"][1]["outputs"] = [{"id": "P1", "mass_ug": 1500, "analytes": {"A": 180, "B": 60}}]
        expect_rejection(self, payload, INVALID_SCHEMA)
        payload = valid_payload()
        payload["steps"][1]["outputs"] = [
            {"id": f"P{i}", "mass_ug": 93, "analytes": {"A": 11, "B": 3}} for i in range(17)
        ]
        expect_rejection(self, payload, INVALID_SCHEMA)

    def test_split_allows_exactly_sixteen_outputs(self):
        payload = {
            "sources": [{"id": "S1", "mass_ug": 16, "analytes": {"A": 32}}],
            "steps": [
                {
                    "id": "sp",
                    "type": "split",
                    "input": "S1",
                    "outputs": [{"id": f"c{i}", "mass_ug": 1, "analytes": {"A": 2}} for i in range(16)],
                }
            ],
        }
        self.assertEqual(len(audit(payload)["terminal_batches"]), 16)

    def test_unknown_step_type(self):
        payload = valid_payload()
        payload["steps"][0]["type"] = "transmute"
        expect_rejection(self, payload, INVALID_SCHEMA)

    def test_step_unknown_field(self):
        payload = valid_payload()
        payload["steps"][0]["note"] = "oops"
        expect_rejection(self, payload, INVALID_SCHEMA)


class TestDuplicates(unittest.TestCase):
    def test_duplicate_step_id(self):
        payload = valid_payload()
        payload["steps"].append(copy.deepcopy(payload["steps"][0]))
        err = expect_rejection(self, payload, DUPLICATE_STEP_ID)
        self.assertEqual(err.step_ids, ["st-01"])

    def test_duplicate_source_id(self):
        payload = valid_payload()
        payload["sources"].append({"id": "S1", "mass_ug": 5, "analytes": {}})
        err = expect_rejection(self, payload, DUPLICATE_SOURCE_ID)
        self.assertEqual(err.batch_ids, ["S1"])

    def test_duplicate_output_across_steps(self):
        payload = valid_payload()
        payload["steps"][1]["outputs"][0]["id"] = "M2"
        err = expect_rejection(self, payload, DUPLICATE_OUTPUT_ID)
        self.assertEqual(err.batch_ids, ["M2"])
        self.assertEqual(sorted(err.step_ids), ["st-02", "st-03"])

    def test_output_collides_with_source(self):
        payload = valid_payload()
        payload["steps"][0]["output"]["id"] = "S3"
        err = expect_rejection(self, payload, DUPLICATE_OUTPUT_ID)
        self.assertEqual(err.batch_ids, ["S3"])

    def test_duplicate_output_within_one_split(self):
        payload = valid_payload()
        payload["steps"][1]["outputs"][1]["id"] = "P1"
        expect_rejection(self, payload, DUPLICATE_OUTPUT_ID)


class TestGraphRules(unittest.TestCase):
    def test_unknown_batch_reference(self):
        payload = valid_payload()
        payload["steps"][0]["inputs"] = ["S1", "GHOST"]
        err = expect_rejection(self, payload, UNKNOWN_BATCH)
        self.assertEqual(err.batch_ids, ["GHOST"])
        self.assertEqual(err.step_ids, ["st-01"])

    def test_reconsumed_across_steps(self):
        payload = valid_payload()
        payload["steps"].append(
            {
                "id": "st-05",
                "type": "merge",
                "inputs": ["Q1", "S3"],
                "output": {"id": "X1", "mass_ug": 1000, "analytes": {"A": 150, "B": 20, "C": 25}},
            }
        )
        err = expect_rejection(self, payload, BATCH_RECONSUMED)
        self.assertEqual(err.batch_ids, ["S3"])  # S3 is consumed by both st-03 and st-05
        self.assertEqual(err.step_ids, ["st-03", "st-05"])

    def test_reconsumed_within_one_step(self):
        payload = valid_payload()
        payload["steps"][0]["inputs"] = ["S1", "S1"]
        err = expect_rejection(self, payload, BATCH_RECONSUMED)
        self.assertEqual(err.batch_ids, ["S1"])

    def test_cycle_two_step_ring(self):
        payload = {
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
        err = expect_rejection(self, payload, CYCLE_DETECTED)
        self.assertEqual(err.step_ids, ["cy-1", "cy-2"])

    def test_cycle_self_loop(self):
        payload = {
            "sources": [{"id": "S1", "mass_ug": 100, "analytes": {"A": 10}}],
            "steps": [
                {
                    "id": "loop",
                    "type": "split",
                    "input": "X",
                    "outputs": [
                        {"id": "X", "mass_ug": 40, "analytes": {"A": 4}},
                        {"id": "Y", "mass_ug": 60, "analytes": {"A": 6}},
                    ],
                }
            ],
        }
        expect_rejection(self, payload, CYCLE_DETECTED)

    def test_cycle_via_merge(self):
        payload = {
            "sources": [
                {"id": "S1", "mass_ug": 10, "analytes": {"A": 1}},
                {"id": "S2", "mass_ug": 10, "analytes": {"A": 1}},
            ],
            "steps": [
                {
                    "id": "m1",
                    "type": "merge",
                    "inputs": ["S1", "B"],
                    "output": {"id": "A", "mass_ug": 20, "analytes": {"A": 2}},
                },
                {
                    "id": "m2",
                    "type": "merge",
                    "inputs": ["S2", "A"],
                    "output": {"id": "B", "mass_ug": 20, "analytes": {"A": 2}},
                },
            ],
        }
        expect_rejection(self, payload, CYCLE_DETECTED)


class TestConservation(unittest.TestCase):
    def test_merge_mass_not_conserved(self):
        payload = valid_payload()
        payload["steps"][0]["output"]["mass_ug"] = 3001
        err = expect_rejection(self, payload, CONSERVATION_VIOLATION)
        self.assertEqual(err.step_ids, ["st-01"])

    def test_merge_analyte_not_conserved(self):
        payload = valid_payload()
        payload["steps"][0]["output"]["analytes"]["A"] = 359
        expect_rejection(self, payload, CONSERVATION_VIOLATION)

    def test_merge_output_missing_analyte_key(self):
        payload = valid_payload()
        del payload["steps"][0]["output"]["analytes"]["B"]
        expect_rejection(self, payload, CONSERVATION_VIOLATION)

    def test_merge_output_extra_analyte(self):
        payload = valid_payload()
        payload["steps"][0]["output"]["analytes"]["C"] = 1
        expect_rejection(self, payload, CONSERVATION_VIOLATION)

    def test_split_mass_not_conserved(self):
        payload = valid_payload()
        payload["steps"][3]["outputs"][0]["mass_ug"] = 501
        expect_rejection(self, payload, CONSERVATION_VIOLATION)

    def test_split_analyte_not_conserved(self):
        payload = valid_payload()
        payload["steps"][3]["outputs"][1]["analytes"]["B"] = 41
        expect_rejection(self, payload, CONSERVATION_VIOLATION)

    def test_split_child_extra_analyte_rejected(self):
        payload = valid_payload()
        payload["steps"][3]["outputs"][0]["analytes"]["C"] = 1
        expect_rejection(self, payload, CONSERVATION_VIOLATION)


class TestRatioPreservation(unittest.TestCase):
    def test_ratio_drift_compensated_by_sibling(self):
        payload = valid_payload()
        payload["steps"][3]["outputs"][0]["analytes"]["A"] = 50
        payload["steps"][3]["outputs"][1]["analytes"]["A"] = 130  # total A still 180
        err = expect_rejection(self, payload, RATIO_MISMATCH)
        self.assertEqual(err.step_ids, ["st-04"])
        self.assertEqual(err.batch_ids, ["P2", "Q1"])

    def test_ratio_drift_missing_key_in_child(self):
        payload = valid_payload()
        del payload["steps"][3]["outputs"][0]["analytes"]["B"]
        payload["steps"][3]["outputs"][1]["analytes"]["B"] = 60  # total B still 60
        expect_rejection(self, payload, RATIO_MISMATCH)

    def test_ratio_drift_concentrated_child(self):
        # Same analyte-to-analyte ratio, doubled concentration: still drift.
        payload = {
            "sources": [{"id": "S1", "mass_ug": 200, "analytes": {"A": 30, "B": 60}}],
            "steps": [
                {
                    "id": "sp",
                    "type": "split",
                    "input": "S1",
                    "outputs": [
                        {"id": "a", "mass_ug": 100, "analytes": {"A": 20, "B": 40}},
                        {"id": "b", "mass_ug": 100, "analytes": {"A": 10, "B": 20}},
                    ],
                }
            ],
        }
        expect_rejection(self, payload, RATIO_MISMATCH)

    def test_uneven_but_proportional_split_ok(self):
        payload = {
            "sources": [{"id": "S1", "mass_ug": 900, "analytes": {"A": 90, "B": 45}}],
            "steps": [
                {
                    "id": "sp",
                    "type": "split",
                    "input": "S1",
                    "outputs": [
                        {"id": "a", "mass_ug": 100, "analytes": {"A": 10, "B": 5}},
                        {"id": "b", "mass_ug": 300, "analytes": {"A": 30, "B": 15}},
                        {"id": "c", "mass_ug": 500, "analytes": {"A": 50, "B": 25}},
                    ],
                }
            ],
        }
        result = audit(payload)
        self.assertEqual([b["id"] for b in result["terminal_batches"]], ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
