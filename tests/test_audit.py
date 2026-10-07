import itertools
import json
import random
import unittest

from app.audit import (
    CYCLE,
    DUPLICATE_BATCH,
    DUPLICATE_CONSUMPTION,
    DUPLICATE_STEP,
    INVALID_SCHEMA,
    MERGE_NOT_CONSERVED,
    RATIO_MISMATCH,
    SPLIT_NOT_CONSERVED,
    UNKNOWN_BATCH,
    AuditError,
    audit_payload,
)


def batch(batch_id, mass_ug, analytes=None):
    return {"id": batch_id, "mass_ug": mass_ug, "analytes": dict(analytes or {})}


def merge(step_id, inputs, output):
    return {"id": step_id, "type": "merge", "inputs": list(inputs), "output": output}


def split(step_id, input_id, outputs):
    return {"id": step_id, "type": "split", "input": input_id, "outputs": list(outputs)}


def valid_payload():
    """A ->\\            /-> P1 ->\\
              M (=A+B)            F (=P1+C)   (terminal)
        B ->/            \\-> P2 (terminal)   C ->/
    """
    return {
        "sources": [
            batch("A", 1000, {"Cu": 30, "Zn": 10}),
            batch("B", 2000, {"Cu": 60, "Zn": 20}),
            batch("C", 500, {"Cu": 10, "Zn": 5}),
        ],
        "steps": [
            merge("m1", ["A", "B"], batch("M", 3000, {"Cu": 90, "Zn": 30})),
            split("s1", "M", [batch("P1", 1200, {"Cu": 36, "Zn": 12}),
                              batch("P2", 1800, {"Cu": 54, "Zn": 18})]),
            merge("m2", ["P1", "C"], batch("F", 1700, {"Cu": 46, "Zn": 17})),
        ],
    }


EXPECTED_CERTIFICATE = {
    "status": "ok",
    "terminal_batches": [
        {
            "id": "F",
            "mass_ug": 1700,
            "analytes": {"Cu": 46, "Zn": 17},
            "concentrations_ng_per_ug": {
                "Cu": {"numerator": 23, "denominator": 850},
                "Zn": {"numerator": 1, "denominator": 100},
            },
        },
        {
            "id": "P2",
            "mass_ug": 1800,
            "analytes": {"Cu": 54, "Zn": 18},
            "concentrations_ng_per_ug": {
                "Cu": {"numerator": 3, "denominator": 100},
                "Zn": {"numerator": 1, "denominator": 100},
            },
        },
    ],
    "source_totals": {"mass_ug": 3500, "analytes": {"Cu": 100, "Zn": 35}},
}


class ValidLineageTests(unittest.TestCase):
    def test_certificate(self):
        self.assertEqual(audit_payload(valid_payload()), EXPECTED_CERTIFICATE)

    def test_step_and_source_order_do_not_matter(self):
        canonical = json.dumps(EXPECTED_CERTIFICATE, sort_keys=True)
        rng = random.Random(20261007)
        orders = list(itertools.permutations(range(3)))
        rng.shuffle(orders)
        for order in orders:
            shuffled = valid_payload()
            shuffled["steps"] = [shuffled["steps"][i] for i in order]
            shuffled["sources"] = list(reversed(shuffled["sources"]))
            self.assertEqual(
                json.dumps(audit_payload(shuffled), sort_keys=True), canonical,
                "step order %s changed the certificate" % (order,))

    def test_unconsumed_source_is_terminal(self):
        payload = valid_payload()
        payload["steps"] = payload["steps"][:2]  # drop m2: P1 and C stay unconsumed
        result = audit_payload(payload)
        self.assertEqual([b["id"] for b in result["terminal_batches"]], ["C", "P1", "P2"])

    def test_zero_analyte_reduces_to_zero_over_one(self):
        payload = {
            "sources": [batch("A", 1000, {"Cu": 0})],
            "steps": [split("s1", "A", [batch("X", 400, {"Cu": 0}),
                                        batch("Y", 600, {"Cu": 0})])],
        }
        result = audit_payload(payload)
        for terminal in result["terminal_batches"]:
            self.assertEqual(terminal["concentrations_ng_per_ug"]["Cu"],
                             {"numerator": 0, "denominator": 1})

    def test_fraction_is_reduced(self):
        payload = {
            "sources": [batch("A", 1000, {"Cu": 4}), batch("B", 500, {"Cu": 2})],
            "steps": [merge("m1", ["A", "B"], batch("M", 1500, {"Cu": 6}))],
        }
        result = audit_payload(payload)
        self.assertEqual(result["terminal_batches"][0]["concentrations_ng_per_ug"]["Cu"],
                         {"numerator": 1, "denominator": 250})

    def test_split_into_sixteen_outputs(self):
        children = [batch("P%d" % i, 100, {"Cu": 3}) for i in range(16)]
        payload = {
            "sources": [batch("A", 1600, {"Cu": 48})],
            "steps": [split("s1", "A", children)],
        }
        result = audit_payload(payload)
        self.assertEqual(len(result["terminal_batches"]), 16)


class RuleViolationTests(unittest.TestCase):
    def assert_audit_error(self, payload, code, step_ids=None, batch_ids=None):
        with self.assertRaises(AuditError) as ctx:
            audit_payload(payload)
        err = ctx.exception
        self.assertEqual(err.code, code)
        if step_ids is not None:
            self.assertEqual(err.step_ids, sorted(step_ids))
        if batch_ids is not None:
            self.assertEqual(err.batch_ids, sorted(batch_ids))
        return err

    # -- merge conservation -------------------------------------------------
    def test_merge_mass_not_conserved(self):
        p = valid_payload()
        p["steps"][0]["output"]["mass_ug"] = 3001
        self.assert_audit_error(p, MERGE_NOT_CONSERVED,
                                step_ids=["m1"], batch_ids=["A", "B", "M"])

    def test_merge_analyte_not_conserved(self):
        p = valid_payload()
        p["steps"][0]["output"]["analytes"]["Cu"] = 91
        self.assert_audit_error(p, MERGE_NOT_CONSERVED, step_ids=["m1"])

    def test_merge_output_missing_analyte(self):
        p = valid_payload()
        p["steps"][0]["output"]["analytes"] = {"Cu": 90}
        self.assert_audit_error(p, MERGE_NOT_CONSERVED, step_ids=["m1"])

    def test_merge_output_extra_analyte(self):
        p = valid_payload()
        p["steps"][0]["output"]["analytes"]["Pb"] = 1
        self.assert_audit_error(p, MERGE_NOT_CONSERVED, step_ids=["m1"])

    # -- split conservation -------------------------------------------------
    def test_split_mass_not_conserved(self):
        p = valid_payload()
        p["steps"][1]["outputs"][1]["mass_ug"] = 1801
        self.assert_audit_error(p, SPLIT_NOT_CONSERVED,
                                step_ids=["s1"], batch_ids=["M", "P1", "P2"])

    def test_split_analyte_total_not_conserved(self):
        p = valid_payload()
        p["steps"][1]["outputs"][1]["analytes"]["Zn"] = 19
        self.assert_audit_error(p, SPLIT_NOT_CONSERVED, step_ids=["s1"])

    def test_split_child_introduces_new_analyte(self):
        p = valid_payload()
        p["steps"][1]["outputs"][1]["analytes"]["Pb"] = 1  # P2 is terminal: no cascade
        self.assert_audit_error(p, SPLIT_NOT_CONSERVED, step_ids=["s1"])

    def test_ratio_drift_with_conserved_totals(self):
        p = valid_payload()
        p["steps"][1]["outputs"][0]["analytes"]["Cu"] = 37  # 37 + 53 == 90
        p["steps"][1]["outputs"][1]["analytes"]["Cu"] = 53
        p["steps"][2]["output"]["analytes"]["Cu"] = 47  # keep downstream merge conserved
        self.assert_audit_error(p, RATIO_MISMATCH,
                                step_ids=["s1"], batch_ids=["M", "P1", "P2"])

    # -- duplicate production ------------------------------------------------
    def test_duplicate_batch_two_steps(self):
        p = valid_payload()
        p["steps"].append(merge("m3", ["P2", "C"], batch("F", 2300, {"Cu": 64, "Zn": 23})))
        self.assert_audit_error(p, DUPLICATE_BATCH,
                                step_ids=["m2", "m3"], batch_ids=["F"])

    def test_duplicate_batch_source_and_step(self):
        p = valid_payload()
        p["steps"][0]["output"]["id"] = "A"
        self.assert_audit_error(p, DUPLICATE_BATCH, batch_ids=["A"], step_ids=["m1"])

    def test_duplicate_source_ids(self):
        p = valid_payload()
        p["sources"].append(batch("A", 5))
        self.assert_audit_error(p, DUPLICATE_BATCH, batch_ids=["A"])

    def test_duplicate_step_ids(self):
        p = valid_payload()
        p["steps"][1]["id"] = "m1"
        self.assert_audit_error(p, DUPLICATE_STEP, step_ids=["m1"])

    # -- duplicate consumption ------------------------------------------------
    def test_duplicate_consumption_across_steps(self):
        p = valid_payload()
        p["steps"].append(merge("m3", ["M", "F"], batch("G", 4700, {"Cu": 136, "Zn": 47})))
        self.assert_audit_error(p, DUPLICATE_CONSUMPTION,
                                batch_ids=["M"], step_ids=["m3", "s1"])

    def test_duplicate_consumption_within_one_step(self):
        p = valid_payload()
        p["steps"][0]["inputs"] = ["A", "A"]
        self.assert_audit_error(p, DUPLICATE_CONSUMPTION,
                                batch_ids=["A"], step_ids=["m1"])

    # -- dangling references ---------------------------------------------------
    def test_unknown_batch(self):
        p = valid_payload()
        p["steps"][0]["inputs"] = ["A", "GHOST"]
        self.assert_audit_error(p, UNKNOWN_BATCH, batch_ids=["GHOST"], step_ids=["m1"])

    # -- cycles ------------------------------------------------------------------
    def test_cycle_between_steps(self):
        p = {
            "sources": [batch("A", 1000, {"Cu": 30})],
            "steps": [
                split("p", "Q", [batch("X", 100, {"Cu": 3}), batch("R", 100, {"Cu": 3})]),
                split("q", "X", [batch("Q", 50, {"Cu": 1}), batch("S", 50, {"Cu": 2})]),
            ],
        }
        self.assert_audit_error(p, CYCLE, step_ids=["p", "q"])

    def test_self_cycle_split(self):
        p = {
            "sources": [batch("A", 1000)],
            "steps": [split("s1", "X", [batch("X", 100), batch("Y", 100)])],
        }
        self.assert_audit_error(p, CYCLE, step_ids=["s1"])

    def test_self_cycle_merge(self):
        p = {
            "sources": [batch("B", 100)],
            "steps": [merge("m1", ["M", "B"], batch("M", 200))],
        }
        self.assert_audit_error(p, CYCLE, step_ids=["m1"])

    # -- error payload shape -------------------------------------------------
    def test_error_to_dict_is_sorted(self):
        err = AuditError(CYCLE, "boom", step_ids=["b", "a"], batch_ids=["z", "y"])
        self.assertEqual(err.to_dict(), {
            "code": CYCLE, "message": "boom",
            "step_ids": ["a", "b"], "batch_ids": ["y", "z"],
        })


class SchemaTests(unittest.TestCase):
    def assert_schema_error(self, mutate=None, payload=None):
        p = valid_payload() if payload is None else payload
        if mutate is not None:
            mutate(p)
        with self.assertRaises(AuditError) as ctx:
            audit_payload(p)
        self.assertEqual(ctx.exception.code, INVALID_SCHEMA)

    def test_payload_not_object(self):
        self.assert_schema_error(payload=[1, 2, 3])

    def test_missing_sources(self):
        self.assert_schema_error(lambda p: p.pop("sources"))

    def test_no_sources(self):
        self.assert_schema_error(lambda p: p.update(sources=[]))

    def test_too_many_sources(self):
        self.assert_schema_error(
            lambda p: p.update(sources=[batch("S%d" % i, 1) for i in range(65)]))

    def test_missing_steps(self):
        self.assert_schema_error(lambda p: p.pop("steps"))

    def test_no_steps(self):
        self.assert_schema_error(lambda p: p.update(steps=[]))

    def test_too_many_steps(self):
        def mutate(p):
            p["steps"] = [merge("s%d" % i, ["A", "B"], batch("O%d" % i, 1))
                          for i in range(257)]
        self.assert_schema_error(mutate)

    def test_split_single_output(self):
        self.assert_schema_error(
            lambda p: p["steps"].__setitem__(
                1, split("s1", "M", [batch("P1", 3000, {"Cu": 90, "Zn": 30})])))

    def test_split_seventeen_outputs(self):
        self.assert_schema_error(
            lambda p: p["steps"].__setitem__(
                1, split("s1", "M", [batch("P%d" % i, 1) for i in range(17)])))

    def test_merge_single_input(self):
        self.assert_schema_error(
            lambda p: p["steps"].__setitem__(0, merge("m1", ["A"], batch("M", 1000))))

    def test_zero_mass(self):
        self.assert_schema_error(lambda p: p["sources"][0].update(mass_ug=0))

    def test_negative_mass(self):
        self.assert_schema_error(lambda p: p["sources"][0].update(mass_ug=-5))

    def test_bool_mass(self):
        self.assert_schema_error(lambda p: p["sources"][0].update(mass_ug=True))

    def test_string_mass(self):
        self.assert_schema_error(lambda p: p["sources"][0].update(mass_ug="1000"))

    def test_float_mass(self):
        self.assert_schema_error(lambda p: p["sources"][0].update(mass_ug=1.5))

    def test_mass_above_max(self):
        self.assert_schema_error(lambda p: p["sources"][0].update(mass_ug=10 ** 18 + 1))

    def test_negative_analyte(self):
        self.assert_schema_error(
            lambda p: p["sources"][0]["analytes"].update(Cu=-1))

    def test_float_analyte(self):
        self.assert_schema_error(
            lambda p: p["sources"][0]["analytes"].update(Cu=0.5))

    def test_bool_analyte(self):
        self.assert_schema_error(
            lambda p: p["sources"][0]["analytes"].update(Cu=False))

    def test_analytes_not_object(self):
        self.assert_schema_error(lambda p: p["sources"][0].update(analytes=["Cu"]))

    def test_missing_batch_id(self):
        self.assert_schema_error(lambda p: p["sources"][0].pop("id"))

    def test_empty_batch_id(self):
        self.assert_schema_error(lambda p: p["sources"][0].update(id=""))

    def test_unknown_step_type(self):
        self.assert_schema_error(lambda p: p["steps"][0].update(type="transmute"))


if __name__ == "__main__":
    unittest.main()
