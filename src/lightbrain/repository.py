from pathlib import Path, PurePosixPath
import subprocess
import tempfile


def git(cwd, *args, binary=False):
    result = subprocess.run(["git", "-c", "core.hooksPath=NUL" if __import__("os").name=="nt" else "core.hooksPath=/dev/null",
                             "--no-optional-locks", *map(str, args)], cwd=cwd, capture_output=True, timeout=90)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8","replace")[-1200:])
    return result.stdout if binary else result.stdout.decode("utf-8","replace").strip()


def baseline(store, case_id, path):
    repo = Path(path).resolve(strict=True)
    if not repo.is_dir() or Path(git(repo,"rev-parse","--show-toplevel")).resolve() != repo:
        raise ValueError("请选择 Git 仓库根目录")
    sha = git(repo,"rev-parse","HEAD")
    with tempfile.TemporaryDirectory(dir=store.root) as temp:
        if git(repo,"status","--porcelain"):
            from .host import git_workspace as vcs
            copy=vcs.checkout(temp,repo,sha)
            vcs.materialize_dirty(copy,repo)
            snapshot=vcs.export_bundle(copy,temp)
            return {"candidate_sha":snapshot["candidate_sha"],"bundle_id":store.ingest_file(snapshot["bundle"],case_id)["sha256"]}
        bundle = Path(temp) / "source.bundle"
        git(repo,"bundle","create",bundle,"HEAD")
        return {"candidate_sha":sha, "bundle_id":store.ingest_file(bundle,case_id)["sha256"]}


def source_context(bundle, sha, directory):
    """Only committed source and repository instructions, capped before sending to the configured model."""
    repo = Path(directory) / "read.git"
    git(directory,"init","--bare",repo)
    git(repo,"-c","protocol.file.allow=always","fetch","--no-tags",Path(bundle).resolve(),"HEAD")
    if git(repo,"rev-parse","FETCH_HEAD") != sha:
        raise ValueError("source transfer SHA mismatch")
    source = {}
    used = 0
    files = git(repo,"ls-tree","-r","--name-only","-z",sha,binary=True).decode("utf-8","replace").split("\0")
    def rank(name):
        return (Path(name).name not in {"AGENTS.md","README.md","app.py","index.html","package.json","pyproject.toml"}, name)
    for name in sorted(filter(None,files), key=rank):
        p = PurePosixPath(name)
        if (p.suffix.lower() not in {".py",".md",".ts",".tsx",".js",".jsx",".json",".html",".css",".toml",".sql",".txt"}
            or any(part.startswith(".") or part in {"dist","node_modules","vendor","build","__pycache__"} for part in p.parts)
            or any(k in name.lower() for k in ("credential","secret","package-lock","token.json","key.json"))):
            continue
        if len(source) >= 65 or used >= 96000:
            break
        if int(git(repo,"cat-file","-s",sha+":"+name)) > 200000:
            continue
        content=git(repo,"show",sha+":"+name,binary=True).decode("utf-8","replace")[:min(24000,96000-used)]
        source[name]=content
        used+=len(content)
    return source


def validate_candidate(store,case,bundle_id,head,base):
    import re
    if not isinstance(head,str) or not re.fullmatch("[a-f0-9]{40}|[a-f0-9]{64}",head):
        raise ValueError("候选源码缺少完整 Git SHA")
    bundle=store.read_path(bundle_id,case)
    with tempfile.TemporaryDirectory(prefix="inspect-",dir=store.root) as temp:
        git(temp,"init","--bare","--template=")
        git(temp,"bundle","verify",bundle)
        git(temp,"-c","protocol.file.allow=always","fetch","--no-tags",bundle,"HEAD")
        if git(temp,"rev-parse","FETCH_HEAD^{commit}")!=head:
            raise ValueError("候选提交与独立读取到的源码版本不符")
        if base:
            git(temp,"merge-base","--is-ancestor",base,head)
