"""Opt-in acceptance run against the configured real model and isolated execution host."""
from pathlib import Path
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from urllib.request import urlopen
from uuid import uuid4
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from lightbrain.cli import Client
from lightbrain.settings import auth_token,configure_temp


def eventually(fn,seconds=420):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=fn()
        if value:return value
        time.sleep(0.35)
    raise RuntimeError("timed out waiting for "+fn.__name__)


def ready(url):
    try:
        with urlopen(url+"/health",timeout=1) as response:
            return json.load(response)["ok"]
    except OSError:
        return False


def start():
    root=ROOT/".lb"/"acceptance"/uuid4().hex
    root.mkdir(parents=True)
    cfg=root/"state"
    configure_temp(root)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        url="http://127.0.0.1:"+str(sock.getsockname()[1])
    token=auth_token(cfg)
    cli=Client(url,token)
    children=[]
    log=(root/"service.log").open("w",encoding="utf-8")
    def launch(*args):
        proc=subprocess.Popen([sys.executable,"-B","-X","utf8",str(ROOT/"lb.py"),"--home",str(cfg),"--url",url,*args],
            cwd=ROOT,stdout=log,stderr=log,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        children.append(proc)
        return proc
    def stop(proc):
        if proc.poll() is None:
            proc.terminate();proc.wait(timeout=12)
    def terminal(cid):
        c=cli.json("/cases/"+cid)
        if c["state"] in {"needs_input","failed","cancelled"}:
            raise RuntimeError(str((cid,c["state"],c.get("error"),c.get("decisions"))))
        return c if c["state"]=="delivered" and c.get("learning") is not None else None
    def body(name):
        return [{"id":"old-version","kind":"command","argv":["python3",name,"--version"],"stdout_equals":"1.0\n"},
                {"id":"two-forward","kind":"command","argv":["python3",name,"--rotate","2","A","B","C","D","E"],"stdout_equals":"C D E A B\n"},
                {"id":"left-one","kind":"command","argv":["python3",name,"--rotate","-1","A","B","C"],"stdout_equals":"C A B\n"},
                {"id":"empty","kind":"command","argv":["python3",name,"--rotate","9"],"stdout_equals":"\n"}]
    idea=("给交接班做个离线名单轮换 CLI。保留 --version 返回 1.0。用 python3 shift.py --rotate 步长 姓名... "
          "输出左移对应步数后的姓名，以空格分隔。支持负步长、大于人数的步长和空名单（空行）。"
          "不要联网，README 写出启动命令。")
    repo=root/"original";repo.mkdir()
    (repo/"shift.py").write_text("import sys\nif sys.argv[1:] == ['--version']:\n    print('1.0')\nelse:\n    raise SystemExit(2)\n",encoding="utf-8")
    def git(*args):
        return subprocess.check_output(["git","-c","core.hooksPath="+os.devnull,"-c","user.name=LB Acceptance",
            "-c","user.email=lb@localhost","--no-optional-locks",*args],cwd=repo,text=True).strip()
    git("init","-q","--initial-branch=main");git("add","shift.py");git("commit","-qm","Existing CLI")
    original=git("rev-parse","HEAD")
    report={"scope":"real model, local delivery and simulated release","results":[],"state_directory":str(cfg)}
    output=Path(os.environ.get("LB_ACCEPTANCE_OUTPUT",ROOT/"交付验证"/"coding.json"))
    try:
        server=launch("serve");eventually(lambda:ready(url))
        c=cli.json("/brains/acceptance/cases",{"idea":idea,"project":"crew-a","baseline":str(repo),
            "criteria":body("shift.py"),"simulate_release":True,"event_id":"existing-repo"})
        cid=c["id"]
        eventually(lambda:cli.json("/cases/"+cid+"/jobs"))
        worker=launch("worker","--once");worker.wait(timeout=220)
        def planned():
            c=cli.json("/cases/"+cid)
            if c["state"] in {"failed","needs_input"}: raise RuntimeError(str(c.get("error") or c.get("decisions")))
            return c if c.get("plan") else None
        before=eventually(planned,15)
        stop(server)
        server=launch("serve");eventually(lambda:ready(url),20)
        after=cli.json("/cases/"+cid)
        assert before["plan"]==after["plan"] and before["criteria"]==after["criteria"] and after["input_source"]["candidate_sha"]==original
        alternative=os.environ.get("LB_ACCEPTANCE_SECOND_MODEL","glm-5.3-flash")
        cli.json("/cases/"+cid+"/events",{"kind":"switch_model","model":alternative})
        worker=launch("worker")
        finished=eventually(lambda:terminal(cid))
        jobs=cli.json("/cases/"+cid+"/jobs")
        baseline=next(j for j in jobs if j["payload"].get("purpose")=="baseline" and j["receipt"].get("checks"))
        checks=baseline["receipt"]["checks"]
        assert not baseline["receipt"]["ok"] and next(x for x in checks if x["id"]=="old-version")["ok"]
        assert all(not x["ok"] for x in checks if x["id"]!="old-version")
        declared=finished["delivery"];archive=declared["archive_id"]
        assert finished["code_model"]["requested_model"]==alternative and finished["plan_model"]["model"]!=finished["code_model"]["model"]
        assert declared["candidate_sha"]!=original and original==git("rev-parse","HEAD") and not git("status","--porcelain")
        assert declared["simulation"]["active"] and declared["simulation"]["deployment_id"]
        verified=next(j["receipt"] for j in jobs if j["id"]==declared["export_job"])
        assert verified["ok"] and verified["archive_sha256"]==archive and verified["candidate_sha"]==declared["candidate_sha"]
        dest=output.parent;dest.mkdir(parents=True,exist_ok=True)
        with cli.request("/cases/"+cid+"/artifacts/"+archive) as resp:
            binary=resp.read()
        assert hashlib.sha256(binary).hexdigest()==archive
        (dest/"名单轮换.zip").write_bytes(binary)
        with zipfile.ZipFile(dest/"名单轮换.zip") as z:
            assert "shift.py" in z.namelist()
        report["results"].append({"case_id":cid,"project":"crew-a","original_sha":original,"candidate_sha":declared["candidate_sha"],
            "baseline_checks":[[x["id"],x["ok"]] for x in checks],"export_checks":[[x["id"],x["ok"]] for x in verified["checks"]],
            "export_sha256":archive,"models":[finished["plan_model"]["model"],finished["code_model"]["model"]],
            "environment":verified["environment"],"recovered_after_restart":True,"original_unchanged":True,
            "release":declared["simulation"],"methods":finished["learning"],
            "jobs":[{"id":j["id"],"kind":j["kind"],"attempt":j["attempt"],"ok":j["receipt"]["ok"]} for j in jobs]})
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(report,ensure_ascii=False),flush=True)
    finally:
        for child in reversed(children): stop(child)
        log.close()


if __name__=="__main__":
    start()
