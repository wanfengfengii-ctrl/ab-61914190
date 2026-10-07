"""Core lineage-audit logic for the standard-materials blend service.

Every batch (source, merge product or split child) is declared with a
positive integer mass in micrograms and non-negative integer analyte
amounts in nanograms.  The auditor verifies the declared lineage as a
whole:

* every batch id is produced exactly once (as a source or by one step),
* every batch id is consumed by at most one downstream step,
* the lineage graph is acyclic and references only known batch ids,
* merges conserve mass and every analyte item by item,
* splits conserve mass and every analyte total, and every split child
  keeps exactly the parent's analyte concentrations (ratios).

On success a deterministic certificate is returned: the unconsumed
terminal batches with reduced concentration fractions plus the overall
source totals.  On failure an :class:`AuditError` carrying a stable
reason code is raised and no partial certificate is produced.
"""

from __future__ import annotations

import heapq
from math import gcd

MAX_SOURCES = 64
MAX_STEPS = 256
MIN_MERGE_INPUTS = 2
MIN_SPLIT_OUTPUTS = 2
MAX_SPLIT_OUTPUTS = 16
MAX_BATCHES = MAX_SOURCES + MAX_STEPS * MAX_SPLIT_OUTPUTS
MAX_ID_LENGTH = 128
MAX_ANALYTE_NAME_LENGTH = 128
MAX_VALUE = 10 ** 18  # upper bound for any single declared mass/amount

# Stable rejection reason codes.
INVALID_SCHEMA = "INVALID_SCHEMA"
DUPLICATE_STEP = "DUPLICATE_STEP"
DUPLICATE_BATCH = "DUPLICATE_BATCH"
UNKNOWN_BATCH = "UNKNOWN_BATCH"
DUPLICATE_CONSUMPTION = "DUPLICATE_CONSUMPTION"
CYCLE = "CYCLE"
MERGE_NOT_CONSERVED = "MERGE_NOT_CONSERVED"
SPLIT_NOT_CONSERVED = "SPLIT_NOT_CONSERVED"
RATIO_MISMATCH = "RATIO_MISMATCH"


class AuditError(Exception):
    """Rejection of a whole lineage, carrying a stable reason code."""

    def __init__(self, code, message, step_ids=(), batch_ids=()):
        super().__init__(message)
        self.code = code
        self.message = message
        self.step_ids = sorted(set(step_ids))
        self.batch_ids = sorted(set(batch_ids))

    def to_dict(self):
        error = {"code": self.code, "message": self.message}
        if self.step_ids:
            error["step_ids"] = self.step_ids
        if self.batch_ids:
            error["batch_ids"] = self.batch_ids
        return error


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def _fail(message):
    raise AuditError(INVALID_SCHEMA, message)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _check_id(value, field):
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_ID_LENGTH:
        _fail("%s must be a string of 1..%d characters" % (field, MAX_ID_LENGTH))
    return value


def _check_amount(value, field, minimum):
    if not _is_int(value) or not minimum <= value <= MAX_VALUE:
        _fail("%s must be an integer between %d and %d" % (field, minimum, MAX_VALUE))
    return value


def _parse_batch(node, field):
    if not isinstance(node, dict):
        _fail("%s must be an object" % field)
    batch_id = _check_id(node.get("id"), "%s.id" % field)
    mass = _check_amount(node.get("mass_ug"), "%s.mass_ug" % field, 1)
    analytes_node = node.get("analytes", {})
    if not isinstance(analytes_node, dict):
        _fail("%s.analytes must be an object mapping analyte names to "
              "non-negative integer nanogram amounts" % field)
    analytes = {}
    for name, amount in analytes_node.items():
        if not isinstance(name, str) or not 1 <= len(name) <= MAX_ANALYTE_NAME_LENGTH:
            _fail("%s.analytes keys must be strings of 1..%d characters"
                  % (field, MAX_ANALYTE_NAME_LENGTH))
        analytes[name] = _check_amount(amount, "%s.analytes[%r]" % (field, name), 0)
    return {"id": batch_id, "mass_ug": mass, "analytes": analytes}


def _parse_step(node, index):
    field = "steps[%d]" % index
    if not isinstance(node, dict):
        _fail("%s must be an object" % field)
    step_id = _check_id(node.get("id"), "%s.id" % field)
    step_type = node.get("type")
    if step_type == "merge":
        inputs_node = node.get("inputs")
        if not isinstance(inputs_node, list) or not MIN_MERGE_INPUTS <= len(inputs_node) <= MAX_BATCHES:
            _fail("%s.inputs must be a list of %d..%d batch ids"
                  % (field, MIN_MERGE_INPUTS, MAX_BATCHES))
        inputs = [_check_id(item, "%s.inputs[]" % field) for item in inputs_node]
        output = _parse_batch(node.get("output"), "%s.output" % field)
        return {"id": step_id, "type": "merge", "inputs": inputs, "outputs": [output]}
    if step_type == "split":
        input_id = _check_id(node.get("input"), "%s.input" % field)
        outputs_node = node.get("outputs")
        if not isinstance(outputs_node, list) or not MIN_SPLIT_OUTPUTS <= len(outputs_node) <= MAX_SPLIT_OUTPUTS:
            _fail("%s.outputs must be a list of %d..%d batches"
                  % (field, MIN_SPLIT_OUTPUTS, MAX_SPLIT_OUTPUTS))
        outputs = [_parse_batch(item, "%s.outputs[]" % field) for item in outputs_node]
        return {"id": step_id, "type": "split", "inputs": [input_id], "outputs": outputs}
    _fail("%s.type must be 'merge' or 'split'" % field)


def _parse_payload(payload):
    if not isinstance(payload, dict):
        _fail("request body must be a JSON object")
    sources_node = payload.get("sources")
    steps_node = payload.get("steps")
    if not isinstance(sources_node, list) or not 1 <= len(sources_node) <= MAX_SOURCES:
        _fail("sources must be a list of 1..%d batches" % MAX_SOURCES)
    if not isinstance(steps_node, list) or not 1 <= len(steps_node) <= MAX_STEPS:
        _fail("steps must be a list of 1..%d steps" % MAX_STEPS)
    sources = [_parse_batch(node, "sources[%d]" % i) for i, node in enumerate(sources_node)]
    steps = [_parse_step(node, i) for i, node in enumerate(steps_node)]
    return sources, steps


# ---------------------------------------------------------------------------
# Lineage graph checks
# ---------------------------------------------------------------------------

def _check_duplicate_steps(steps):
    counts = {}
    for step in steps:
        counts[step["id"]] = counts.get(step["id"], 0) + 1
    duplicates = sorted(sid for sid, n in counts.items() if n > 1)
    if duplicates:
        raise AuditError(DUPLICATE_STEP, "step ids must be unique", step_ids=duplicates)


def _check_duplicate_batches(sources, steps):
    """Return {batch id: [producer label, ...]} or raise DUPLICATE_BATCH."""
    producers = {}
    for source in sources:
        producers.setdefault(source["id"], []).append("source")
    for step in steps:
        for output in step["outputs"]:
            producers.setdefault(output["id"], []).append(step["id"])
    duplicates = {bid: labels for bid, labels in producers.items() if len(labels) > 1}
    if duplicates:
        raise AuditError(
            DUPLICATE_BATCH,
            "every batch id must be produced exactly once (as a source or by a single step)",
            batch_ids=sorted(duplicates),
            step_ids=sorted({label for labels in duplicates.values()
                             for label in labels if label != "source"}),
        )
    return producers


def _check_references(steps, producers):
    """Return {batch id: [consuming step id, ...]} or raise on dangling
    references / repeated consumption."""
    consumers = {}
    unknown = {}
    for step in steps:
        for batch_id in step["inputs"]:
            if batch_id in producers:
                consumers.setdefault(batch_id, []).append(step["id"])
            else:
                unknown.setdefault(batch_id, []).append(step["id"])
    if unknown:
        raise AuditError(
            UNKNOWN_BATCH,
            "steps reference batch ids that are never produced",
            batch_ids=sorted(unknown),
            step_ids=sorted({sid for sids in unknown.values() for sid in sids}),
        )
    reused = {bid: sids for bid, sids in consumers.items() if len(sids) > 1}
    if reused:
        raise AuditError(
            DUPLICATE_CONSUMPTION,
            "every batch may be consumed by at most one downstream step",
            batch_ids=sorted(reused),
            step_ids=sorted({sid for sids in reused.values() for sid in sids}),
        )
    return consumers


def _check_cycles(steps):
    producer_step = {}
    for step in steps:
        for output in step["outputs"]:
            producer_step[output["id"]] = step["id"]
    successors = {step["id"]: set() for step in steps}
    indegree = {step["id"]: 0 for step in steps}
    for step in steps:
        for batch_id in step["inputs"]:
            upstream = producer_step.get(batch_id)
            if upstream is not None and step["id"] not in successors[upstream]:
                successors[upstream].add(step["id"])
                indegree[step["id"]] += 1
    ready = [sid for sid, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    processed = 0
    while ready:
        current = heapq.heappop(ready)
        processed += 1
        for nxt in successors[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(ready, nxt)
    if processed < len(steps):
        remaining = sorted(sid for sid, degree in indegree.items() if degree > 0)
        raise AuditError(
            CYCLE,
            "the lineage graph contains a cycle; these steps cannot be ordered",
            step_ids=remaining,
        )


# ---------------------------------------------------------------------------
# Conservation / ratio checks (each step is checked against declared values,
# so the verdicts are independent of the order steps are listed in)
# ---------------------------------------------------------------------------

def _analyte_names(*batches):
    names = set()
    for batch in batches:
        names.update(batch["analytes"])
    return names


def _merge_conserved(step, compositions):
    inputs = [compositions[bid] for bid in step["inputs"]]
    output = step["outputs"][0]
    if output["mass_ug"] != sum(batch["mass_ug"] for batch in inputs):
        return False
    return all(
        output["analytes"].get(name, 0)
        == sum(batch["analytes"].get(name, 0) for batch in inputs)
        for name in _analyte_names(output, *inputs)
    )


def _split_conserved(parent, children):
    if sum(child["mass_ug"] for child in children) != parent["mass_ug"]:
        return False
    return all(
        sum(child["analytes"].get(name, 0) for child in children)
        == parent["analytes"].get(name, 0)
        for name in _analyte_names(parent, *children)
    )


def _ratios_preserved(parent, children):
    parent_mass = parent["mass_ug"]
    for child in children:
        child_mass = child["mass_ug"]
        for name in _analyte_names(parent, child):
            if (child["analytes"].get(name, 0) * parent_mass
                    != parent["analytes"].get(name, 0) * child_mass):
                return False
    return True


def _step_batch_ids(step):
    ids = list(step["inputs"])
    ids.extend(output["id"] for output in step["outputs"])
    return ids


def _check_conservation(steps, compositions):
    merge_violations = []
    split_violations = []
    ratio_violations = []
    for step in steps:
        if step["type"] == "merge":
            if not _merge_conserved(step, compositions):
                merge_violations.append(step)
        else:
            parent = compositions[step["inputs"][0]]
            children = step["outputs"]
            if not _split_conserved(parent, children):
                split_violations.append(step)
            elif not _ratios_preserved(parent, children):
                ratio_violations.append(step)
    for code, message, violators in (
        (MERGE_NOT_CONSERVED,
         "merge step outputs must equal the item-by-item sum of their inputs",
         merge_violations),
        (SPLIT_NOT_CONSERVED,
         "split step outputs must conserve the parent batch mass and analyte totals",
         split_violations),
        (RATIO_MISMATCH,
         "every split output must keep the parent batch analyte ratios exactly",
         ratio_violations),
    ):
        if violators:
            raise AuditError(
                code,
                message,
                step_ids=[step["id"] for step in violators],
                batch_ids=sorted({bid for step in violators for bid in _step_batch_ids(step)}),
            )


# ---------------------------------------------------------------------------
# Certificate
# ---------------------------------------------------------------------------

def _certificate(sources, compositions, consumers):
    all_names = sorted({name for batch in compositions.values() for name in batch["analytes"]})
    terminal_ids = sorted(bid for bid in compositions if bid not in consumers)
    terminals = []
    for batch_id in terminal_ids:
        batch = compositions[batch_id]
        mass = batch["mass_ug"]
        analytes = {}
        concentrations = {}
        for name in all_names:
            amount = batch["analytes"].get(name, 0)
            analytes[name] = amount
            divisor = gcd(amount, mass)  # gcd(0, m) == m -> 0/1
            concentrations[name] = {
                "numerator": amount // divisor,
                "denominator": mass // divisor,
            }
        terminals.append({
            "id": batch_id,
            "mass_ug": mass,
            "analytes": analytes,
            "concentrations_ng_per_ug": concentrations,
        })
    totals = {name: sum(source["analytes"].get(name, 0) for source in sources)
              for name in all_names}
    return {
        "status": "ok",
        "terminal_batches": terminals,
        "source_totals": {
            "mass_ug": sum(source["mass_ug"] for source in sources),
            "analytes": totals,
        },
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def audit_payload(payload):
    """Audit one lineage payload.

    Returns the certificate dict on success; raises :class:`AuditError`
    (with a stable reason code) on any violation, producing no partial
    result.  The result is independent of the order in which sources and
    steps are listed.
    """
    sources, steps = _parse_payload(payload)
    _check_duplicate_steps(steps)
    producers = _check_duplicate_batches(sources, steps)
    compositions = {}
    for batch in sources:
        compositions[batch["id"]] = batch
    for step in steps:
        for output in step["outputs"]:
            compositions[output["id"]] = output
    consumers = _check_references(steps, producers)
    _check_cycles(steps)
    _check_conservation(steps, compositions)
    return _certificate(sources, compositions, consumers)
