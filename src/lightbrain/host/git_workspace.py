"""Private Git checkouts, portable bundles and content-addressed source identity."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_name(path):
    if not isinstance(path, str) or not path or len(path) > 500 or "\\" in path:
        raise ValueError("Invalid portable relative path")
    parts = path.split("/")
    for part in parts:
        if (not part or part in {".", ".."} or part.lower() == ".git"
            or part.rstrip(". ") != part
            or re.match(r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", part)
            or re.match(r"(?i)^git~[0-9]", part)
            or any(ord(c) < 32 or c in '<>:"|?*' for c in part)):
            raise ValueError(f"Unsafe project path {path!r}")
    return path


def git(cwd, *args, binary=False):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="file",
               GIT_AUTHOR_NAME="Light-brain", GIT_AUTHOR_EMAIL="lb@localhost",
               GIT_COMMITTER_NAME="Light-brain", GIT_COMMITTER_EMAIL="lb@localhost")
    argv = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.autocrlf=false",
            "-c", "core.protectNTFS=true", "-c", "core.fsmonitor=false", *args]
    r = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=120, check=False)
    if r.returncode:
        raise ValueError("Git " + str(args[0]) + ": " + r.stderr.decode("utf-8","replace")[-1200:])
    return r.stdout if binary else r.stdout.decode("utf-8").strip()


def directory(root, prefix):
    path = Path(root).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=path))


def check_tree(repo, sha):
    seen = set()
    for item in git(repo, "ls-tree", "-rz", sha, binary=True).split(b"\0"):
        if not item:
            continue
        meta, name = item.split(b"\t", 1)
        key = safe_name(name.decode("utf-8")).casefold()
        if meta.split()[0] not in {b"100644", b"100755"} or key in seen:
            raise ValueError("Unsupported link, submodule or colliding project path")
        seen.add(key)


def checkout(root, source, sha=None):
    """A bundle or an explicitly authorized existing local repository."""
    repo = directory(root, "checkout-")
    if not source:
        git(repo, "init", "--initial-branch=main", "--template=")
        git(repo, "commit", "--allow-empty", "-m", "Initialize project")
        return repo
    src = Path(source).resolve(strict=True)
    git(repo, "clone", "--template=", "--no-local", "--no-checkout", str(src), ".")
    if src.is_file() and sha:
        heads = git(repo, "bundle", "list-heads", str(src), "HEAD").split()
        if heads != [sha, "HEAD"]:
            raise ValueError("Bundle HEAD does not match the requested candidate")
    if sha:
        if not re.fullmatch("[0-9a-f]{40}|[0-9a-f]{64}", str(sha)):
            raise ValueError("Expected a complete Git commit ID")
    else:
        sha = git(repo, "rev-parse", "HEAD")
    check_tree(repo, sha)
    git(repo, "checkout", "--detach", sha)
    if git(repo, "rev-parse", "HEAD") != sha:
        raise ValueError("Candidate commit mismatch")
    return repo


def materialize_dirty(repo, original):
    """Copy the supplied user's working changes into a private baseline."""
    names = git(original, "ls-files", "--modified", "--deleted", "--others",
                "--exclude-standard", "-z", binary=True)
    names += git(original, "diff", "--cached", "--no-ext-diff", "--name-only",
                 "--no-renames", "-z", binary=True)
    for raw in set(names.split(b"\0")) - {b""}:
        rel = safe_name(raw.decode("utf-8"))
        origin, target = Path(original) / rel, repo / rel
        if origin.is_symlink() or not origin.resolve().is_relative_to(Path(original).resolve()):
            raise ValueError("Working tree contains a link outside the authorized source tree")
        if origin.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, target)
        elif not origin.exists() and target.is_file():
            target.unlink()
    if names:
        git(repo, "add", "--all")
        if git(repo, "diff", "--cached", "--name-only"):
            git(repo, "commit", "-m", "Snapshot supplied working changes")


def write_changes(repo, files, deletions):
    for rel in deletions:
        target = repo / safe_name(rel)
        if target.is_file():
            target.unlink()
    for rel, content in files.items():
        target = repo / safe_name(rel)
        if not isinstance(content, str):
            raise ValueError("Generated file content must be text")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))


def check_create_only(repo, files):
    existing = {}
    for base,dirs,names in os.walk(repo):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in [*dirs,*names]:
            rel = (Path(base)/name).relative_to(repo).as_posix()
            existing[rel.casefold()] = rel
    added = {}
    for rel in files:
        safe_name(rel)
        key = rel.casefold()
        if key in existing or key in added:
            raise ValueError(f"Create-only method conflicts with existing path {rel!r}")
        # Reject a differently cased parent directory and file-as-directory conflicts.
        for parent in Path(rel).parents:
            folded = parent.as_posix().casefold()
            if folded in added and added[folded] != ("dir",parent.as_posix()):
                raise ValueError("A method would use a file or a different spelling for a directory")
            if folded in existing and (existing[folded]!=parent.as_posix() or not (repo/existing[folded]).is_dir()):
                raise ValueError("A method would overwrite a parent or collide with its casing")
            added[folded] = ("dir",parent.as_posix())
        added[key] = ("file",rel)


def check_protected(repo, protected):
    for rel, expected in protected.items():
        file = repo / safe_name(rel)
        if not file.is_file() or file.is_symlink() or digest(file) != expected:
            raise ValueError(f"Protected source changed: {rel}")


def export_bundle(repo, artifacts):
    git(repo, "add", "--all")
    if git(repo, "diff", "--cached", "--name-only"):
        git(repo, "commit", "-m", "Implement commissioned change")
    sha = git(repo, "rev-parse", "HEAD")
    check_tree(repo, sha)
    git(repo, "branch", "-f", "lb-candidate", sha)
    git(repo, "symbolic-ref", "HEAD", "refs/heads/lb-candidate")
    output = directory(artifacts, "candidate-") / "source.bundle"
    git(repo, "bundle", "create", str(output), "HEAD", "refs/heads/lb-candidate")
    git(repo, "bundle", "verify", str(output))
    return {"candidate_sha": sha, "bundle": str(output), "bundle_sha256": digest(output)}


def clean_source(repo, dest):
    for raw in git(repo, "ls-files", "-z", binary=True).split(b"\0"):
        if raw:
            rel = safe_name(raw.decode("utf-8"))
            target = Path(dest) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo / rel, target)


def archive_source(repo, output):
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for raw in git(repo, "ls-files", "-z", binary=True).split(b"\0"):
            if raw:
                rel = safe_name(raw.decode("utf-8"))
                archive.write(repo / rel, rel)
