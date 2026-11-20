"""Version-locked, local Copier components. Rendering returns proposed changes."""

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


DATA = Path(__file__).resolve().with_name("recipes_data")


def _sha(data):
    return sha256(data).hexdigest()


def _source_files(directory):
    from .host.git_workspace import safe_name
    paths = {}
    if directory.is_symlink() or directory.is_junction():
        raise ValueError("recipe directories must be real directories")
    for p in sorted(directory.rglob("*")):
        if p.is_symlink() or p.is_junction():
            raise ValueError("links are not allowed in recipe sources")
        if p.is_file():
            name = safe_name(p.relative_to(directory).as_posix())
            # Canonical text input is independent of Git autocrlf on Windows.
            paths[name] = p.read_text(encoding="utf-8").encode("utf-8")
    return paths


def _tree_digest(files):
    hashes = {name:_sha(blob) for name,blob in files.items()}
    return _sha(json.dumps(hashes,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode())


def catalog():
    """Read the package-controlled registry and reject drift under a retained version."""
    entries = json.loads((DATA/"catalog.json").read_text(encoding="utf-8"))
    for entry in entries:
        root = DATA/entry["id"]/entry["version"]
        if not root.resolve().is_relative_to(DATA.resolve()) or _tree_digest(_source_files(root)) != entry["digest"]:
            raise ValueError(f"registered recipe source changed: {entry['id']} {entry['version']}")
    return entries


def render(selected, workdir):
    from .host.git_workspace import safe_name
    if not isinstance(selected,list) or len(selected)>12:
        raise ValueError("expected at most 12 frozen recipe references")
    if not selected:
        return {"files":{},"receipts":[]}
    work = Path(workdir).resolve()
    work.mkdir(parents=True,exist_ok=True)
    lookup = {(c["id"],c["version"]):c for c in catalog()}
    files, receipts, names, seen = {}, [], set(), set()
    with tempfile.TemporaryDirectory(prefix="recipe-",dir=work,ignore_cleanup_errors=True) as temp:
        scratch = Path(temp)
        env = dict(os.environ)
        cache = scratch/"cache"
        cache.mkdir()
        env.update(TMP=str(cache),TEMP=str(cache),TMPDIR=str(cache),XDG_CACHE_HOME=str(cache),
                   XDG_CONFIG_HOME=str(cache),PYTHONDONTWRITEBYTECODE="1",GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL=os.devnull,GIT_TERMINAL_PROMPT="0",GIT_ALLOW_PROTOCOL="file")
        for i, pin in enumerate(selected):
            if not isinstance(pin,dict) or not all(isinstance(pin.get(k),str) for k in ("id","version","digest")):
                raise ValueError("incomplete frozen recipe reference")
            key = pin["id"],pin["version"]
            row = lookup.get(key)
            if not row or row["digest"] != pin["digest"] or key in seen:
                raise ValueError("unknown, changed or repeated recipe version")
            seen.add(key)
            # Snapshot local template bytes before validation; Copier only sees this private copy.
            source = _source_files(DATA/row["id"]/row["version"])
            if _tree_digest(source) != pin["digest"]:
                raise ValueError("recipe changed during snapshot")
            copy = scratch/f"source-{i}"
            dest = scratch/f"output-{i}"
            for name, blob in source.items():
                path = copy/name
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(blob)
            result = subprocess.run([sys.executable,"-B",__file__,"--copy",str(copy),str(dest),row["version"]],
                                    cwd=scratch,env=env,stdin=subprocess.DEVNULL,capture_output=True,timeout=45)
            if result.returncode:
                raise ValueError("Copier failed to render a verified local recipe: "
                                 + result.stderr.decode("utf-8","replace")[-900:])
            output = {}
            for path in sorted(dest.rglob("*")):
                if path.is_symlink() or path.is_junction():
                    raise ValueError("recipe produced a linked path")
                if not path.is_file():
                    continue
                name = safe_name(path.relative_to(dest).as_posix())
                target = work/name
                if (not target.resolve().is_relative_to(work) or target.is_symlink() or target.is_junction()
                        or any(p.is_symlink() or p.is_junction() or p.is_file() for p in target.parents
                               if p != work and p.is_relative_to(work))):
                    raise ValueError("output would traverse an existing link or file")
                if name.casefold() in names:
                    raise ValueError("multiple recipes claim " + name)
                blob = path.read_bytes()
                if target.exists() and (not target.is_file() or target.read_bytes()!=blob):
                    raise ValueError("recipe conflicts with existing project file: " + name)
                files[name] = blob.decode("utf-8")
                output[name] = _sha(blob)
                names.add(name.casefold())
            if not output:
                raise ValueError("empty recipe output")
            receipts.append({"id":key[0],"version":key[1],"digest":row["digest"],
                             "engine":"copier", "outputs":output})
    return {"files":files,"receipts":receipts}


if __name__ == "__main__" and len(sys.argv)==5 and sys.argv[1]=="--copy":
    from copier import run_copy
    run_copy(src_path=sys.argv[2],dst_path=sys.argv[3],data={"component_version":sys.argv[4]},
             defaults=True,skip_tasks=True,unsafe=False,overwrite=False,quiet=True)
