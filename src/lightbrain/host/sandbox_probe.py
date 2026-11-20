"""Trusted probe, copied read-only and invoked only inside the isolated namespace."""
import ctypes
import http.client
import json
import os
import platform
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import stat


def protect():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) or libc.prctl(38, 1, 0, 0, 0):
        raise RuntimeError("Unable to restrict probe process")
    # Keep socket creation inside the sandbox's INET and UNIX namespaces.
    lib = ctypes.CDLL("libseccomp.so.2")
    lib.seccomp_init.restype = ctypes.c_void_p
    ctx = ctypes.c_void_p(lib.seccomp_init(0x7FFF0000))
    if not ctx:
        raise RuntimeError("Cannot initialize sandbox policy")
    disallow = 0x00050000 | 1
    for name in (b"ptrace", b"process_vm_writev", b"process_vm_readv", b"pidfd_getfd",
                 b"bpf", b"perf_event_open", b"keyctl", b"add_key", b"request_key",
                 b"mount", b"umount2", b"setns", b"unshare", b"open_by_handle_at",
                 b"io_uring_setup", b"socketcall"):
        number = lib.seccomp_syscall_resolve_name(name)
        if number >= 0 and lib.seccomp_rule_add(ctx, disallow, number, 0):
            raise RuntimeError("Cannot load sandbox denial")
    class Rule(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_uint),
                    ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]
    for name in (b"socket", b"socketpair"):
        # Permit AF_UNIX (1), AF_INET (2) and AF_INET6 (10) only.
        nr = lib.seccomp_syscall_resolve_name(name)
        for lo, hi in ((0, 0), (3, 9), (11, 0xffffffff)):
            # libseccomp cannot compare the same operand twice in one rule.
            if lo == hi or hi < 10:
                for num in range(lo, hi + 1):
                    rule = Rule(0, 4, num, 0)
                    if lib.seccomp_rule_add_array(ctx, disallow, nr, 1, ctypes.byref(rule)):
                        raise RuntimeError("Cannot restrict socket family")
            else:
                rule = Rule(0, 5, lo, 0)  # SCMP_CMP_GE
                if lib.seccomp_rule_add_array(ctx, disallow, nr, 1, ctypes.byref(rule)):
                    raise RuntimeError("Cannot restrict socket family")
    if lib.seccomp_load(ctx):
        raise RuntimeError("Cannot apply sandbox policy")
    lib.seccomp_release(ctx)


def call_http(proc, criterion):
    port = int(criterion["port"])
    deadline = time.monotonic() + 7
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Service exited with code {proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=.2):
                break
        except OSError:
            time.sleep(.04)
    else:
        raise RuntimeError("Service did not listen on the expected port")
    observed = []
    for request in criterion["requests"]:
        req = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        path = request["path"]
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Expect a local absolute request path")
        body = request.get("body")
        headers = dict(request.get("headers", {}))
        if "json" in request:
            body = json.dumps(request["json"], ensure_ascii=False).encode()
            headers.setdefault("Content-Type", "application/json")
        try:
            req.request(request.get("method", "GET"), path, body=body, headers=headers)
            resp = req.getresponse()
            raw = resp.read(131073)
            if len(raw) > 131072:
                raise ValueError("Response exceeds observation limit")
            observed.append({"path": path, "status": resp.status, "body": raw.decode("utf-8", "replace")})
        finally:
            req.close()
    return observed


def export_edits(root):
    """Only a developer command can request portable text edits from its tmpfs."""
    root = Path(root)
    original = Path("/input")
    written, seen = {}, set()
    used = 0
    for base, dirs, files in os.walk(root):
        for d in dirs:
            if (Path(base)/d).is_symlink():
                raise ValueError("Cannot export a linked project directory")
        dirs[:] = [d for d in dirs if d not in {".git","__pycache__"}]
        for name in files:
            f = Path(base)/name
            if f.is_symlink() or not stat.S_ISREG(f.lstat().st_mode):
                raise ValueError("Cannot export a linked or non-regular project file")
            rel=f.relative_to(root).as_posix()
            seen.add(rel)
            baseline=original/rel
            if f.stat().st_size > 4*1024*1024:
                raise ValueError("A developer command produced an oversized file")
            data=f.read_bytes()
            if baseline.is_file() and data==baseline.read_bytes():
                continue
            used+=len(data)
            if used > 4*1024*1024 or len(written)>=128:
                raise ValueError("Too many command edits to import")
            written[rel]=data.decode("utf-8")
    deleted=[p.relative_to(original).as_posix() for p in original.rglob("*") if p.is_file()
             and p.relative_to(original).as_posix() not in seen and "__pycache__" not in p.parts]
    if len(written)+len(deleted)>128:
        raise ValueError("Command change count exceeds budget")
    return {"files":written,"delete":deleted}


def main():
    criterion = json.load(sys.stdin)
    protect()
    directory = tempfile.mkdtemp(dir="/tmp")
    shutil.copytree("/input", directory, dirs_exist_ok=True)
    argv = criterion["argv"].copy()
    if argv[0] == "python":
        argv[0] = "python3"
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr, tempfile.TemporaryFile() as stdin:
        stdin.write(criterion.get("stdin", "").encode())
        stdin.seek(0)
        with subprocess.Popen(argv, cwd=directory, env={"PATH":"/usr/bin:/bin","HOME":"/tmp",
                                "LANG":"C.UTF-8","PYTHONDONTWRITEBYTECODE":"1",
                                "PORT":str(criterion.get("port", 8000))},
                              stdin=stdin, stdout=stdout, stderr=stderr) as proc:
            observed = {}
            try:
                if criterion["kind"] == "http":
                    observed["responses"] = call_http(proc, criterion)
                else:
                    observed["exit_code"] = proc.wait(timeout=12)
                    if criterion.get("export_edits") and observed["exit_code"] == 0:
                        observed["edits"] = export_edits(directory)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                observed["execution_error"] = str(exc)
            finally:
                if proc.poll() is None:
                    proc.kill()
        stdout.seek(0)
        stderr.seek(0)
        observed.update(stdout=stdout.read(32768).decode("utf-8","replace"),
                        stderr=stderr.read(8192).decode("utf-8","replace"),
                        interpreter=sys.version.split()[0], platform=platform.platform())
        print(json.dumps(observed, ensure_ascii=True))


if __name__ == "__main__":
    main()
