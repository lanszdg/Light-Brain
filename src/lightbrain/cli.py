"""Command-line client for a loopback Light-brain service."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request
from uuid import uuid4


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_url(value):
    u = urlsplit(value)
    if u.scheme != "http" or not u.hostname or u.username or u.password or u.path not in ("","/") or u.query or u.fragment:
        raise ValueError("--url requires a local HTTP origin")
    if u.hostname != "localhost" and not ipaddress.ip_address(u.hostname).is_loopback:
        raise ValueError("--url must use a loopback address")
    if u.port == 0:
        raise ValueError("port 0 is unavailable for a persistent service")
    return value.rstrip("/")


class Client:
    def __init__(self, url, token):
        self.url = local_url(url)
        self.token = token
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(self, path, data=None):
        body = json.dumps(data,ensure_ascii=False).encode("utf-8") if data is not None else None
        req = Request(self.url + path, data=body, headers={
            "Content-Type":"application/json", "Authorization":"Bearer " + self.token})
        return self.opener.open(req, timeout=60)

    def json(self, path, data=None):
        with self.request(path,data) as res:
            return json.load(res)


def print_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def watch_parent():
    """Close the attached worker even when the control service was killed."""
    pid = os.environ.pop("LB_ATTACHED_PARENT", None)
    if pid is None:
        return
    pid = int(pid)

    def monitor():
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            process = kernel.OpenProcess(0x00100000, False, pid)
            if process:
                kernel.WaitForSingleObject(process, 0xFFFFFFFF)
                kernel.CloseHandle(process)
        else:
            while os.getppid() == pid:
                time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=monitor, name="lb-control-lifetime", daemon=True).start()


def serve(url, token, home, with_worker):
    import uvicorn
    from .application import create_app

    origin = urlsplit(url)
    app = create_app(home, token)

    class Server(uvicorn.Server):
        child = None

        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started and with_worker:
                env = dict(os.environ)
                env["LB_ATTACHED_PARENT"] = str(os.getpid())
                src = str(Path(__file__).resolve().parents[1])
                env["PYTHONPATH"] = os.pathsep.join(filter(None, [src, env.get("PYTHONPATH")]))
                self.child = subprocess.Popen([sys.executable, "-B", "-m", "lightbrain.cli",
                                               "--home", str(home), "--url", url, "worker"],
                                              env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        async def on_tick(self, counter):
            if self.child is not None and self.child.poll() is not None:
                self.worker_failed = True
                return True
            return await super().on_tick(counter)

    server = Server(uvicorn.Config(app, host=origin.hostname, port=origin.port or 80,
                                   access_log=False, workers=1))
    try:
        server.run()
    finally:
        if server.child is not None and server.child.poll() is None:
            server.child.terminate()
            try:
                server.child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                server.child.kill()
                server.child.wait()
    if getattr(server, "worker_failed", False):
        raise RuntimeError("worker exited unexpectedly; check its output before restarting")
    return 0 if server.started else 1


def parser():
    # Accept connection options either before or after the command.
    conn = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    conn.add_argument("--home", type=Path, help="服务状态目录")
    conn.add_argument("--url", help="本地服务地址")
    p = argparse.ArgumentParser(prog="lb", description="Light-brain 独立研发服务", parents=[conn])
    sub = p.add_subparsers(dest="cmd", required=True)
    def command(name, help):
        return sub.add_parser(name, help=help, parents=[conn])
    command("serve","启动主服务").add_argument("--with-worker",action="store_true")
    command("worker","启动独立工作进程").add_argument("--once",action="store_true")
    submit=command("submit","从一句想法或已有工程发起委托")
    submit.add_argument("idea")
    submit.add_argument("--project",default="default")
    submit.add_argument("--brain",default="personal")
    submit.add_argument("--repo",type=Path)
    submit.add_argument("--criteria",type=Path,help="冻结验收条件 JSON 文件")
    submit.add_argument("--event-id",help="供幂等重发的委托标识")
    submit.add_argument("--max-model-jobs",type=int,help="本委托的模型作业总额度")
    submit.add_argument("--simulate-release",action="store_true")
    submit.add_argument("--model",help="此委托使用的兼容模型标识")
    submit.add_argument("--executor",choices=("model","openhands"),help="此委托使用的开发执行器")
    submit.add_argument("--reuse-method",action="append",help="要求采用的已登记方法标识，可重复指定")
    command("list","列出指定光脑的委托").add_argument("--brain",default="personal")
    live=command("preview","为已验收的 HTTP 应用启动独立的可操作预览")
    live.add_argument("case_id")
    live.add_argument("--ttl",type=int,default=600)
    channel=command("channel","将当前交付版发布到本地模拟渠道，或回到已验收的旧版")
    channel.add_argument("case_id")
    channel.add_argument("action",choices=("stage","rollback"))
    channel.add_argument("--sha")
    for name, help in [("show","查看委托与作品"),("events","查看事件"),("graph","查看状态图"),
                       ("jobs","查看作业与回执"),("cancel","停止当前委托"),
                       ("wait","等待交付、失败或需要输入"),("download","导出产物"),
                       ("revise","修订原委托"),("decision","作出决定"),("retry","重试委托"),
                       ("switch-model","在原委托上切换模型")]:
        c=command(name,help)
        c.add_argument("case_id")
        if name=="wait":
            c.add_argument("--timeout",type=float,default=1800)
            c.add_argument("--interval",type=float,default=1.0)
        if name=="events":
            c.add_argument("--after",type=int,default=0)
        if name=="graph":
            c.add_argument("--revision",type=int)
        if name=="download":
            c.add_argument("output",type=Path)
            c.add_argument("--digest",help="默认下载正式交付的源码归档")
        if name in ("revise","decision"):
            c.add_argument("text")
        if name=="switch-model":
            c.add_argument("model")
        if name=="retry":
            c.add_argument("--text",default="")
    for name in ("methods","promote","revoke"):
        c=command(name,{"methods":"查看可用方法","promote":"以第二项交付推广方法","revoke":"撤回方法"}[name])
        c.add_argument("--project",default="default")
        c.add_argument("--brain",default="personal")
        if name != "methods":
            c.add_argument("method_id")
        if name=="promote":
            c.add_argument("other_case_id")
    return p


def run(args):
    from .settings import runtime_home, auth_token, configure_temp

    home = (getattr(args,"home",None) or runtime_home()).resolve()
    configure_temp(home)
    url = local_url(getattr(args,"url",None) or os.getenv("LB_URL") or "http://127.0.0.1:48717")
    token = auth_token(home)
    if args.cmd=="serve":
        return serve(url,token,home,args.with_worker)
    if args.cmd=="worker":
        from .worker import run_worker
        watch_parent()
        run_worker(url, token, home, once=args.once)
        return 0
    cli=Client(url,token)
    if args.cmd=="submit":
        body={"idea":args.idea,"project":args.project}
        if args.repo:
            body["baseline"]=str(args.repo.resolve(strict=True))
        if args.criteria:
            body["criteria"]=json.loads(args.criteria.read_text(encoding="utf-8-sig"))
            if not isinstance(body["criteria"],list):
                raise ValueError("criteria file must contain a JSON array")
        if args.event_id:
            body["event_id"]=args.event_id
        if args.max_model_jobs is not None:
            body["max_model_jobs"]=args.max_model_jobs
        if args.simulate_release:
            body["simulate_release"]=True
        if args.model:
            body["model"]=args.model
        if args.executor:
            body["executor"]=args.executor
        if args.reuse_method:
            body["reuse_methods"]=list(dict.fromkeys(args.reuse_method))
        print_json(cli.json("/brains/"+quote(args.brain,safe="")+"/cases",body))
        return 0
    if args.cmd=="list":
        print_json(cli.json("/brains/"+quote(args.brain,safe="")+"/cases"))
        return 0
    if args.cmd in ("methods","promote","revoke"):
        path="/brains/"+quote(args.brain,safe="")+"/methods"
        if args.cmd=="methods":
            print_json(cli.json(path+"?"+urlencode({"project":args.project})))
        else:
            body={"project":args.project}
            if args.cmd=="promote":
                body["other_case_id"]=args.other_case_id
            print_json(cli.json(path+"/"+quote(args.method_id,safe="")+"/"+args.cmd,body))
        return 0
    case="/cases/"+quote(args.case_id,safe="")
    if args.cmd=="show":
        print_json(cli.json(case))
    elif args.cmd=="preview":
        print_json(cli.json(case+"/live",{"ttl":args.ttl}))
    elif args.cmd=="channel":
        print_json(cli.json(case+"/channel",{"action":args.action,"candidate_sha":args.sha}))
    elif args.cmd in ("events","graph","jobs"):
        path=case+"/"+args.cmd
        if args.cmd=="events":
            path+="?"+urlencode({"after":args.after})
        if args.cmd=="graph" and args.revision is not None:
            path+="?"+urlencode({"revision":args.revision})
        print_json(cli.json(path))
    elif args.cmd=="wait":
        if args.timeout<=0 or args.interval<=0:
            raise ValueError("wait timeout and interval must be positive")
        end=time.monotonic()+args.timeout
        while True:
            state=cli.json(case)
            status=state["state"]
            if status in {"delivered","failed","cancelled","needs_input"}:
                print_json(state)
                return 0 if status=="delivered" else 3
            remaining=end-time.monotonic()
            if remaining<=0:
                print_json(state)
                return 4
            time.sleep(min(remaining,args.interval))
    elif args.cmd=="download":
        checksum=args.digest or (cli.json(case).get("delivery") or {}).get("archive_id")
        if not checksum:
            raise ValueError("this case has no delivered archive")
        if args.output.exists():
            raise ValueError(f"output already exists: {args.output}")
        output=args.output.resolve()
        output.parent.mkdir(parents=True,exist_ok=True)
        tmp=output.with_name("."+uuid4().hex+".part")
        try:
            from hashlib import file_digest
            with cli.request(case+"/artifacts/"+quote(checksum,safe="")) as res, tmp.open("xb") as dest:
                shutil.copyfileobj(res,dest)
            with tmp.open("rb") as source:
                if file_digest(source,"sha256").hexdigest()!=checksum:
                    raise ValueError("download digest does not match the requested artifact")
            os.link(tmp,output)
        finally:
            tmp.unlink(missing_ok=True)
        print_json({"path":str(output),"sha256":checksum})
    elif args.cmd in ("revise","decision","switch-model","retry","cancel"):
        body={"kind":args.cmd.replace("-","_"),"text":getattr(args,"text","")}
        if args.cmd=="switch-model":
            body["model"]=args.model
        print_json(cli.json(case+"/events",body))
    return 0


def main(argv=None):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,"reconfigure"):
            stream.reconfigure(encoding="utf-8",errors="replace")
    try:
        return run(parser().parse_args(argv))
    except HTTPError as err:
        print(f"HTTP {err.code} {err.read(8192).decode('utf-8','replace')}",file=sys.stderr)
    except (ValueError, OSError, RuntimeError, URLError, ImportError) as err:
        print(str(err),file=sys.stderr)
    except KeyboardInterrupt:
        return 130
    return 1


if __name__=="__main__":
    raise SystemExit(main())
