"""Reconstruct the supported facts of one immutable case snapshot."""

from collections import defaultdict
import json


def token(value):
    """A typed identity. A numeric sensor reading cannot equal its string label."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fact_key(row):
    key = row["key"]
    labels = {name: row[name] for name in ("object", "unit", "branch", "version") if name in row}
    if not labels:
        return key
    if isinstance(key, dict):
        return {**key, **labels}
    return {"key": key, **labels}


def same_scope(row, plan):
    return all(name not in row or row[name] == plan.get(name)
               for name in ("brain_id", "case_id", "project_id", "goal_revision", "environment", "candidate_sha"))


def _closure(rows):
    # Every support group is a conjunction; alternative groups form a disjunction.
    # Positive least-fixed-point semantics prevent a cycle from supporting itself.
    try:
        import clingo
    except ImportError:
        live = {k for k, groups in rows.items() if [] in groups}
        while True:
            added = {k for k, groups in rows.items() if any(all(x in live for x in g) for g in groups)}
            if added <= live:
                return live
            live.update(added)
    numbers = {k: i for i, k in enumerate(rows)}
    rules = []
    for name, groups in rows.items():
        for group in groups:
            if all(parent in numbers for parent in group):
                body = ",".join(f"live({numbers[parent]})" for parent in group)
                rules.append(f"live({numbers[name]})" + (" :- " + body if body else "") + ".")
    if not rules:
        return set()
    control = clingo.Control(["--warn=none"])
    control.add("base", [], "\n".join(rules))
    control.ground([("base", [])])
    with control.solve(yield_=True) as handle:
        ids = {a.arguments[0].number for m in handle for a in m.symbols(atoms=True) if a.name == "live"}
    return {k for k, n in numbers.items() if n in ids}


def snapshot(plan, records):
    """The caller supplies an authorized case snapshot (or ordered receipt history)."""
    evidence = {}
    observations = []
    for index, raw in enumerate(records):
        row = {"key": raw} if isinstance(raw, str) else raw
        if not isinstance(row, dict) or not same_scope(row, plan):
            continue
        if row.get("stage") in ("predicted", "hypothesis") or row.get("source") in ("model", "prediction"):
            continue
        receipt = str(row.get("id", f"snapshot:{index}"))
        evidence[receipt] = row
    groups = {}
    for receipt, row in evidence.items():
        if row.get("status") in ("withdrawn", "retracted", "superseded"):
            continue
        support = row.get("supports", [[]])
        if not isinstance(support, list) or not all(isinstance(g, list) for g in support):
            raise ValueError("supports must be a list of alternative groups of evidence ids")
        groups[receipt] = support
    live = _closure(groups)
    support_ids = defaultdict(list)
    refutations = defaultdict(list)
    for receipt in sorted(live):
        row = evidence[receipt]
        if "probe_id" in row and "value" in row:
            observations.append({**row, "id": receipt})
        if "key" in row:
            key = token(fact_key(row))
            target = refutations if row.get("polarity") == "negative" or row.get("value") is False else support_ids
            target[key].append(receipt)
    usable = set(support_ids) - set(refutations)
    return usable, dict(support_ids), dict(refutations), observations
