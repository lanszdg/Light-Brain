"""Build a fresh unprivileged Linux sandbox for each independent observation."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request


ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = ROOT / ".runtime" / "tools" / "bubblewrap"
PACKAGE = "bubblewrap_0.9.0-1ubuntu0.3_amd64.deb"
PACKAGE_HASH = "2461f1beee9cb04c8942739fe1a2b37e7b7c2a3d518f0779dc75f9245baa3094"


def linux_path(path):
    p = Path(path).resolve()
    if os.name != "nt":
        return str(p)
    if len(p.drive) != 2 or p.drive[1] != ":":
        raise ValueError("Worker files must be on a local drive")
    return "/mnt/" + p.drive[0].lower() + "/" + "/".join(p.parts[1:])


def linux_command(context):
    if os.name == "nt":
        return ["wsl.exe", "--distribution", context.get("distribution", "Ubuntu"), "--exec"]
    return []


def install_local(context=None):
    """Install into this checkout, using the Ubuntu package's recorded digest."""
    context = context or {}
    TOOL_DIR.mkdir(parents=True, exist_ok=True)
    dest = TOOL_DIR / "package.deb"
    if not dest.is_file() or hashlib.sha256(dest.read_bytes()).hexdigest() != PACKAGE_HASH:
        for mirror in ("https://mirrors.edge.kernel.org/ubuntu", "https://security.ubuntu.com/ubuntu"):
            try:
                with urllib.request.urlopen(mirror + "/pool/main/b/bubblewrap/" + PACKAGE, timeout=20) as response:
                    content = response.read(100000)
                if hashlib.sha256(content).hexdigest() != PACKAGE_HASH:
                    raise ValueError("Unexpected bubblewrap package digest")
                dest.write_bytes(content)
                break
            except OSError:
                if "security.ubuntu.com" in mirror:
                    raise
    subprocess.run(linux_command(context) + ["dpkg-deb", "-x", linux_path(dest), linux_path(TOOL_DIR)],
                   cwd=TOOL_DIR, check=True, timeout=30)


def observe(source, criterion, context):
    helper = Path(__file__).with_name("sandbox_probe.py")
    conf = json.dumps(criterion, ensure_ascii=False).encode("utf-8")
    image = context.get("verify_image")
    if image:
        if not re.fullmatch(r"[\w./:-]+@sha256:[a-f0-9]{64}", image):
            raise ValueError("Container image must use a digest")
        cmd = linux_command(context) + [
            "docker", "run", "--rm", "--pull=never", "--network=none", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--read-only", "--memory=512m", "--cpus=1",
            "--pids-limit=64", "--tmpfs", "/tmp:size=64m", "--user", "65534:65534", "-i",
            "-v", linux_path(source) + ":/input:ro", "-v", linux_path(helper) + ":/verify.py:ro",
            image, "timeout", "-k", "1", "24", "python3", "-I", "-B", "/verify.py"]
        flavor = "docker"
    else:
        installed = TOOL_DIR / "usr/bin/bwrap"
        binary = linux_path(installed) if installed.is_file() else (shutil.which("bwrap") if os.name != "nt" else "/usr/bin/bwrap")
        if not binary:
            raise RuntimeError("Bubblewrap unavailable; install distro bubblewrap or configure a digest-pinned Docker image")
        cmd = linux_command(context) + [
            "/usr/bin/timeout", "-k", "1", "24", "/usr/bin/prlimit", "--core=0", "--fsize=8388608",
            "--cpu=18", "--nofile=96", "--as=1610612736", "--nproc=256", "--",
            binary, "--unshare-all", "--unshare-user", "--disable-userns", "--new-session",
            "--die-with-parent", "--clearenv", "--cap-drop", "ALL",
            "--ro-bind", "/usr/bin", "/usr/bin", "--ro-bind", "/usr/lib", "/usr/lib",
            "--ro-bind-try", "/usr/lib64", "/usr/lib64", "--ro-bind-try", "/usr/share/nodejs", "/usr/share/nodejs",
            "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--proc", "/proc", "--dev", "/dev", "--size", str(64*1024*1024), "--tmpfs", "/tmp",
            "--ro-bind", linux_path(source), "/input", "--ro-bind", linux_path(helper), "/verify.py",
            "--setenv", "LANG", "C.UTF-8", "--setenv", "PATH", "/usr/bin:/bin",
            "--setenv", "HOME", "/tmp", "--chdir", "/tmp", "/usr/bin/python3", "-I", "-B", "/verify.py"]
        flavor = "bubblewrap"
    run = subprocess.run(cmd, input=conf, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=source, timeout=30)
    if run.returncode:
        detail = run.stderr.replace(b"\0", b"").decode("utf-8", "replace")[-900:]
        raise RuntimeError(f"Sandbox {flavor} failed ({run.returncode}): {detail}")
    result = json.loads(run.stdout)
    result["environment"] = {"executor": flavor, "image": image,
                             "interpreter": result.pop("interpreter"), "platform": result.pop("platform"),
                             "probe_sha256": hashlib.sha256(helper.read_bytes()).hexdigest()}
    return result


if __name__ == "__main__" and sys.argv[1:] == ["install-bubblewrap"]:
    install_local()
