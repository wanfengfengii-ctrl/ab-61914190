"""Core batch-genealogy audit for blend / aliquot certificate issuance.

A request declares 1..64 source batches and 1..256 steps (in any order).
Each batch carries a positive integer mass in micrograms and, per analyte, a
non-negative integer amount in nanograms.  A step either merges several
batches into one, or splits one batch into 2..16 children.

Rules enforced here (all amounts are exact integers, so every check is
exact):

* every produced batch id is created by exactly one step and never collides
  with a source id;
* every batch id is consumed by at most one subsequent step;
* the genealogy is acyclic and never references an unknown batch id;
* merges conserve mass and every analyte item-by-item;
* splits conserve mass and analyte totals, and every child keeps exactly the
  same analyte concentration (analyte ng / batch ug) as the parent.

The audit is order-independent: steps are resolved as a graph, so any
permutation of a legal genealogy yields the same certificate, and any
permutation of an illegal one yields the same stable reason code.
"""

from __future__ import annotations

import math
from collections import Counter, deque

# ---- limits ---------------------------------------------------------------
MAX_SOURCES = 64
MAX_STEPS = 256
MIN_MERGE_INPUTS = 2
MIN_SPLIT_OUTPUTS = 2
MAX_SPLIT_OUTPUTS = 16
MAX_NAME_LEN = 128

# ---- stable reason codes ---------------------------------------------------
INVALID_SCHEMA = "INVALID_SCHEMA"
DUPLICATE_STEP_ID = "DUPLICATE_STEP_ID"
DUPLICATE_SOURCE_ID = "DUPLICATE_SOURCE_ID"
DUPLICATE_OUTPUT_ID = "DUPLICATE_OUTPUT_ID"
UNKNOWN_BATCH = "UNKNOWN_BATCH"
BATCH_RECONSUMED = "BATCH_RECONSUMED"
CYCLE_DETECTED = "CYCLE_DETECTED"
CONSERVATION_VIOLATION = "CONSERVATION_VIOLATION"
RATIO_MISMATCH = "RATIO_MISMATCH"


class AuditRejection(Exception):
    """A payload violated a genealogy rule; carries a stable reason code.

    ``step_ids`` / ``batch_ids`` identify the offending steps / batches and
    are always sorted so the same defect reports identically regardless of
    how the request ordered its steps.
    """

    def __init__(self, reason_code, detail, step_ids=(), batch_ids=()):
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail
        self.step_ids = sorted(step_ids)
        self.batch_ids = sorted(batch_ids)


# ---- schema validation ------------------------------------------------------


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_name(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_NAME_LEN


def _schema_error(detail):
    return AuditRejection(INVALID_SCHEMA, detail)


def _check_keys(obj, allowed, where):
    missing = [k for k in allowed if k not in obj]
    if missing:
        raise _schema_error(f"{where}: missing field(s): {', '.join(sorted(missing))}")
    extra = [k for k in obj if k not in allowed]
    if extra:
        raise _schema_error(f"{where}: unknown field(s): {', '.join(sorted(extra))}")


def _validate_batch(obj, where):
    if not isinstance(obj, dict):
        raise _schema_error(f"{where}: must be an object")
    _check_keys(obj, ("id", "mass_ug", "analytes"), where)
    if not _is_name(obj["id"]):
        raise _schema_error(f"{where}.id: must be a non-empty string of at most {MAX_NAME_LEN} chars")
    if not _is_int(obj["mass_ug"]) or obj["mass_ug"] < 1:
        raise _schema_error(f"{where}.mass_ug: must be a positive integer")
    analytes = obj["analytes"]
    if not isinstance(analytes, dict):
        raise _schema_error(f"{where}.analytes: must be an object mapping analyte names to non-negative integers")
    for name, amount in analytes.items():
        if not _is_name(name):
            raise _schema_error(f"{where}.analytes: analyte names must be non-empty strings of at most {MAX_NAME_LEN} chars")
        if not _is_int(amount) or amount < 0:
            raise _schema_error(f"{where}.analytes[{name!r}]: must be a non-negative integer")
    return {"id": obj["id"], "mass_ug": obj["mass_ug"], "analytes": dict(analytes)}


def _validate_id_list(obj, where):
    if not isinstance(obj, list) or any(not _is_name(item) for item in obj):
        raise _schema_error(f"{where}: must be a list of batch id strings")
    return list(obj)


def _validate_step(obj, index):
    where = f"steps[{index}]"
    if not isinstance(obj, dict):
        raise _schema_error(f"{where}: must be an object")
    if "id" not in obj or not _is_name(obj["id"]):
        raise _schema_error(f"{where}.id: must be a non-empty string of at most {MAX_NAME_LEN} chars")
    step_type = obj.get("type")
    if step_type == "merge":
        _check_keys(obj, ("id", "type", "inputs", "output"), where)
        inputs = _validate_id_list(obj["inputs"], f"{where}.inputs")
        if len(inputs) < MIN_MERGE_INPUTS:
            raise _schema_error(f"{where}.inputs: a merge consumes at least {MIN_MERGE_INPUTS} batches")
        output = _validate_batch(obj["output"], f"{where}.output")
        return {"id": obj["id"], "type": "merge", "inputs": inputs, "output": output}
    if step_type == "split":
        _check_keys(obj, ("id", "type", "input", "outputs"), where)
        if not _is_name(obj.get("input")):
            raise _schema_error(f"{where}.input: must be a batch id string")
        raw_outputs = obj["outputs"]
        if not isinstance(raw_outputs, list) or not (MIN_SPLIT_OUTPUTS <= len(raw_outputs) <= MAX_SPLIT_OUTPUTS):
            raise _schema_error(
                f"{where}.outputs: a split produces between {MIN_SPLIT_OUTPUTS} and {MAX_SPLIT_OUTPUTS} batches"
            )
        outputs = [_validate_batch(child, f"{where}.outputs[{i}]") for i, child in enumerate(raw_outputs)]
        return {"id": obj["id"], "type": "split", "input": obj["input"], "outputs": outputs}
    raise _schema_error(f"{where}.type: must be 'merge' or 'split'")


def _validate_payload(payload):
    if not isinstance(payload, dict):
        raise _schema_error("request body must be a JSON object")
    _check_keys(payload, ("sources", "steps"), "request")

    raw_sources = payload["sources"]
    if not isinstance(raw_sources, list) or not (1 <= len(raw_sources) <= MAX_SOURCES):
        raise _schema_error(f"sources: must be a list of 1..{MAX_SOURCES} batches")
    sources = [_validate_batch(s, f"sources[{i}]") for i, s in enumerate(raw_sources)]

    raw_steps = payload["steps"]
    if not isinstance(raw_steps, list) or not (1 <= len(raw_steps) <= MAX_STEPS):
        raise _schema_error(f"steps: must be a list of 1..{MAX_STEPS} steps")
    steps = [_validate_step(s, i) for i, s in enumerate(raw_steps)]

    duplicate_step_ids = sorted(sid for sid, n in Counter(s["id"] for s in steps).items() if n > 1)
    if duplicate_step_ids:
        raise AuditRejection(
            DUPLICATE_STEP_ID,
            f"step id(s) defined more than once: {', '.join(duplicate_step_ids)}",
            step_ids=duplicate_step_ids,
        )
    duplicate_source_ids = sorted(bid for bid, n in Counter(s["id"] for s in sources).items() if n > 1)
    if duplicate_source_ids:
        raise AuditRejection(
            DUPLICATE_SOURCE_ID,
            f"source batch id(s) defined more than once: {', '.join(duplicate_source_ids)}",
            batch_ids=duplicate_source_ids,
        )
    return sources, steps


def _step_inputs(step):
    return step["inputs"] if step["type"] == "merge" else [step["input"]]


def _step_outputs(step):
    return [step["output"]] if step["type"] == "merge" else step["outputs"]


# ---- conservation / ratio checks --------------------------------------------


def _check_merge(step, known):
    inputs = [known[batch_id] for batch_id in step["inputs"]]
    output = step["output"]
    total_mass = sum(b["mass_ug"] for b in inputs)
    if total_mass != output["mass_ug"]:
        raise AuditRejection(
            CONSERVATION_VIOLATION,
            f"merge step '{step['id']}': output mass_ug {output['mass_ug']} != sum of inputs {total_mass}",
            step_ids=[step["id"]],
            batch_ids=[output["id"]],
        )
    keys = set(output["analytes"])
    for b in inputs:
        keys |= set(b["analytes"])
    for key in sorted(keys):
        expected = sum(b["analytes"].get(key, 0) for b in inputs)
        actual = output["analytes"].get(key, 0)
        if actual != expected:
            raise AuditRejection(
                CONSERVATION_VIOLATION,
                f"merge step '{step['id']}': analyte '{key}' output {actual} ng != sum of inputs {expected} ng",
                step_ids=[step["id"]],
                batch_ids=[output["id"]],
            )


def _check_split(step, known):
    parent = known[step["input"]]
    children = step["outputs"]
    total_mass = sum(c["mass_ug"] for c in children)
    if total_mass != parent["mass_ug"]:
        raise AuditRejection(
            CONSERVATION_VIOLATION,
            f"split step '{step['id']}': children mass_ug sum {total_mass} != parent '{parent['id']}' mass_ug {parent['mass_ug']}",
            step_ids=[step["id"]],
            batch_ids=[parent["id"]],
        )
    keys = set(parent["analytes"])
    for c in children:
        keys |= set(c["analytes"])
    for key in sorted(keys):
        expected = parent["analytes"].get(key, 0)
        actual = sum(c["analytes"].get(key, 0) for c in children)
        if actual != expected:
            raise AuditRejection(
                CONSERVATION_VIOLATION,
                f"split step '{step['id']}': children analyte '{key}' sum {actual} ng != parent '{parent['id']}' {expected} ng",
                step_ids=[step["id"]],
                batch_ids=[parent["id"]],
            )
    # Every child must keep the parent's exact analyte concentration:
    # child_ng / child_ug == parent_ng / parent_ug, checked by cross-multiplication.
    for child in children:
        child_keys = set(parent["analytes"]) | set(child["analytes"])
        for key in sorted(child_keys):
            child_ng = child["analytes"].get(key, 0)
            parent_ng = parent["analytes"].get(key, 0)
            if child_ng * parent["mass_ug"] != parent_ng * child["mass_ug"]:
                raise AuditRejection(
                    RATIO_MISMATCH,
                    f"split step '{step['id']}': child '{child['id']}' analyte '{key}' concentration "
                    f"{child_ng}/{child['mass_ug']} ng/ug differs from parent '{parent['id']}' "
                    f"{parent_ng}/{parent['mass_ug']} ng/ug",
                    step_ids=[step["id"]],
                    batch_ids=[parent["id"], child["id"]],
                )


# ---- certificate assembly ----------------------------------------------------


def _reduced_fraction(numerator, denominator):
    gcd = math.gcd(numerator, denominator)
    return {"numerator": numerator // gcd, "denominator": denominator // gcd}


def _certificate(sources, known, consumed_ids):
    terminals = []
    for batch_id in sorted(known):
        if batch_id in consumed_ids:
            continue
        batch = known[batch_id]
        terminals.append(
            {
                "id": batch["id"],
                "mass_ug": batch["mass_ug"],
                "analytes": {k: batch["analytes"][k] for k in sorted(batch["analytes"])},
                "concentrations": {
                    k: _reduced_fraction(batch["analytes"][k], batch["mass_ug"])
                    for k in sorted(batch["analytes"])
                },
            }
        )

    total_mass = sum(s["mass_ug"] for s in sources)
    totals = {}
    for s in sources:
        for key, amount in s["analytes"].items():
            totals[key] = totals.get(key, 0) + amount

    return {
        "status": "ok",
        "terminal_batches": terminals,
        "source_totals": {
            "mass_ug": total_mass,
            "analytes": {k: totals[k] for k in sorted(totals)},
        },
    }


# ---- entry point --------------------------------------------------------------


def audit(payload):
    """Audit one genealogy payload.

    Returns the certificate dict on success; raises :class:`AuditRejection`
    with a stable reason code on the first rule violated.  Checks run in a
    fixed order and ties break on sorted ids, so the outcome never depends on
    the order in which sources or steps were listed.
    """
    sources, steps = _validate_payload(payload)
    source_by_id = {s["id"]: s for s in sources}
    step_ids = [s["id"] for s in steps]

    # Register produced batches: each id may be produced exactly once and may
    # not collide with a source id.
    produced = {}
    producer_step = {}
    duplicate_outputs = set()
    duplicate_output_steps = set()
    for step in steps:
        for batch in _step_outputs(step):
            batch_id = batch["id"]
            if batch_id in produced:
                duplicate_outputs.add(batch_id)
                duplicate_output_steps.add(producer_step[batch_id])
                duplicate_output_steps.add(step["id"])
            elif batch_id in source_by_id:
                duplicate_outputs.add(batch_id)
                duplicate_output_steps.add(step["id"])
            else:
                produced[batch_id] = batch
                producer_step[batch_id] = step["id"]
    if duplicate_outputs:
        raise AuditRejection(
            DUPLICATE_OUTPUT_ID,
            f"batch id(s) produced more than once or colliding with a source: {', '.join(sorted(duplicate_outputs))}",
            step_ids=duplicate_output_steps,
            batch_ids=duplicate_outputs,
        )

    known = dict(source_by_id)
    known.update(produced)

    # Resolve every step input; each batch may be consumed at most once.
    consumers = {}
    unknown_refs = set()
    unknown_ref_steps = set()
    for step in sorted(steps, key=lambda s: s["id"]):
        for batch_id in _step_inputs(step):
            if batch_id not in known:
                unknown_refs.add(batch_id)
                unknown_ref_steps.add(step["id"])
            else:
                consumers.setdefault(batch_id, []).append(step["id"])
    if unknown_refs:
        raise AuditRejection(
            UNKNOWN_BATCH,
            f"step input(s) reference unknown batch id(s): {', '.join(sorted(unknown_refs))}",
            step_ids=unknown_ref_steps,
            batch_ids=unknown_refs,
        )
    reconsumed = sorted(b for b, users in consumers.items() if len(users) > 1)
    if reconsumed:
        involved = sorted({sid for b in reconsumed for sid in consumers[b]})
        raise AuditRejection(
            BATCH_RECONSUMED,
            f"batch id(s) consumed by more than one step: {', '.join(reconsumed)}",
            step_ids=involved,
            batch_ids=reconsumed,
        )
    consumer_step = {b: users[0] for b, users in consumers.items()}

    # Cycle detection over the step graph (Kahn's algorithm).  Edges run from
    # the step that produced a batch to the step that consumes it.
    indegree = {sid: 0 for sid in step_ids}
    adjacency = {sid: [] for sid in step_ids}
    seen_edges = set()
    for batch_id, consumer in consumer_step.items():
        producer = producer_step.get(batch_id)
        if producer is None or (producer, consumer) in seen_edges:
            continue
        seen_edges.add((producer, consumer))
        adjacency[producer].append(consumer)
        indegree[consumer] += 1
    queue = deque(sorted(sid for sid in step_ids if indegree[sid] == 0))
    ordered = 0
    while queue:
        node = queue.popleft()
        ordered += 1
        for nxt in adjacency[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if ordered < len(step_ids):
        remaining = sorted(sid for sid in step_ids if indegree[sid] > 0)
        raise AuditRejection(
            CYCLE_DETECTED,
            f"batch genealogy contains a cycle involving step(s): {', '.join(remaining)}",
            step_ids=remaining,
        )

    # Quantity checks, in a deterministic (sorted) step order.
    for step in sorted(steps, key=lambda s: s["id"]):
        if step["type"] == "merge":
            _check_merge(step, known)
        else:
            _check_split(step, known)

    return _certificate(sources, known, set(consumer_step))
