"""Bind live problem representations to actual owner decisions or versioned observations."""

from copy import deepcopy
import math
import re

from .core import solve
from .contracts import digest


PROMPT = """
If genuinely competing interpretations would change the implementation, you may supply inquiry.
Use one homogeneous domain, "intent" (owner preference) or "implementation" (observable behaviour of
the existing repository). For a new repository use intent or omit inquiry. Do not delegate a known
requirement back to the owner. Omit duplicate decisions when an inquiry already resolves a question.
Schema: {domain, branches:[{id, action_ids:[plan action IDs conditional on this branch],
predictions:{probe_id:[possible scalar observations]}}], probes:[...]}. Omitted predictions mean unknown.
Shared actions may occur in all branches or be unreferenced. A common action must not depend on an
action available only under a discarded branch. Freeze only acceptance criteria that remain valid
across branches, unless the owner supplied criteria. Use actual option labels as intent predictions.
Intent probe: {id,cost,question,choices:["choice A","choice B"]}. Implementation probe:
{id,cost,criterion:{id,kind:"command",argv:["python3","existing.py"],expect_exit:0},read:"stdout"|"exit_code"}.
The system chooses a distinguishing probe and applies the observed branch. Never invent an observation.
"""


def _identifier(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[\w.-]{1,96}", value))


def _distinct_ids(records, label, minimum, maximum):
    if not isinstance(records, list) or not minimum <= len(records) <= maximum:
        raise ValueError(f"{label} expects {minimum} to {maximum} entries")
    ids = [r.get("id") for r in records if isinstance(r, dict)]
    if len(ids) != len(records) or not all(map(_identifier, ids)) or len(ids) != len(set(ids)):
        raise ValueError(f"invalid or duplicate {label} id")
    return set(ids)


def validate(raw_inquiry, action_ids=()):
    if raw_inquiry is None:
        return None
    if not isinstance(raw_inquiry, dict):
        raise ValueError("inquiry must be an object")
    q = deepcopy(raw_inquiry)
    domain = q.get("domain")
    if domain not in ("intent", "implementation"):
        raise ValueError("inquiry.domain must be intent or implementation")
    _distinct_ids(q.get("branches"), "branches", 2, 8)
    probes = _distinct_ids(q.get("probes"), "probes", 1, 12)
    actions = {a["id"] if isinstance(a, dict) else a for a in action_ids}
    choices = {}
    for p in q["probes"]:
        cost = p.get("cost", 1)
        if type(cost) not in (int,float) or not math.isfinite(cost) or cost < 0:
            raise ValueError("invalid inquiry probe cost")
        if domain == "intent":
            options = p.get("choices")
            if (not isinstance(options, list) or not 2 <= len(options) <= 12 or
                    not all(isinstance(o,str) and o.strip() and len(o)<=300 for o in options)
                    or len(set(options)) != len(options) or not isinstance(p.get("question"),str)
                    or not p["question"].strip() or "criterion" in p):
                raise ValueError("intent requires an owner question and distinct text choices")
            choices[p["id"]] = set(options)
        else:
            c = p.get("criterion", {})
            if (c.get("kind") != "command" or not _identifier(c.get("id"))
                    or not isinstance(c.get("argv"),list) or not 1 <= len(c["argv"]) <= 64
                    or not all(isinstance(s,str) and s and len(s) <= 4096 for s in c["argv"])
                    or p.get("read") not in {"stdout", "exit_code"} or "question" in p):
                raise ValueError("implementation probe must read actual stdout or exit_code")
    for branch in q["branches"]:
        assigned = branch.get("action_ids", [])
        predictions = branch.get("predictions", {})
        if (not isinstance(assigned,list) or not all(_identifier(a) and a in actions for a in assigned)
                or len(set(assigned)) != len(assigned) or not isinstance(predictions,dict)
                or set(predictions) - probes):
            raise ValueError("branch references unknown actions or probes")
        for pid, values in predictions.items():
            if values is None:
                continue
            if not isinstance(values,list) or not 1 <= len(values) <= 32:
                raise ValueError("inquiry expects a bounded set of predicted values")
            if any(type(v) not in (int,str) for v in values):
                raise ValueError("predictions must be integer exit codes or text")
            if domain == "intent" and not set(values) <= choices[pid]:
                raise ValueError("intent prediction outside the owner's offered choices")
    return q


def next_action(inquiry, observations=(), candidate_sha=None):
    probes = [{**p,"domain":inquiry["domain"]} for p in inquiry["probes"]]
    branches = [{**b,"domain":inquiry["domain"]} for b in inquiry["branches"]]
    graph = {"goal":["decide"], "operators":[], "branches":branches, "probes":probes,
             "candidate_sha":candidate_sha}
    finding = solve(graph, observations)
    active = finding["branches"]
    trace = {"branches":active,"rejected_branches":finding["rejected_branches"]}
    if len(active) == 1:
        selected = next(b for b in branches if b["id"] == active[0])
        return {**trace,"kind":"resolved","branch_id":selected["id"],"action_ids":selected.get("action_ids",[])}
    if not active:
        return {**trace,"kind":"unresolved","reason":"all_predictions_contradicted"}
    if finding["conflicting_observations"]:
        return {**trace,"kind":"unresolved","reason":"conflicting_observations"}
    p = finding["probe"]
    if not p:
        return {**trace,"kind":"unresolved","reason":"no_discriminating_probe"}
    selection = {**trace,"probe_id":p["id"],"predictions":p["predictions"],
                 "guaranteed_eliminations":p["guaranteed_eliminations"]}
    if inquiry["domain"] == "intent":
        return {**selection,"kind":"ask","choices":p["choices"],"question":p["question"]}
    if not candidate_sha:
        return {**selection,"kind":"unresolved","reason":"implementation_probe_requires_a_candidate"}
    criteria = [p["criterion"]]
    return {**selection,"kind":"observe","criteria":criteria,"candidate_sha":candidate_sha,
            "criteria_digest":digest(criteria)}


def record_answer(inquiry, selected, value, event_id):
    if inquiry["domain"] != "intent" or selected.get("kind") != "ask" or not event_id:
        raise ValueError("only the owner may answer an outstanding intent question")
    p = next((p for p in inquiry["probes"] if p["id"] == selected["probe_id"]), None)
    if not p or value not in p["choices"]:
        raise ValueError("response must select an offered choice")
    return {"id":event_id,"source":"user","domain":"intent","probe_id":p["id"],"value":value}


def record_observation(inquiry, selected, receipt, event_id):
    if inquiry["domain"] != "implementation" or selected.get("kind") != "observe" or not event_id:
        raise ValueError("an implementation probe must precede a verifier observation")
    p = next((p for p in inquiry["probes"] if p["id"] == selected["probe_id"]), None)
    if (not p or receipt.get("candidate_sha") != selected["candidate_sha"] or receipt.get("kind") != "verify"
            or receipt.get("criteria_digest") != digest([p["criterion"]])):
        raise ValueError("observation does not belong to the selected version and probe")
    rows = receipt.get("checks", [])
    if len(rows) != 1 or rows[0].get("id") != p["criterion"]["id"] or rows[0].get("kind") != "command":
        raise ValueError("missing independent command observation")
    data = rows[0].get("observed") or {}
    if "execution_error" in data or not data.get("environment", {}).get("executor"):
        raise ValueError("command did not supply a measured environment and output")
    value = data.get(p["read"])
    if p["read"] == "stdout" and isinstance(value,str):
        value = value.rstrip("\r\n")
    elif p["read"] != "exit_code" or type(value) is not int:
        raise ValueError("missing or invalid observed value")
    return {"id":event_id,"source":"verifier","domain":"implementation","probe_id":p["id"],"value":value,
            "candidate_sha":selected["candidate_sha"], "environment_observed":data["environment"]}


def active_actions(actions, inquiry, resolution):
    if not inquiry:
        return actions
    if resolution.get("kind") != "resolved":
        raise ValueError("select a branch before committing its actions")
    selected = next((b for b in inquiry["branches"] if b["id"] == resolution.get("branch_id")), None)
    if not selected:
        raise ValueError("unknown selected branch")
    assigned = {a for b in inquiry["branches"] for a in b.get("action_ids", [])}
    retained = {a["id"] for a in actions if a["id"] not in assigned or a["id"] in selected.get("action_ids", [])}
    result = [a for a in actions if a["id"] in retained]
    if any(set(a.get("requires", [])) - retained for a in result):
        raise ValueError("selected actions require an operation only available in another interpretation")
    if not result:
        raise ValueError("selected branch has no deliverable action")
    return result
