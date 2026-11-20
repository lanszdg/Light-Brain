"""Turn independently observed coding behaviour into reusable, scoped checks."""

from copy import deepcopy
from hashlib import file_digest, sha256
from pathlib import PurePosixPath
import json
import re

from .core import qualify_method
from .host.runner import expect_body
from .store import NotFound


def checksum(value):
    return sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                             sort_keys=True).encode()).hexdigest()


def _entry(criterion):
    """Trusted adapter for two concrete, finite check languages. Never executes here."""
    kind = criterion.get("kind")
    argv = criterion.get("argv")
    if kind not in {"command", "http"} or not isinstance(argv, list) or len(argv) < 2:
        return None
    runtime, script = argv[:2]
    if runtime in {"python", "python3"} and script.endswith(".py"):
        family = "python"
    elif runtime == "node" and script.endswith((".js", ".mjs", ".cjs")):
        family = "node"
    else:
        return None
    path = PurePosixPath(script)
    if path.is_absolute() or ".." in path.parts or script.startswith(("-", "\\")):
        return None
    if kind == "command":
        if not any(k in criterion for k in ("stdout_equals", "stdout_contains", "json_equals", "expect_exit")):
            return None
        # Match the command-line interface, then replay the previously verified vector.
        # The exact expected output and historical parameter values stay in the method.
        interface = [v for v in argv[2:] if v.startswith("-")]
        if len(argv) > 2 and re.fullmatch(r"[a-z][a-z_-]*", argv[2]):
            interface = [argv[2], *interface]
        return [kind, family, interface]
    requests = criterion.get("requests")
    if not isinstance(requests, list) or not requests:
        return None
    interface = []
    for req in requests:
        if not isinstance(req, dict) or not isinstance(req.get("path"), str):
            return None
        path = req["path"].split("?", 1)[0]
        if not path.startswith("/") or path.startswith("//"):
            return None
        interface.append([req.get("method", "GET").upper(), path])
    return [kind, family, interface]


def _rebind(source, target):
    # Intentionally accepts only compatible language, operation and argument interface.
    # Use target entrypoint/port and source behaviour vector. The next verify job must
    # run this exact adapted criterion; a model's assertion about compatibility is insufficient.
    signature = _entry(source)
    if not signature or signature != _entry(target):
        return None
    check = deepcopy(source)
    check["argv"][:2] = target["argv"][:2]
    if check["kind"] == "http":
        check["port"] = target["port"]
    return check


def _behaviour(criterion):
    projection = {k: v for k, v in criterion.items() if k not in {"id", "version", "port", "argv"}}
    return {"interface": _entry(criterion), "args": criterion["argv"][2:], "check": projection}


def _observed_result(c, observation):
    if not observation.get("environment", {}).get("executor") or "execution_error" in observation:
        return None
    try:
        if c["kind"] == "command":
            stdout = observation["stdout"]
            return (observation["exit_code"] == c.get("expect_exit", 0)
                    and c.get("stdout_contains", "") in stdout
                    and c.get("stdout_equals", stdout) == stdout
                    and expect_body(c, stdout))
        replies = observation["responses"]
        return (len(replies) == len(c["requests"])
                and all(r["status"] == rule.get("expect_status", rule.get("status", 200))
                        and expect_body(rule, r["body"]) for r, rule in zip(replies, c["requests"])))
    except (TypeError, KeyError, ValueError, IndexError):
        return None


def _final_sha(case):
    return (case.get("delivery") or case.get("candidate") or {}).get("candidate_sha") or case.get("candidate_sha")


def _idea_hash(case):
    idea = case["request"].get("idea")
    return checksum(idea.strip()) if isinstance(idea, str) and idea.strip() else None


def _grounded(store, case):
    criteria = case.get("criteria") or case.get("plan", {}).get("criteria") or []
    if not criteria:
        return
    frozen = {row["id"]:row for row in store.list_criteria(case["id"])
              if row["version"] == case.get("goal_version", 1)}
    for job in store.list_jobs(case["id"]):
        if job["kind"] != "verify" or job["status"] != "finished":
            continue
        p, r = job["payload"], job.get("receipt") or {}
        sha, digest = p.get("candidate_sha", ""), checksum(criteria)
        if (p.get("criteria") != criteria or p.get("criteria_digest") != digest
                or r.get("criteria_digest") != digest or r.get("candidate_sha") != sha
                or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", sha)
                or p.get("goal_version") != case.get("goal_version", 1)
                or r.get("kind") != "verify" or not r.get("environment")):
            continue
        if not all(c["id"] in frozen and all(frozen[c["id"]].get(k) == v for k,v in c.items())
                   and not set(frozen[c["id"]]) - set(c) - {"authority","intent_quote","frozen_at","version","digest"}
                   for c in criteria):
            continue
        bid = p.get("bundle_id") or r.get("bundle_id")
        if not bid or r.get("bundle_sha256") != bid:
            continue
        try:
            path = store.read_path(bid, case["id"])
        except NotFound:
            continue
        with path.open("rb") as stream:
            if file_digest(stream, "sha256").hexdigest() != bid:
                continue
        by_id = {c["id"]: c for c in criteria}
        for actual in r.get("checks", []):
            criterion = by_id.get(actual.get("id"))
            if (criterion and criterion["kind"] == actual.get("kind") and _entry(criterion)
                    and isinstance(actual.get("ok"), bool)
                    and _observed_result(criterion, actual.get("observed", {})) is actual["ok"]):
                yield criterion, job, actual


def _trusted_hit(store, case, source):
    """A second case counts only if the same bound behaviour actually passed there."""
    for target, job, check in _grounded(store, case):
        adapted = _rebind(source, target)
        if (adapted and _behaviour(adapted) == _behaviour(target)
                and check["ok"] and job["receipt"].get("ok") is True
                and _final_sha(case) == job["payload"]["candidate_sha"]):
            return job, target
    return None


def _boundary(source):
    # Exercise the actual applicability guard on an out-of-domain input. Keep this
    # counterexample distinct from a failed implementation that should have passed.
    outside = {"kind":"file", "id":"out-of-domain", "path":"README.md", "contains":"run"}
    if _rebind(source, outside) is not None:
        raise ValueError("method incorrectly accepts a static check as behavioural evidence")
    return {"id":"domain:" + checksum([_entry(source), outside]), "case":"negative",
            "passed":True, "input":outside, "observed":"not_applicable"}


def _positive(case, job, criterion):
    return {"id":job["id"] + "/" + criterion["id"], "case":"positive", "passed":True,
            "project_id":case["project_id"], "case_id":case["id"], "candidate_sha":job["payload"]["candidate_sha"],
            "bundle_id":job["receipt"]["bundle_sha256"], "criteria_digest":job["receipt"]["criteria_digest"]}


def learn_from_case(store, case):
    """Extract replayable checks. The case argument identifies the formal store snapshot."""
    case = store.get_case(case["id"] if isinstance(case, dict) else case)
    rows = list(_grounded(store, case))
    result = {}
    for criterion, job, observed in rows:
        sha = job["payload"]["candidate_sha"]
        if not observed["ok"] or job["receipt"].get("ok") is not True or _final_sha(case) != sha:
            continue
        signature = _behaviour(criterion)
        mid = "check-" + checksum(signature)[:24]
        brain, project = case["brain_id"], case["project_id"]
        history = store.method_history(brain, project, mid)
        if history:
            if history[-1]["status"] == "active":
                result[mid] = history[-1]
            continue
        variable = "vector_" + mid.split("-", 1)[1]
        op = {"id":mid, "job_kind":"method_verify", "requires":["checked_candidate", "compatible:" + mid],
              "provides":["method_verified:" + mid], "parameters":[variable],
              "payload":{"method_id":mid, "criteria":"${" + variable + "}"}}
        replays = [_positive(case, job, criterion), _boundary(criterion)]
        failures = [j["id"] + "/" + c["id"] for c,j,a in rows
                    if c["id"] == criterion["id"] and not a["ok"] and j["payload"]["candidate_sha"] != sha]
        method = qualify_method({"id":mid, "revision":1, "scope":{"brain_id":brain, "project_id":project},
                                 "operator":op, "source_case":case["id"], "source_criterion":criterion,
                                 "intent_digest":_idea_hash(case),
                                 "interface":_entry(criterion), "regressions_seen":failures,
                                 "title":"Replay " + criterion["id"],
                                 "description":"Run the previously verified " + criterion["kind"] + " contract "
                                               + criterion["id"] + " with interface " + json.dumps(_entry(criterion)),
                                 "replays":replays}, replays)
        result[mid] = store.register_method(brain, project, method)
    return list(result.values())


def bind_methods(store, case, criteria, use_methods):
    methods, facts, goals, bindings = [], [], [], {}
    requested = set(use_methods or [])
    # Prefer a scoped project method to a brain-wide method with the same stable ID.
    records = {m["id"]:m for m in sorted(store.list_methods(case["brain_id"], case["project_id"], True),
                                         key=lambda m: len(m["scope"]))}
    # A named grant from the owner borrows this revision for one case. The model
    # alone cannot grant access to another project's private method directory.
    explicit=set(case["request"].get("reuse_methods") or [])
    for ref in sorted(explicit):
        if "/" not in ref:
            continue
        project, mid=ref.split("/",1)
        if not mid.startswith("check-"):
            continue
        try:
            method=store.get_method(case["brain_id"],project,mid)
        except NotFound:
            continue
        if method["status"]=="active":
            records[mid]={**method,"source_scope":method["scope"],"scope":{"brain_id":case["brain_id"],"project_id":case["project_id"]}}
            requested.add(mid)
            explicit.add(mid)
    for mid in sorted(requested & records.keys()):
        method = records[mid]
        if (method["status"] != "active" or "source_criterion" not in method
                or method.get("operator", {}).get("job_kind") != "method_verify"):
            continue
        source = method["source_criterion"]
        # Two CLI programs with identical shapes may have contrary semantics. Expand
        # old examples only under the same commission or an owner's explicit reuse grant.
        authorised = mid in explicit
        identical_goal = bool(method.get("intent_digest") and method["intent_digest"] == _idea_hash(case))
        matches = [candidate for c in criteria if (candidate := _rebind(source, c))
                   and (authorised or identical_goal or _behaviour(candidate) == _behaviour(c))]
        # Two genuinely different entrypoints require a user/model binding decision.
        unique = {checksum(c): c for c in matches}
        if len(unique) != 1:
            continue
        methods.append(method)
        facts.append("compatible:" + mid)
        goals.append("method_verified:" + mid)
        bindings[method["operator"]["parameters"][0]] = [next(iter(unique.values()))]
    return methods, facts, goals, bindings


def promote(store, brain, source_project, method_id, other_case_id):
    m = store.get_method(brain, source_project, method_id)
    if m["status"] != "active" or m.get("operator", {}).get("job_kind") != "method_verify":
        raise ValueError("source method is inactive")
    target = store.get_case(other_case_id)
    if target["brain_id"] != brain or target["project_id"] == source_project:
        raise ValueError("promotion requires a different project of the same brain")
    source = store.get_case(m["source_case"])
    initial = next(r for r in m["replays"] if r["case"] == "positive")
    original_job = store.get_job(initial["id"].split("/", 1)[0])
    p = original_job["payload"]
    if original_job["case_id"] != source["id"] or initial["candidate_sha"] != p["candidate_sha"]:
        raise ValueError("source replay no longer resolves to the recorded candidate")
    original = {**source,"criteria":p["criteria"],"goal_version":p["goal_version"],"delivery":None,
                "candidate":{"candidate_sha":p["candidate_sha"]}}
    first = _trusted_hit(store, original, m["source_criterion"])
    second = _trusted_hit(store, target, m["source_criterion"])
    if not first or not second:
        raise ValueError("both projects must independently pass the same behavioural contract")
    if first[0]["payload"]["candidate_sha"] == second[0]["payload"]["candidate_sha"]:
        raise ValueError("the two projects must provide independent candidates")
    history = store.method_history(brain, None, method_id)
    if history and history[-1]["status"] == "active":
        return history[-1]
    replays = [_positive(source, *first), _positive(target, *second), _boundary(m["source_criterion"])]
    shared = qualify_method({**m, "scope":{"brain_id":brain},
                             "revision":history[-1]["revision"] + 1 if history else 1, "replays":replays}, replays)
    return store.register_method(brain, None, shared)
