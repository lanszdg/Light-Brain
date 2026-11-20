"""Finite, evidence-grounded action composition for one goal revision."""

from collections import defaultdict
from itertools import product
import json
import math
import re

from .evidence import snapshot, token
from .experience import active_methods


VARIABLE = re.compile(r"\$\{([a-zA-Z_]\w*)\}")
AUTHORITIES = {
    "intent": {"user", "contract", "owner"},
    "world": {"device", "observer", "observation", "verifier", "verification", "worker", "receipt"},
    "implementation": {"device", "observer", "observation", "runner", "verifier", "verification",
                       "independent_verifier", "test", "worker", "receipt"},
}


def bind(value, bindings):
    if isinstance(value, dict):
        return {k: bind(v, bindings) for k, v in value.items()}
    if isinstance(value, list):
        return [bind(v, bindings) for v in value]
    if isinstance(value, str):
        match = VARIABLE.fullmatch(value)
        if match:
            return bindings[match.group(1)]
        return VARIABLE.sub(lambda m: str(bindings[m.group(1)]), value)
    return value


def _alternatives(branches, observations):
    remaining, rejected, evidence_conflicts = [], {}, {}
    for branch in branches:
        domain = branch.get("domain", "implementation")
        if domain not in AUTHORITIES:
            raise ValueError("unknown branch evidence domain")
        per_probe = defaultdict(list)
        for row in observations:
            if row.get("domain") == domain and row.get("source") in AUTHORITIES[domain]:
                per_probe[row["probe_id"]].append(row)
        reasons = []
        for probe, predicted in branch.get("predictions", {}).items():
            if predicted is None or not per_probe.get(probe):
                continue
            rows = per_probe[probe]
            actual = {token(o["value"]) for o in rows}
            if len(actual) > 1:
                evidence_conflicts[probe] = [o["id"] for o in rows]
            elif not actual.intersection(token(v) for v in predicted):
                reasons.extend(o["id"] for o in rows)
        if reasons:
            rejected[branch["id"]] = sorted(set(reasons))
        else:
            remaining.append(branch)
    return remaining, rejected, evidence_conflicts


def discriminate(branches, probes, usable, permissions):
    """Pick a bounded test by the *least* information any predicted result delivers."""
    best = None
    for probe in probes:
        if not set(probe.get("permissions", [])) <= permissions:
            continue
        if not all(token(v) in usable for v in probe.get("requires", [])):
            continue
        pid = probe["id"]
        domains = {b.get("domain", "implementation") for b in branches}
        if len(domains) > 1 and "domain" not in probe:
            continue
        domain = probe.get("domain", next(iter(domains)))
        predictions = {b["id"]: b.get("predictions", {}).get(pid) for b in branches
                       if b.get("domain", "implementation") == domain}
        outcomes = {token(v) for values in predictions.values() if values is not None for v in values}
        if not outcomes:
            continue
        eliminated = min(sum(values is not None and outcome not in {token(v) for v in values}
                             for values in predictions.values()) for outcome in outcomes)
        if eliminated <= 0:
            continue
        cost = _cost(probe)
        score = (-eliminated, cost, pid)
        if best is None or score < best[0]:
            best = (score, {**probe, "kind": probe.get("job_kind", "probe"), "payload": probe.get("payload", {}),
                            "predictions": predictions, "guaranteed_eliminations": eliminated})
    return best[1] if best else None


def _cost(op):
    value = op.get("cost", 1)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or not math.isfinite(value):
        raise ValueError("cost must be finite and nonnegative")
    return value


def _instances(plan, operators, permitted, remaining, usable, limit):
    result, skipped = [], []
    seen = set()
    known = set(plan.get("bindings", {}))
    consumed = 0
    for op in operators:
        op_id = op["id"]
        if not set(op.get("permissions", [])) <= permitted:
            skipped.append({"id": op_id, "reason": "permission"})
            continue
        branch = op.get("branch")
        if branch and remaining != [branch]:
            skipped.append({"id": op_id, "reason": "unconfirmed_branch"})
            continue
        # Instantiate declared finite domains; delegate an unbound variable back to the proposer.
        params = list(op.get("parameters", ()))
        missing = sorted(set(params) - known)
        domains = [plan.get("domains", {}).get(k, []) for k in missing]
        if any(not d for d in domains):
            skipped.append({"id": op_id, "reason": "unbound", "parameters": missing})
            continue
        for values in product(*domains):
            if consumed >= limit:
                return result, skipped, True
            consumed += 1
            bindings = {**plan.get("bindings", {}), **dict(zip(missing, values))}
            try:
                applied = bind(op, bindings)
            except KeyError as exc:
                skipped.append({"id": op_id, "reason": "unbound", "parameters": [str(exc.args[0])]})
                continue
            if any(token(v) in usable for v in applied.get("exclude_facts", [])):
                skipped.append({"id": op_id, "reason": "counterexample"})
                continue
            label = op_id + ("[" + token(dict(zip(missing, values))) + "]" if missing else "")
            if label in seen:
                raise ValueError(f"ambiguous operation id {label}")
            seen.add(label)
            result.append({**applied, "id": label, "kind": applied["job_kind"], "bindings": bindings,
                           "cost": _cost(applied), "requires": applied.get("requires", []),
                           "payload": applied.get("payload", {})})
    return result, skipped, False


def _producers(operators, goals):
    by_effect = defaultdict(list)
    for op in operators:
        for effect in op["provides"]:
            by_effect[token(effect)].append(op)
    try:
        import networkx as nx
    except ImportError:
        return by_effect
    # Slice the bipartite dependency graph backward from this delivery's effects.
    graph = nx.DiGraph()
    root = ("goal", "")
    graph.add_node(root)
    for g in goals:
        graph.add_edge(root, ("effect", g))
    for effect, producers in by_effect.items():
        for op in producers:
            node = ("operation", op["id"])
            graph.add_edge(("effect", effect), node)
            for item in op["requires"]:
                graph.add_edge(node, ("effect", token(item)))
    relevant = nx.descendants(graph, root)
    return {effect: ops for effect, ops in by_effect.items() if ("effect", effect) in relevant}


def solve(plan, facts, methods=(), budget=128):
    """Return executable jobs, projected routes and their evidence. Never performs I/O."""
    if not isinstance(budget, int) or isinstance(budget, bool) or not 0 < budget <= 10000:
        raise ValueError("budget must be in 1..10000")
    goal_values = plan.get("goal", [])
    if not isinstance(goal_values, list) or not goal_values:
        raise ValueError("goal must contain one or more effect keys")
    goals = [token(bind(g, plan.get("bindings", {}))) for g in goal_values]
    permissions = set(plan.get("permissions", []))
    usable, supports, negative, observations = snapshot(plan, facts)
    alive, rejected, contradictory = _alternatives(plan.get("branches", []), observations)
    branch_ids = [b["id"] for b in alive]
    probe = discriminate(alive, plan.get("probes", []), usable, permissions) if len(alive) > 1 else None
    ops, skipped, limited = _instances(plan, [*plan.get("operators", []), *active_methods(plan, methods)],
                                       permissions, branch_ids, usable, max(budget, len(plan.get("operators", []))))
    index = _producers(ops, goals)
    by_id = {op["id"]: op for op in ops}
    gaps, cycles = set(), set()
    remaining = budget
    # A route is a topologically ordered set of projected actions. Only route members
    # whose inputs already have live support can appear in ready.
    def path(effect, visiting, missing):
        nonlocal remaining, limited
        if effect in usable:
            return ()
        if effect in visiting:
            cycles.add(effect)
            return None
        if remaining <= 0 or len(visiting) >= 256:
            limited = True
            return None
        candidates = index.get(effect, [])
        if not candidates:
            missing.add(effect)
        best, failed_inputs = None, set()
        for op in sorted(candidates, key=lambda o: (o["cost"], o["id"])):
            if remaining <= 0:
                limited = True
                break
            remaining -= 1
            ordered, unmet = {}, set()
            for req in op["requires"]:
                part = path(token(req), visiting | {effect}, unmet)
                if part is None:
                    failed_inputs.update(unmet)
                    break
                ordered.update(dict.fromkeys(part))
            else:
                ordered[op["id"]] = None
                route = tuple(ordered)
                score = (sum(by_id[k]["cost"] for k in route), len(route), route)
                if best is None or score < best[0]:
                    best = (score, route)
        if best is None:
            missing.update(failed_inputs)
            return None
        return best[1]

    routes, selected = [], {}
    for effect in goals:
        chain = path(effect, set(), gaps)
        if chain is not None:
            routes.append({"goal": json.loads(effect), "steps": list(chain), "actual": effect in usable})
            selected.update(dict.fromkeys(chain))
    ready = []
    for op_id in selected:
        op = by_id[op_id]
        if all(token(req) in usable for req in op["requires"]):
            ready.append({**op, "support_ids": sorted({i for req in op["requires"] for i in supports[token(req)]})})
    return {
        "satisfied": all(g in usable for g in goals), "ready": ready, "routes": routes,
        "gaps": [json.loads(k) for k in sorted(gaps)], "cycles": [json.loads(k) for k in sorted(cycles)],
        "probe": probe, "branches": branch_ids, "rejected_branches": rejected, "skipped": skipped,
        "evidence": [{"effect": json.loads(k), "support_ids": ids, "refutations": negative.get(k, [])}
                     for k, ids in supports.items()],
        "conflicting_observations": contradictory, "budget_exhausted": limited,
    }
