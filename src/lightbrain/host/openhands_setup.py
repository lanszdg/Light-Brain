"""Install the locked Linux worker in the current Light-brain project."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


def file_sha256(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f,"sha256").hexdigest()


def install_archive(archive,runtime,root):
    spec = json.loads((root/"工程依赖/openhands-runtime.json").read_text(encoding="utf-8"))
    if file_sha256(root/"工程依赖/worker-linux.lock.txt") != spec["lock_sha256"]:
        raise ValueError("OpenHands archive was built for a different dependency lock")
    if archive.stat().st_size != spec["compressed_bytes"] or file_sha256(archive) != spec["compressed_sha256"]:
        raise ValueError("OpenHands compressed archive digest mismatch")
    prefix = ["wsl.exe","--distribution","Ubuntu","--exec"] if os.name == "nt" else []
    platform = subprocess.check_output(prefix+["/usr/bin/python3","-I","-c",
        "import platform,sys;print(platform.system().lower(),platform.machine(),sys.version_info.major,sys.version_info.minor)"],
        text=True, timeout=20).strip().split()
    if platform != [spec["platform"],spec["architecture"],*spec["python"].split(".")]:
        raise RuntimeError("OpenHands archive requires Linux x86_64 and Python 3.12")
    runtime.mkdir(parents=True,exist_ok=True)
    target = runtime/"sdk-runtime.tar"
    if target.is_file() and file_sha256(target) == spec["tar_sha256"]:
        return
    with tempfile.NamedTemporaryFile(dir=runtime,delete=False) as f:
        tmp=Path(f.name)
    try:
        h = hashlib.sha256()
        length = 0
        with gzip.open(archive,"rb") as src, tmp.open("wb") as dst:
            while chunk := src.read(1024*1024):
                length += len(chunk)
                if length > spec["tar_bytes"]:
                    raise ValueError("OpenHands archive exceeds expected size")
                h.update(chunk)
                dst.write(chunk)
        if length != spec["tar_bytes"] or h.hexdigest() != spec["tar_sha256"]:
            raise ValueError("OpenHands unpacked runtime digest mismatch")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def pack_runtime(runtime):
    """Pack site-packages once to avoid thousands of WSL-to-NTFS reads per session."""
    src = runtime/"venv/lib/python3.12/site-packages"
    with tempfile.NamedTemporaryFile(dir=runtime,delete=False) as tmp:
        archive = Path(tmp.name)
    try:
        with tarfile.open(archive,"w") as tar:
            for path in src.rglob("*"):
                if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
                    tar.add(path,arcname=path.relative_to(src).as_posix(),recursive=False)
        archive.replace(runtime/"sdk-runtime.tar")
    finally:
        archive.unlink(missing_ok=True)


def install():
    root = Path(__file__).resolve().parents[3]
    online_target = sys.argv[2] if len(sys.argv)==3 and sys.argv[1]=="--online-target" else None
    runtime = Path(online_target or os.environ.get("LB_OPENHANDS_RUNTIME",root/".runtime/openhands")).resolve()
    if os.environ.get("LB_OPENHANDS_ARCHIVE"):
        selected = Path(os.environ["LB_OPENHANDS_ARCHIVE"]).resolve(strict=True)
    else:
        name = "Light-brain-OpenHands-linux-x64.tar.gz"
        selected = next((folder/name for folder in (root,root.parent,root/"交付验证")
                         if (folder/name).is_file()),None)
    if selected and not online_target:
        install_archive(selected,runtime,root)
        return
    if os.name == "nt":
        from .sandbox import linux_path
        subprocess.run(["wsl.exe","--distribution","Ubuntu","--exec","/usr/bin/python3","-I","-B",
                        linux_path(__file__),"--online-target",linux_path(runtime)],check=True,cwd=root)
        pack_runtime(runtime)
        return
    for folder in ("home","tmp","cache","uvtool"):
        (runtime/folder).mkdir(parents=True,exist_ok=True)
    env = {**os.environ, "HOME":str(runtime/"home"), "TMPDIR":str(runtime/"tmp"),
           "XDG_CACHE_HOME":str(runtime/"cache"),"UV_CACHE_DIR":str(runtime/"cache/uv"),
           "PYTHONDONTWRITEBYTECODE":"1", "PIP_DISABLE_PIP_VERSION_CHECK":"1",
           "UV_PYTHON_DOWNLOADS":"never", "UV_LINK_MODE":"copy"}
    uv = runtime/"uvtool/bin/uv"
    if not uv.is_file():
        subprocess.run([sys.executable,"-m","pip","install","--no-cache-dir","--target",
                        runtime/"uvtool","--index-url","https://pypi.org/simple","uv==0.13.5"],
                       check=True,env=env)
    python = runtime/"venv/bin/python"
    if not python.is_file():
        subprocess.run([uv,"venv","--python",sys.executable,runtime/"venv"],env=env,check=True)
    subprocess.run([uv,"pip","sync","--python",python,"--require-hashes",
                    "--index-url","https://pypi.org/simple",root/"工程依赖/worker-linux.lock.txt"],
                   env=env,check=True)


if __name__ == "__main__":
    install()
