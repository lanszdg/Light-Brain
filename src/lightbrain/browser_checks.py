"""Drive an actual Chromium renderer against a virtual, network-isolated static origin."""

import hashlib
import mimetypes
import os
from pathlib import Path, PurePosixPath
import platform
import tempfile
import time
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4


ORIGIN = "https://lb-preview.invalid"
ACTIONS = {"fill", "click", "press", "select", "reload",
           "expect_text", "expect_value", "expect_visible", "expect_count"}


def relative_file(name):
    if not isinstance(name, str) or not name or len(name) > 1024:
        raise ValueError("invalid browser asset path")
    parts = name.split("/")
    if any(p in {"", ".", ".."} or p.startswith(".") or p.endswith((" ", "."))
           or any(ord(c) < 32 or c in '\\:<>"|?*' for c in p) for p in parts):
        raise ValueError("invalid browser asset path")
    return PurePosixPath(*parts)


def validate_criterion(rule):
    relative_file(rule.get("path", "index.html"))
    size = rule.get("viewport", {"width": 1280, "height": 800})
    if not isinstance(size, dict) or set(size) != {"width", "height"} or any(
            type(n) is not int or not 240 <= n <= 2400 for n in size.values()):
        raise ValueError("browser viewport needs width and height from 240 to 2400")
    steps = rule.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 40:
        raise ValueError("browser check requires 1 to 40 steps")
    assertions = 0
    for step in steps:
        if not isinstance(step, dict) or step.get("action") not in ACTIONS:
            raise ValueError("unknown browser action")
        action = step["action"]
        selector = step.get("selector")
        if action != "reload" and (not isinstance(selector, str) or not 1 <= len(selector) <= 512
                                   or ">>" in selector):
            raise ValueError("browser step requires one CSS selector")
        value = step.get("value")
        if action == "expect_count" and (type(value) is not int or not 0 <= value <= 1000):
            raise ValueError("expect_count requires an integer")
        if action in {"expect_text", "expect_value", "fill", "select", "press"} and (
                not isinstance(value, str) or len(value) > 2048):
            raise ValueError("browser step value must be a short string")
        if action in {"expect_text", "press"} and not value:
            raise ValueError("empty browser expectation or key")
        assertions += action.startswith("expect_")
    if not assertions:
        raise ValueError("browser check needs an observable assertion")
    return rule


def _browser_path():
    configured = os.environ.get("LB_BROWSER_EXECUTABLE")
    if configured:
        return str(Path(configured).resolve(strict=True))
    from shutil import which
    paths = [which("chromium"), which("google-chrome"), which("chromium-browser")]
    for base in ("ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(base):
            root = Path(os.environ[base])
            paths += [root / "Google/Chrome/Application/chrome.exe",
                      root / "Microsoft/Edge/Application/msedge.exe"]
    return next((str(p) for p in paths if p and Path(p).is_file()), None)


def _apply(page, step, expect, timeout):
    command = step["action"]
    if command == "reload":
        page.reload(wait_until="domcontentloaded", timeout=timeout)
        return {}
    loc = page.locator("css=" + step["selector"])
    value = step.get("value")
    if command == "expect_count":
        expect(loc).to_have_count(value, timeout=timeout)
        return {"count": loc.count()}
    if command.startswith("expect_"):
        expect(loc).to_be_visible(timeout=timeout)
        if command == "expect_text":
            expect(loc).to_contain_text(value, use_inner_text=True, timeout=timeout)
            return {"text": loc.inner_text(timeout=timeout)[:2048]}
        if command == "expect_value":
            expect(loc).to_have_value(value, timeout=timeout)
            return {"value": loc.input_value(timeout=timeout)[:2048]}
        return {"visible": True}
    if command == "click":
        loc.click(timeout=timeout)
    elif command == "fill":
        loc.fill(value, timeout=timeout)
    elif command == "press":
        loc.press(value, timeout=timeout)
    else:
        loc.select_option(value, timeout=timeout)
    return {}


def run_browser(source, criterion, context):
    from playwright.sync_api import Error, expect, sync_playwright
    validate_criterion(criterion)
    root = Path(source).resolve(strict=True)
    entry = str(relative_file(criterion.get("path", "index.html")))
    if not (root / entry).is_file():
        raise ValueError("browser entry is absent from this candidate")
    viewport = criterion.get("viewport", {"width": 1280, "height": 800})
    output = Path(context["artifacts"]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    work = Path(context["workdir"]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    screenshot = output / f"browser-{uuid4().hex}.png"
    obs = {"entry": entry, "steps": [], "viewport": viewport, "blocked_requests": 0}
    blocked = {"count": 0}
    env = dict(os.environ)
    for key in list(env):
        if any(s in key.upper() for s in ("TOKEN", "API_KEY", "PASSWORD", "SECRET", "MODEL_ENV")):
            del env[key]
    env.update(TEMP=str(work), TMP=str(work), TMPDIR=str(work), XDG_CACHE_HOME=str(work / "cache"))
    with tempfile.TemporaryDirectory(dir=work, prefix="chromium-", ignore_cleanup_errors=True) as profile:
        with sync_playwright() as pw:
            browser = pw.chromium.launch_persistent_context(
                profile, executable_path=_browser_path(), headless=True, chromium_sandbox=True,
                viewport=viewport, env=env, locale="zh-CN", accept_downloads=False,
                downloads_path=str(work / "downloads"), service_workers="block",
                proxy={"server": "http://127.0.0.1:9", "bypass": "<-loopback>"},
                args=["--disable-background-networking", "--disable-breakpad", "--disable-crash-reporter",
                      "--disable-extensions", "--disable-default-apps", "--disable-sync",
                      "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                      "--host-resolver-rules=MAP * ~NOTFOUND"],
            )
            try:
                obs["environment"] = {"executor": "playwright-chromium", "version": browser.browser.version,
                                      "platform": platform.system(), "renderer_sandbox": True,
                                      "source": "virtual-static-origin"}
                def route(request):
                    url = urlsplit(request.request.url)
                    if url.scheme != "https" or url.netloc != "lb-preview.invalid" or request.request.method != "GET":
                        blocked["count"] += 1
                        return request.abort()
                    try:
                        path = (root / relative_file(unquote(url.path[1:] or "index.html"))).resolve(strict=True)
                        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 16*1024*1024:
                            raise ValueError()
                        body = path.read_bytes()
                    except (OSError, ValueError):
                        return request.fulfill(status=404, body=b"")
                    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    request.fulfill(body=body, content_type=mime, headers={
                        "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; "
                        "frame-src 'none'; object-src 'none'; worker-src 'none'; form-action 'none'; base-uri 'none'",
                        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
                browser.route("**/*", route)
                browser.route_web_socket("**/*", lambda ws: ws.close())
                page = browser.pages[0]
                page.on("popup", lambda p: p.close())
                ok = False
                try:
                    page.goto(ORIGIN + "/" + quote(entry), wait_until="domcontentloaded", timeout=8000)
                    deadline = time.monotonic() + 22
                    for step in criterion["steps"]:
                        ms = min(3500, int(1000 * (deadline - time.monotonic())))
                        if ms <= 0:
                            raise ValueError("browser step budget exhausted")
                        detail = {"action": step["action"], "selector": step.get("selector")}
                        obs["steps"].append(detail)
                        detail.update(_apply(page, step, expect, ms), ok=True)
                    ok = True
                except (Error, AssertionError, ValueError) as exc:
                    obs["failure"] = str(exc)[:900]
                obs["blocked_requests"] = blocked["count"]
                page.screenshot(path=str(screenshot), timeout=5000, animations="disabled")
            finally:
                browser.close()
    obs["screenshot_path"] = str(screenshot)
    obs["screenshot_sha256"] = hashlib.sha256(screenshot.read_bytes()).hexdigest()
    return ok, obs
