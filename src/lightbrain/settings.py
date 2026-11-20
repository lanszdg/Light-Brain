from pathlib import Path
import os
import secrets
from contextlib import contextmanager


ROOT = Path(__file__).resolve().parents[2]


def worker_key(token):
    import hmac
    return hmac.new(token.encode(),b"lightbrain-worker-v1","sha256").hexdigest()


def runtime_home():
    return Path(os.environ.get("LB_HOME", ROOT / ".lb" / "state")).resolve()


@contextmanager
def controller_lock(home):
    """The OS releases this lock on crash. The file itself can survive a reboot."""
    file=(Path(home)/"controller.lock").open("a+b")
    try:
        if file.tell()==0:
            file.write(b"1");file.flush()
        file.seek(0)
        try:
            if os.name=="nt":
                import msvcrt
                msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("该状态目录已有主服务在运行") from exc
        yield
    finally:
        file.close()


def configure_temp(home):
    import tempfile
    directory = Path(home) / "tmp"
    directory.mkdir(parents=True, exist_ok=True)
    for key in ("TMP", "TEMP", "TMPDIR"):
        os.environ[key] = str(directory)
    tempfile.tempdir = str(directory)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def auth_token(home):
    if os.environ.get("LB_TOKEN"):
        return os.environ["LB_TOKEN"]
    path = Path(home) / "access.token"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Publish a complete file even if the service and worker start together.
    import uuid
    tmp = path.with_suffix("." + uuid.uuid4().hex)
    if not path.exists():
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(secrets.token_urlsafe(36))
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
        finally:
            tmp.unlink(missing_ok=True)
    return path.read_text(encoding="ascii").strip()


def model_settings():
    values = {}
    profile = os.environ.get("LB_MODEL_ENV")
    if profile:
        for line in Path(profile).read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    def get(name):
        return os.getenv("LB_" + name) or values.get(name) or os.getenv(name)
    return get("MODEL_BASE_URL"), get("MODEL_API_KEY"), get("MODEL_NAME")
