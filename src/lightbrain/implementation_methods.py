"""Acquire and reuse independently verified, self-contained Python CLI implementations."""

import ast
from hashlib import file_digest, sha256
import json
from pathlib import PurePosixPath
import zipfile

from .core import qualify_method
from .host.git_workspace import safe_name
from .learning import _behaviour, _boundary, _entry, _grounded, _positive, _rebind, checksum
from .store import NotFound


# This allowlist defines the portable single-file profile. Execution still takes
# place inside the independent sandbox under normal project permissions.
LIBRARIES = {"math","decimal","fractions","statistics","itertools","functools","operator",
             "re","json","collections","datetime","string","unicodedata","typing","sys","argparse"}
DENIED = {"open","exec","eval","compile","inputhook","globals","locals","vars","getattr","setattr",
          "delattr","breakpoint","help","exit","quit","__import__"}
SYS_ATTRS = {"argv","stdin","stdout","stderr","exit"}
PROFILE = "python-stdlib-stdin-argv/v1"


def _portable(source):
    if not source or len(source) > 128 * 1024:
        return False
    try:
        tree = ast.parse(source)
    except (TypeError, SyntaxError, ValueError, RecursionError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(n.name not in LIBRARIES or (n.name == "sys" and n.asname) for n in node.names):
                return False
        elif isinstance(node, ast.ImportFrom):
            if (node.level or node.module not in LIBRARIES or any(n.name == "*" or n.name.startswith("_") for n in node.names)
                    or node.module == "sys" and any(n.name not in SYS_ATTRS for n in node.names)):
                return False
        elif isinstance(node, ast.Name):
            if node.id in DENIED or (node.id.startswith("__") and node.id != "__name__"):
                return False
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr in {"FileType","read_text","write_text","fromfile_prefix_chars"}:
                return False
            if isinstance(node.value, ast.Name) and node.value.id == "sys" and node.attr not in SYS_ATTRS:
                return False
        elif isinstance(node, ast.keyword) and node.arg == "fromfile_prefix_chars":
            return False
    return True


def _stamp(case):
    delivery = case.get("delivery") or {}
    if case.get("state") != "delivered" or not delivery.get("archive_id") or not delivery.get("export_job"):
        return None
    return {"case_id":case["id"], "export_job":delivery["export_job"], "archive_id":delivery["archive_id"],
            "candidate_sha":delivery["candidate_sha"], "goal_version":case.get("goal_version",1),
            "criteria_digest":checksum(case["criteria"])}


def _archive(store, stamp):
    if not stamp:
        return None
    job = store.get_job(stamp["export_job"])
    receipt = job.get("receipt") or {}
    archive = stamp["archive_id"]
    if (job["kind"] != "verify_export" or job["case_id"] != stamp["case_id"] or job["status"] != "finished"
            or job["payload"].get("goal_version") != stamp["goal_version"]
            or job["payload"].get("archive_id") != archive or receipt.get("ok") is not True
            or receipt.get("archive_sha256") != archive or receipt.get("candidate_sha") != stamp["candidate_sha"]
            or receipt.get("criteria_digest") != stamp["criteria_digest"]):
        return None
    path = store.read_path(archive, stamp["case_id"])
    with path.open("rb") as stream:
        if file_digest(stream,"sha256").hexdigest() != archive:
            raise ValueError("verified export bytes no longer match the receipt")
    return path


def _source(store, stamp, rel):
    archive = _archive(store, stamp)
    if not archive:
        return None
    rel = safe_name(rel)
    if PurePosixPath(rel).suffix != ".py":
        return None
    with zipfile.ZipFile(archive) as z:
        if json.loads(z.comment).get("candidate_sha") != stamp["candidate_sha"]:
            raise ValueError("export source identity does not match the accepted candidate")
        candidates = [item for item in z.infolist() if item.filename == rel]
        if len(candidates) != 1 or candidates[0].file_size > 128*1024:
            return None
        data = z.read(candidates[0])
    try:
        text = data.decode("utf8")
    except UnicodeError:
        return None
    if not _portable(text):
        return None
    return text, sha256(data).hexdigest()


def _saved_stamp(store, method):
    if method.get("source_export"):
        return method["source_export"]
    # Recover the original source version for a previously catalogued asset.
    for job in store.list_jobs(method["source_case"]):
        p = job["payload"]
        if job["kind"] == "verify_export" and p.get("archive_id") == method["archive_id"]:
            return {"case_id":method["source_case"], "export_job":job["id"], "archive_id":p["archive_id"],
                    "candidate_sha":p["candidate_sha"], "criteria_digest":p["criteria_digest"],
                    "goal_version":p["goal_version"]}
    return None


def _domain(source):
    outcome = _boundary(source)
    invalid = "from local_dependency import calculate\nprint(calculate(input()))"
    if _portable(invalid):
        raise ValueError("portability guard failed to reject a missing module")
    return {**outcome, "profile":PROFILE,"invalid_source_hash":sha256(invalid.encode()).hexdigest()}


def learn_implementations(store, case):
    c = store.get_case(case["id"] if isinstance(case,dict) else case)
    if not c.get("delivery") or not c["delivery"].get("archive_id"):
        return []
    learned = {}
    for criterion, job, actual in _grounded(store, c):
        if (_entry(criterion)[:2] != ["command","python"] or not actual["ok"]
                or job["receipt"].get("ok") is not True or job["payload"]["candidate_sha"] != c["delivery"]["candidate_sha"]):
            continue
        filename = criterion["argv"][1]
        stamp = _stamp(c)
        code = _source(store, stamp, filename)
        if not code:
            continue
        body_hash = code[1]
        mid = "impl-" + checksum([PROFILE, body_hash, _behaviour(criterion)])[:24]
        history = store.method_history(c["brain_id"], c["project_id"], mid)
        if history:
            if history[-1]["status"] == "active":
                learned[mid] = history[-1]
            continue
        suffix = mid[5:]
        path_variable, content_variable = "target_" + suffix, "source_" + suffix
        operator = {"id":mid, "job_kind":"reuse_implementation", "requires":["implementation_granted:" + mid],
                    "provides":["implemented:" + mid], "parameters":[path_variable, content_variable],
                    "payload":{"method_id":mid,"source_sha256":body_hash,"target_path":"${" + path_variable + "}",
                               "source_text":"${" + content_variable + "}","create_only":True}}
        replays = [_positive(c,job,criterion), _domain(criterion)]
        method = qualify_method({"id":mid, "revision":1, "scope":{"brain_id":c["brain_id"],"project_id":c["project_id"]},
                                 "asset_kind":"implementation","profile":PROFILE,"source_case":c["id"],"source_path":filename,
                                 "archive_id":stamp["archive_id"], "source_export":stamp, "content_sha256":body_hash,
                                 "description":"Verified standalone Python CLI implementation: " + criterion["id"],
                                 "source_criterion":criterion,"operator":operator,"replays":replays}, replays)
        learned[mid] = store.register_method(c["brain_id"],c["project_id"],method)
    return list(learned.values())


def _resolve(store, case, ref):
    brain, project = case["brain_id"],case["project_id"]
    if "/" in ref:
        project, mid = ref.rsplit("/",1)
        if not project:
            return None
        try:
            return store.get_method(brain,project,mid)
        except NotFound:
            return None
    candidates = [m for m in store.list_methods(brain,project,True) if m["id"] == ref]
    return max(candidates, key=lambda m:len(m["scope"]), default=None)


def bind_implementations(store, case, criteria, actions=()):
    """Only the immutable owner's original request may grant source transfer to this case."""
    case = store.get_case(case["id"])
    sources, facts, goals, bindings = {}, [], [], {}
    for ref in case.get("request", {}).get("reuse_methods") or []:
        if not isinstance(ref,str):
            continue
        m = _resolve(store,case,ref)
        if not m or m.get("asset_kind") != "implementation" or m["status"] != "active" or m["id"] in sources:
            continue
        mid = m["id"]
        matched = [c for c in criteria if _rebind(m["source_criterion"],c) is not None
                   and _behaviour(m["source_criterion"]) == _behaviour(c)]
        destinations = {safe_name(c["argv"][1]) for c in matched}
        if len(destinations) != 1:
            continue
        asset = _source(store,_saved_stamp(store,m),m["source_path"])
        if not asset or asset[1] != m["content_sha256"]:
            raise ValueError("implementation no longer matches its independently verified export")
        pkey, skey = m["operator"]["parameters"]
        if any(bindings[k] == next(iter(destinations)) for k in bindings if k.startswith("target_")):
            raise ValueError("two implementations cannot create the same target path")
        bindings[pkey] = next(iter(destinations))
        bindings[skey] = asset[0]
        # A named source-project grant is usable in this case only. No catalogue write.
        sources[mid] = {**m, "source_scope":m["scope"], "scope":{"brain_id":case["brain_id"],"project_id":case["project_id"]}}
        facts.append("implementation_granted:" + mid)
        goals.append("implemented:" + mid)
    return list(sources.values()),facts,goals,bindings


def materialize(payload):
    """Build a create-only file map from an authorised, digest-bound operation."""
    path = safe_name(payload["target_path"])
    source = payload["source_text"]
    if (payload.get("create_only") is not True or not path.endswith(".py") or not _portable(source)
            or sha256(source.encode()).hexdigest() != payload["source_sha256"]):
        raise ValueError("invalid executable asset payload")
    return {path:source}


def promote_implementation(store, brain, source_project, method_id, other_case_id):
    method = store.get_method(brain,source_project,method_id)
    if method.get("asset_kind") != "implementation" or method["status"] != "active":
        raise ValueError("implementation is not active")
    other = store.get_case(other_case_id)
    if other["brain_id"] != brain or other["project_id"] == source_project or not other.get("delivery"):
        raise ValueError("implementation promotion requires a second delivered project")
    for criterion, job, actual in _grounded(store, other):
        if (not actual["ok"] or not job["receipt"]["ok"] or not _entry(criterion)
                or job["payload"]["candidate_sha"] != other["delivery"]["candidate_sha"]
                or _behaviour(criterion) != _behaviour(method["source_criterion"])):
            continue
        code = _source(store,_stamp(other),criterion["argv"][1])
        if not code or code[1] != method["content_sha256"]:
            continue
        positive = _positive(other,job,criterion)
        first = next(r for r in method["replays"] if r["case"] == "positive")
        if first["candidate_sha"] == positive["candidate_sha"]:
            raise ValueError("two distinct accepted candidate commits are required")
        previous = store.method_history(brain,None,method_id)
        if previous and previous[-1]["status"] == "active":
            return previous[-1]
        replays = [first,positive,_domain(method["source_criterion"])]
        qualified = qualify_method({**method,"scope":{"brain_id":brain},"replays":replays,
                                    "revision":previous[-1]["revision"]+1 if previous else 1},replays)
        return store.register_method(brain,None,qualified)
    raise ValueError("second project's accepted export does not demonstrate this implementation and behaviour")
