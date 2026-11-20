"""Worker handlers. Only the controller may convert these receipts into decisions."""
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

from . import git_workspace as vcs
from .sandbox import observe


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def criteria_digest(criteria):
    return hashlib.sha256(canonical(criteria)).hexdigest()


def pointer(data, path):
    if not path:
        return data
    if not path.startswith("/"):
        raise ValueError("Expected a JSON Pointer")
    for part in path[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        data = data[int(key)] if isinstance(data, list) else data[key]
    return data


def expect_body(rule, body):
    if "contains" in rule and rule["contains"] not in body:
        return False
    if "json_equals" in rule:
        return canonical(pointer(json.loads(body), rule.get("pointer"))) == canonical(rule["json_equals"])
    return True


def check(criterion, source, context):
    kind = criterion["kind"]
    if kind == "browser":
        from ..browser_checks import run_browser
        try:
            return run_browser(source, criterion, context)
        except Exception as exc:
            raise RuntimeError(f"Browser acceptance failed: {exc}") from exc
    if kind in {"file", "json"}:
        path = source / vcs.safe_name(criterion["path"])
        if not path.is_file():
            return False, {"exists": False, "environment": {"executor": "static"}}
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("File exceeds the verification read limit")
        observation = {"sha256": vcs.digest(path), "environment": {"executor": "static"}}
        if kind == "json":
            value = pointer(json.loads(path.read_bytes()), criterion.get("pointer"))
            observation["value"] = value
            return canonical(value) == canonical(criterion["equals"]), observation
        return expect_body(criterion, path.read_text(encoding="utf-8")), observation
    if kind not in {"command", "http"}:
        raise ValueError(f"Unsupported criterion kind {kind}")
    argv = criterion.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(x,str) and "\x00" not in x for x in argv):
        raise ValueError("Criterion requires a nonempty argument array")
    if kind == "http" and not (isinstance(criterion.get("requests"),list) and 0 < len(criterion["requests"]) <= 32):
        raise ValueError("HTTP criterion requires 1 to 32 requests")
    # Keep expected assertions on the trusted side of the process boundary.
    request = {k:criterion[k] for k in ("kind","argv","stdin","port") if k in criterion}
    if kind == "http":
        request["requests"] = [{k:v for k,v in row.items() if k in {"method","path","headers","body","json"}}
                               for row in criterion["requests"]]
    actual = observe(source, request, context)
    if "execution_error" in actual:
        return False, actual
    if kind == "command":
        passed = actual["exit_code"] == criterion.get("expect_exit", 0)
        if "stdout_contains" in criterion:
            passed &= criterion["stdout_contains"] in actual["stdout"]
        if "stdout_equals" in criterion:
            passed &= criterion["stdout_equals"] == actual["stdout"]
        if "json_equals" in criterion:
            passed &= expect_body(criterion, actual["stdout"])
        return passed, actual
    responses = actual["responses"]
    passed = len(responses) == len(criterion["requests"]) and all(
        response["status"] == rule.get("expect_status", rule.get("status",200))
        and expect_body(rule, response["body"]) for response,rule in zip(responses,criterion["requests"]))
    return passed, actual


def verify(source, payload, context):
    criteria = payload["criteria"]
    if not isinstance(criteria,list) or not criteria or len(criteria) > 128:
        raise ValueError("Verification requires 1 to 128 frozen criteria")
    digest = criteria_digest(criteria)
    if payload.get("criteria_digest", digest) != digest:
        raise ValueError("Acceptance criteria digest mismatch")
    identifiers = [row.get("id") for row in criteria]
    if any(not isinstance(i,str) or not i for i in identifiers) or len(set(identifiers)) != len(criteria):
        raise ValueError("Criteria must have distinct nonempty IDs")
    checks = []
    environments = []
    for rule in criteria:
        try:
            passed, observed = check(rule, source, context)
            checks.append({"id":rule["id"], "kind":rule["kind"], "ok":bool(passed), "observed":observed})
            environment = observed.get("environment")
            if environment and environment not in environments:
                environments.append(environment)
        except (OSError, ValueError, KeyError, TypeError, IndexError, RuntimeError, subprocess.TimeoutExpired) as exc:
            checks.append({"id":rule["id"], "kind":rule.get("kind"), "ok":False, "error":str(exc)[:1500]})
    return {"ok":all(row["ok"] for row in checks), "checks":checks,
            "criteria_digest":digest, "environment":environments[0] if len(environments)==1 else {"all":environments}}


def extract_export(payload, root):
    archive = Path(payload["archive"])
    digest = vcs.digest(archive)
    if digest != payload["archive_sha256"]:
        raise ValueError("Export archive digest mismatch")
    target = vcs.directory(root, "export-")
    with zipfile.ZipFile(archive) as z:
        if json.loads(z.comment)["candidate_sha"] != payload["candidate_sha"]:
            raise ValueError("Export manifest candidate mismatch")
        items = z.infolist()
        if len(items) > 50000 or sum(x.file_size for x in items) > 512*1024*1024:
            raise ValueError("Export exceeds configured extraction limit")
        seen = set()
        for item in items:
            path = vcs.safe_name(item.filename)
            if path.casefold() in seen:
                raise ValueError("Duplicate export entry")
            seen.add(path.casefold())
            dest = target / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(item))
    return target, digest


def perform(kind,payload,context):
    work, artifacts = context["workdir"], context["artifacts"]
    if kind == "release":
        from .release import release
        return release(payload,context)
    if kind == "verify_export":
        source, digest = extract_export(payload, work)
        return {"candidate_sha":payload["candidate_sha"], "archive_sha256":digest,
                **verify(source,payload,context)}
    if kind == "develop":
        baseline = payload.get("baseline")
        repo = vcs.checkout(work,payload.get("bundle") or baseline,payload.get("candidate_sha") or payload.get("base_sha"))
        if baseline and not payload.get("bundle") and not payload.get("base_sha"):
            vcs.materialize_dirty(repo, baseline)
        base_sha = vcs.git(repo, "rev-parse", "HEAD")
        if payload.get("create_only"):
            if payload.get("delete") or payload.get("engine") == "openhands":
                raise ValueError("Create-only methods accept additions only")
            vcs.check_create_only(repo,payload.get("files",{}))
        vcs.check_protected(repo,payload.get("protected_files",{}))
        vcs.write_changes(repo,payload.get("files",{}),payload.get("delete",[]))
        engine = {"executor":"materialize"}
        if payload.get("engine") == "openhands":
            from .openhands import run_session
            repo, engine = run_session(repo,payload["task"],context,selected=payload.get("model"))
        vcs.check_protected(repo,payload.get("protected_files",{}))
        return {"base_sha":base_sha, "development":engine, **vcs.export_bundle(repo,artifacts)}
    if kind not in {"verify", "build", "preview"}:
        raise ValueError(f"Unknown worker operation {kind!r}")
    bundle = Path(payload["bundle"]).resolve(strict=True)
    if not bundle.is_file():
        raise ValueError("Expected a candidate bundle file")
    digest = vcs.digest(bundle)
    if payload.get("bundle_sha256", digest) != digest:
        raise ValueError("Bundle digest mismatch")
    sha = payload["candidate_sha"]
    repo = vcs.checkout(work,bundle,sha)
    common = {"candidate_sha":sha,"bundle_sha256":digest}
    if kind == "build":
        archive = vcs.directory(artifacts,"build-") / "source.zip"
        vcs.archive_source(repo,archive)
        with zipfile.ZipFile(archive,"a") as z:
            z.comment = canonical({"candidate_sha":sha,"bundle_sha256":digest})
        return {**common,"archive":str(archive),"archive_sha256":vcs.digest(archive),"entrypoint":payload.get("entrypoint")}
    source = vcs.directory(artifacts if kind=="preview" else work, "preview-" if kind=="preview" else "source-")
    vcs.clean_source(repo,source)
    if kind == "preview":
        entry = vcs.safe_name(payload.get("entry","index.html"))
        if not (source / entry).is_file():
            raise ValueError(f"The project does not expose {entry!r} as a static preview")
        return {**common, "preview_dir":str(source), "entry":entry}
    return {**common, **verify(source,payload,context)}


def handle(kind,payload,context):
    try:
        return {"kind":kind,"ok":True,**perform(kind,payload,context)}
    except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError,
            ImportError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        return {"kind":kind,"ok":False,"error":str(exc)[:1600]}
