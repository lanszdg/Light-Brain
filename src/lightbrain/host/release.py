"""Deployment-provider receipts. The simulator maintains real local release history."""
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile

from .git_workspace import digest


def release(payload,context):
    mode = context.get("release_mode", "simulate")
    if mode not in {"simulate","github-draft"}:
        raise ValueError("Release provider is disabled")
    archive = Path(payload["archive"]).resolve(strict=True)
    sha, artifact = payload["candidate_sha"], payload["archive_sha256"]
    if digest(archive) != artifact:
        raise ValueError("Release file differs from accepted artifact")
    with zipfile.ZipFile(archive) as z:
        if json.loads(z.comment)["candidate_sha"] != sha:
            raise ValueError("Release commit differs from build manifest")
    if mode == "github-draft":
        return github_draft(archive, sha, artifact, context)
    root = Path(context.get("release_store", Path(context["artifacts"]) / "simulated-channel"))
    root.mkdir(parents=True, exist_ok=True)
    blob = root / (artifact + ".zip")
    if not blob.exists():
        with tempfile.NamedTemporaryFile(dir=root,delete=False) as fp, archive.open("rb") as src:
            shutil.copyfileobj(src,fp)
            incoming = Path(fp.name)
        incoming.replace(blob)
    if digest(blob) != artifact:
        raise ValueError("Staged release digest mismatch")
    channel = context.get("channel", context["case_id"])
    key = payload.get("operation_id") or f"{channel}/{sha}/{artifact}"
    with sqlite3.connect(root / "deployments.sqlite") as db:
        db.execute("CREATE TABLE IF NOT EXISTS deploys(seq INTEGER PRIMARY KEY, channel TEXT, op TEXT UNIQUE, sha TEXT, digest TEXT)")
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT OR IGNORE INTO deploys(channel,op,sha,digest) VALUES(?,?,?,?)",(channel,key,sha,artifact))
        row = db.execute("SELECT seq,channel,sha,digest FROM deploys WHERE op=?",(key,)).fetchone()
        if row[1:] != (channel,sha,artifact):
            raise ValueError("Release operation key already belongs to another payload")
        active = db.execute("SELECT seq FROM deploys WHERE channel=? ORDER BY seq DESC LIMIT 1",(channel,)).fetchone()[0]
    return {"mode":"simulate","candidate_sha":sha,"archive_sha256":artifact,"artifact":str(blob),
            "deployment_id":row[0],"active":row[0] == active,"channel":channel,"public":False}


def github_draft(archive,sha,artifact,context):
    approval = context.get("approved_release",{})
    repo = approval.get("repository","")
    if (not re.fullmatch(r"[\w.-]+/[\w.-]+",repo) or
        approval.get("candidate_sha") != sha or approval.get("archive_sha256") != artifact):
        raise ValueError("Worker needs an exact repository, commit and archive approval")
    tag = "lb-" + sha + "-" + artifact[:12]
    def gh(*args, optional=False):
        proc = subprocess.run(["gh",*args],capture_output=True,text=True,encoding="utf-8",timeout=90)
        if proc.returncode:
            if optional and ("404" in proc.stderr or "release not found" in proc.stderr.lower()):
                return None
            raise RuntimeError("GitHub operation did not return a confirmed receipt")
        return proc.stdout.strip()
    if gh("api",f"repos/{repo}/commits/{sha}","--jq",".sha") != sha:
        raise ValueError("Commit not available in approved repository")
    def read():
        data=gh("release","view",tag,"-R",repo,"--json","assets,isDraft,url,targetCommitish",optional=True)
        return None if data is None else json.loads(data)
    info = read()
    if info is None:
        gh("release","create",tag,str(archive),"-R",repo,"--target",sha,
           "--draft","--title",tag,"--notes","Light-brain verified source export.")
        info = read()
    if not info or not info["isDraft"] or info["targetCommitish"] != sha:
        raise ValueError("GitHub draft receipt does not match requested state")
    if not any(a.get("digest") == "sha256:"+artifact for a in info["assets"]):
        raise ValueError("GitHub asset has not returned the expected digest")
    return {"mode":"github-draft","candidate_sha":sha,"archive_sha256":artifact,
            "url":info["url"],"tag":tag,"public":False}
