"""Qualify, promote and withdraw parameterised operations using replay receipts."""

from copy import deepcopy
from hashlib import sha256
from .evidence import token


def qualify_method(record, replays):
    """Trusted caller supplies actual verifier receipts, not a model self-assessment."""
    method = deepcopy(record)
    op = method["operator"]
    for field in ("id", "revision", "scope"):
        if not method.get(field):
            raise ValueError(f"missing method {field}")
    if isinstance(method["revision"], bool) or not isinstance(method["revision"], int) or method["revision"] < 1:
        raise ValueError("invalid method revision")
    scope = method["scope"]
    if not isinstance(scope, dict) or not scope or set(scope) - {"brain_id", "project_id"} or not all(scope.values()):
        raise ValueError("method needs an explicit brain or project scope")
    if not op.get("job_kind") or not op.get("provides"):
        raise ValueError("method needs a runnable entry and a declared effect")
    implementation = sha256(token(op).encode()).hexdigest()
    replay_rows = list(replays)
    results = {"positive": [], "negative": []}
    projects = set()
    seen = set()
    for replay in replay_rows:
        receipt = replay["id"]
        if not receipt or receipt in seen or replay.get("passed") is not True or replay.get("case") not in results:
            raise ValueError("expected distinct successful positive and negative replays")
        if replay.get("implementation_digest", implementation) != implementation:
            raise ValueError("replay concerns a different method implementation")
        seen.add(receipt)
        results[replay["case"]].append(receipt)
        if replay["case"] == "positive" and replay.get("project_id"):
            projects.add(replay["project_id"])
    if not all(results.values()):
        raise ValueError("positive and negative replays are both required")
    if "project_id" not in scope and len(projects) < 2:
        raise ValueError("cross-project scope requires positive receipts from two projects")
    method.update(status="active", implementation_digest=implementation,
                  support_ids=results["positive"], counterexamples=results["negative"])
    return method


def active_methods(plan, history):
    latest = {}
    for method in history:
        if not method.get("scope") or any(plan.get(k) != v for k, v in method["scope"].items()):
            continue
        key = (token(method["scope"]), method["id"])
        old = latest.get(key)
        if old and old["revision"] == method["revision"] and old != method:
            raise ValueError("conflicting content for immutable method revision")
        if not old or old["revision"] < method["revision"]:
            latest[key] = method
    # A project override (including a revocation) shadows its brain-wide default.
    applicable = {m["id"]:m for m in sorted(latest.values(), key=lambda m: len(m["scope"]))}
    for m in applicable.values():
        if m.get("status") != "active" or not m.get("support_ids") or not m.get("counterexamples"):
            continue
        op = m["operator"]
        if m.get("implementation_digest") != sha256(token(op).encode()).hexdigest():
            continue
        yield {
            **op,
            "id": "method:" + m["id"] + "@" + str(m["revision"]),
            "parameters": m.get("parameters", op.get("parameters", [])),
            "requires": [*op.get("requires", []), *m.get("required_facts", [])],
            "exclude_facts": [*op.get("exclude_facts", []), *m.get("exclude_facts", [])],
            "permissions": sorted(set(op.get("permissions", [])) | set(m.get("permissions", []))),
            "method_revision": {"id": m["id"], "revision": m["revision"],
                                "scope": m["scope"], "digest": m["implementation_digest"]},
        }
