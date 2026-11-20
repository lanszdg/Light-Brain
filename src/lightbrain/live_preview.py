"""Authenticated loopback HTTP preview backed by a network-isolated application."""

import base64
from contextlib import suppress
from http.client import HTTPConnection, HTTPException
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit


MAX_BODY = 4 * 1024 * 1024
MAX_FRAME = 2 * MAX_BODY
LIMIT_HEADERS = 96
SUPPORTED = {"GET","HEAD","POST","PUT","PATCH","DELETE","OPTIONS"}
FORWARD = {"accept","accept-language","content-type","range","if-none-match","if-modified-since","cookie"}
RESPONSE = {"content-type","cache-control","etag","last-modified","content-encoding","accept-ranges","content-range",
            "location","set-cookie","content-language","vary"}
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; base-uri 'none'; "
       "form-action 'self'; frame-src 'none'; frame-ancestors 'none'; object-src 'none'; "
       "worker-src 'none'; sandbox allow-same-origin allow-scripts allow-forms")


def _line(out, msg):
    out.write(json.dumps(msg, ensure_ascii=True,separators=(",",":")).encode() + b"\n")
    out.flush()


def _target_path(path):
    if not isinstance(path,str) or len(path)>8192 or not path.startswith("/") or path.startswith("//"):
        raise ValueError("expected a local resource path")
    if any(ord(ch)<32 or ch=="\\" for ch in path):
        raise ValueError("invalid resource path")
    return path


def _bridge():
    """Runs inside the mount/PID/network namespace. Only stdin/stdout speak this protocol."""
    cfg=json.loads(sys.stdin.buffer.readline(65537))
    port=cfg["port"]
    shutil.copytree("/input","/tmp/app")
    app=subprocess.Popen(cfg["argv"],cwd="/tmp/app",stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True,
                         env={"LANG":"C.UTF-8","PATH":"/usr/bin:/bin","HOME":"/tmp","TMPDIR":"/tmp",
                              "HOST":"127.0.0.1","PORT":str(port),"PYTHONDONTWRITEBYTECODE":"1"})
    try:
        deadline=time.monotonic()+12
        while app.poll() is None and time.monotonic()<deadline:
            try:
                with socket.create_connection(("127.0.0.1",port),timeout=.3):
                    break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError("HTTP entrypoint failed to listen on the verified port")
        _line(sys.stdout.buffer,{"ready":True})
        while True:
            line=sys.stdin.buffer.readline(MAX_FRAME+1)
            if not line or len(line)>MAX_FRAME:
                break
            conn=HTTPConnection("127.0.0.1",port,timeout=8)
            try:
                req=json.loads(line)
                if req["method"] not in SUPPORTED:
                    raise ValueError("unsupported request method")
                data=base64.b64decode(req["body"],validate=True)
                if len(data)>MAX_BODY:
                    raise ValueError("request is too large")
                conn.request(req["method"],_target_path(req["path"]),body=data,headers=req["headers"])
                reply=conn.getresponse()
                raw=reply.read(MAX_BODY+1)
                if len(raw)>MAX_BODY or len(reply.getheaders())>LIMIT_HEADERS:
                    raise ValueError("response is too large")
                msg={"status":reply.status,"headers":reply.getheaders(),"body":base64.b64encode(raw).decode()}
            except (OSError,ValueError,KeyError,TypeError,HTTPException) as exc:
                msg={"error":type(exc).__name__}
            finally:
                conn.close()
            _line(sys.stdout.buffer,{**msg,"request_id":req["request_id"]})
    finally:
        app.terminate()
        with suppress(subprocess.TimeoutExpired):
            app.wait(2)
        if app.poll() is None:
            app.kill()


def _spawn(source, rule, life):
    from .host.sandbox import TOOL_DIR, linux_command, linux_path
    local=TOOL_DIR/"usr/bin/bwrap"
    binary=linux_path(local) if local.is_file() else "/usr/bin/bwrap"
    args=linux_command({})+["/usr/bin/timeout","-k","2",str(life+15),"/usr/bin/prlimit",
          "--core=0","--fsize=16777216","--nofile=128","--as=2147483648","--nproc=256","--",
          binary,"--unshare-all","--unshare-user","--disable-userns","--new-session","--die-with-parent",
          "--clearenv","--cap-drop","ALL","--ro-bind","/usr/bin","/usr/bin","--ro-bind","/usr/lib","/usr/lib",
          "--ro-bind-try","/usr/lib64","/usr/lib64","--ro-bind-try","/usr/share/nodejs","/usr/share/nodejs",
          "--symlink","usr/bin","/bin","--symlink","usr/lib","/lib","--symlink","usr/lib64","/lib64",
          "--proc","/proc","--dev","/dev","--size",str(96*1024*1024),"--tmpfs","/tmp",
          "--ro-bind",linux_path(source),"/input","--ro-bind",linux_path(__file__),"/bridge.py",
          "--setenv","PATH","/usr/bin:/bin","--setenv","LANG","C.UTF-8","--chdir","/tmp",
          "/usr/bin/python3","-I","-B","/bridge.py","--bridge"]
    proc=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                          creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    return proc


class _Session:
    def __init__(self, store, case, ttl):
        from .host.runner import extract_export
        from .contracts import digest
        delivery=case.get("delivery") or {}
        if case.get("state")!="delivered" or not delivery.get("archive_id"):
            raise ValueError("preview requires an accepted standalone delivery")
        export=store.get_job(delivery["export_job"])
        receipt=export.get("receipt") or {}
        sha=delivery["candidate_sha"]
        archive=delivery["archive_id"]
        if (export["case_id"]!=case["id"] or export["status"]!="finished" or export["kind"]!="verify_export"
                or receipt.get("ok") is not True or receipt.get("archive_sha256")!=archive
                or receipt.get("candidate_sha")!=sha or receipt.get("criteria_digest")!=digest(case["criteria"])):
            raise ValueError("preview requires an independent matching export receipt")
        passed={r["id"] for r in receipt.get("checks", []) if r.get("kind")=="http" and r.get("ok") is True}
        rules=[r for r in case["criteria"] if r["id"] in passed and r["kind"]=="http"]
        if not rules:
            raise ValueError("delivery has no independently verified HTTP entrypoint")
        rule=next((r for r in rules if r["argv"]==delivery.get("entrypoint")), rules[0])
        self.case_id=case["id"]
        self.sha=sha
        self.deadline=time.monotonic()+ttl
        self.closed=False
        self.guard=threading.Lock()
        self.io=threading.Lock()
        self.frames=queue.Queue(maxsize=2)
        self.invitations={}
        self.cookie=secrets.token_urlsafe(32)
        self.key="lb_preview_"+secrets.token_hex(8)
        self.proc=None
        self.server=None
        self.serving=False
        self.space=tempfile.TemporaryDirectory(prefix="live-",dir=store.root,ignore_cleanup_errors=True)
        try:
            path=store.read_path(archive,self.case_id)
            source,_=extract_export({"archive":path,"archive_sha256":archive,"candidate_sha":sha},Path(self.space.name))
            self.proc=_spawn(source,rule,ttl)
            threading.Thread(target=self._read,daemon=True).start()
            _line(self.proc.stdin,{"port":rule["port"],"argv":rule["argv"]})
            if not self.frames.get(timeout=18).get("ready"):
                raise ValueError("isolated preview entrypoint could not start")
            self._bind()
            self.deadline=time.monotonic()+ttl
            threading.Thread(target=self.server.serve_forever,daemon=True).start()
            self.serving=True
            self.timer=threading.Timer(ttl,self.close)
            self.timer.daemon=True
            self.timer.start()
        except BaseException:
            self.close()
            raise

    def _read(self):
        output=self.proc.stdout
        try:
            while not self.closed:
                line=output.readline(MAX_FRAME+1)
                if not line or len(line)>MAX_FRAME:
                    break
                self.frames.put(json.loads(line),timeout=1)
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            with suppress(queue.Full):
                self.frames.put_nowait({"error":"sandbox exited"})

    def _bind(self):
        session=self
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(12)

            def log_message(self,*args):
                pass

            def do_GET(self):
                session.http(self)

            do_POST=do_HEAD=do_PATCH=do_PUT=do_DELETE=do_OPTIONS=do_GET
        # A different loopback IP per preview keeps the creator's main-service cookies
        # outside this origin and prevents apps from seeing another preview's cookies.
        ip="127."+ ".".join(str(secrets.randbelow(240)+7) for _ in range(3))
        self.server=ThreadingHTTPServer((ip,0),Handler)
        self.server.daemon_threads=True
        self.host=f"{ip}:{self.server.server_port}"

    def info(self):
        with self.guard:
            if self.closed or self.proc.poll() is not None or time.monotonic()>=self.deadline:
                return {"running":False}
            now=time.monotonic()
            self.invitations={k:t for k,t in self.invitations.items() if t>now}
            if len(self.invitations)>=32:
                self.invitations.pop(next(iter(self.invitations)))
            invite=secrets.token_urlsafe(32)
            self.invitations[invite]=min(now+120,self.deadline)
            return {"running":True,"case_id":self.case_id,"candidate_sha":self.sha,
                    "expires_in":max(0,int(self.deadline-now)), "url":"http://"+self.host+"/_lb_session/"+invite}

    def http(self,h):
        if (self.closed or h.headers.get("Host")!=self.host or time.monotonic()>=self.deadline
                or h.headers.get("Origin") not in (None,"http://"+self.host)
                or h.headers.get("Sec-Fetch-Site")=="cross-site" and not h.path.startswith("/_lb_session/")):
            return h.send_error(403)
        if h.path.startswith("/_lb_session/"):
            key=h.path.removeprefix("/_lb_session/")
            with self.guard:
                expiry=self.invitations.pop(key,0)
            if h.command!="GET" or expiry<=time.monotonic():
                return h.send_error(403)
            external=h.headers.get("Sec-Fetch-Site")=="cross-site"
            h.send_response(200 if external else 303)
            h.send_header("Set-Cookie",f"{self.key}={self.cookie}; Path=/; HttpOnly; SameSite=Strict")
            if external:
                h.send_header("Content-Type","text/html; charset=utf-8")
                h.send_header("Content-Security-Policy","default-src 'none'; script-src 'unsafe-inline'; frame-ancestors 'none'")
            else:
                h.send_header("Location","/")
            h.send_header("Cache-Control","no-store")
            h.send_header("Referrer-Policy","no-referrer")
            h.end_headers()
            if external:
                h.wfile.write(b'<!doctype html><script>location.replace("/")</script><a href="/">Open preview</a>')
            return
        cookies=SimpleCookie()
        with suppress(Exception):
            cookies.load(h.headers.get("Cookie",""))
        given=cookies.pop(self.key,None)
        if not given or not secrets.compare_digest(given.value,self.cookie):
            return h.send_error(403)
        try:
            path=_target_path(h.path)
            length=int(h.headers.get("Content-Length",0))
            if not 0<=length<=MAX_BODY or h.headers.get("Transfer-Encoding"):
                return h.send_error(413)
            if h.command not in ("GET","HEAD","OPTIONS") and h.headers.get("Content-Type","").lower().startswith("multipart/"):
                return h.send_error(415,"File uploads are disabled in preview")
            body=h.rfile.read(length)
            headers={k:v for k,v in h.headers.items() if k.lower() in FORWARD and k.lower()!="cookie"}
            if cookies:
                headers["Cookie"]="; ".join(f"{k}={c.coded_value}" for k,c in cookies.items())
            with self.io:
                if self.closed or self.proc.poll() is not None:
                    return h.send_error(410)
                request_id=secrets.token_hex(12)
                _line(self.proc.stdin,{"request_id":request_id,"method":h.command,"path":path,"headers":headers,
                                      "body":base64.b64encode(body).decode()})
                limit=time.monotonic()+12
                while True:
                    reply=self.frames.get(timeout=max(0,limit-time.monotonic()))
                    if reply.get("request_id")==request_id or reply.get("error")=="sandbox exited":
                        break
            if "error" in reply:
                return h.send_error(502,"Preview did not return an HTTP response")
            data=base64.b64decode(reply["body"],validate=True)
            safe=[]
            for k,v in reply["headers"]:
                if k.lower() not in RESPONSE or "\r" in v or "\n" in v:
                    continue
                if k.lower()=="location":
                    loc=urlsplit(v)
                    if loc.scheme or loc.netloc or "\\" in v:
                        return h.send_error(502,"External redirect disabled")
                if k.lower()=="set-cookie" and (v.startswith(self.key+"=") or "domain=" in v.lower()):
                    continue
                safe.append((k,v))
            h.send_response(reply["status"])
            for k,v in safe:
                h.send_header(k,v)
            h.send_header("Content-Length",str(len(data)))
            h.send_header("Content-Security-Policy",CSP)
            h.send_header("Referrer-Policy","no-referrer")
            h.send_header("X-Content-Type-Options","nosniff")
            h.send_header("X-DNS-Prefetch-Control","off")
            h.end_headers()
            if h.command!="HEAD":
                h.wfile.write(data)
        except (OSError,ValueError,TypeError,queue.Empty):
            with suppress(OSError):
                h.send_error(502)

    def close(self):
        with self.guard:
            if self.closed:
                return
            self.closed=True
        if hasattr(self,"timer"):
            self.timer.cancel()
        if self.server:
            if self.serving:
                self.server.shutdown()
            self.server.server_close()
        if self.proc:
            with suppress(OSError):
                self.proc.stdin.close()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
            with suppress(OSError):
                self.proc.stdout.close()
        self.space.cleanup()


class PreviewManager:
    def __init__(self, store):
        self.store=store
        self.sessions={}
        self.lock=threading.Lock()

    def start(self, case_id, ttl=600, archive_id=None):
        if type(ttl) is not int or not 1<=ttl<=1800:
            raise ValueError("preview lifetime must be between 1 and 1800 seconds")
        with self.lock:
            case=self.store.get_case(case_id)
            key=case_id
            if archive_id is not None:
                if not isinstance(archive_id,str) or len(archive_id)!=64:
                    raise ValueError("preview requires a valid archived release")
                history=(row["body"].get("patch",{}).get("delivery") or {}
                         for row in reversed(self.store.list_events(case_id)) if row["event_type"]=="delivered")
                delivery=next((entry for entry in history if entry.get("archive_id")==archive_id and entry.get("export_job")),None)
                if not delivery:
                    raise ValueError("preview requires an accepted release in this case")
                job=self.store.get_job(delivery["export_job"])
                case={**case,"state":"delivered","delivery":delivery,"criteria":job["payload"]["criteria"]}
                key=case_id+":"+archive_id
            current=self.sessions.get(key)
            if current:
                info=current.info()
                if info["running"] and case.get("state")=="delivered" and info["candidate_sha"]==case["delivery"]["candidate_sha"]:
                    return info
                current.close()
            self.sessions={k:v for k,v in self.sessions.items() if not v.closed}
            if len(self.sessions)>=8:
                raise ValueError("eight live previews are already running")
            session=_Session(self.store,case,ttl)
            self.sessions[key]=session
            return session.info()

    def status(self, case_id):
        with self.lock:
            session=self.sessions.get(case_id)
            return session.info() if session else {"running":False}

    def stop(self, case_id):
        with self.lock:
            session=self.sessions.pop(case_id,None)
        if session:
            session.close()
        return {"running":False}

    def close(self):
        with self.lock:
            sessions=list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            session.close()


if __name__=="__main__" and sys.argv[1:]==["--bridge"]:
    _bridge()
