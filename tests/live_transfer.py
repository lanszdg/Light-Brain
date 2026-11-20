"""Opt-in real gateway test for granting a previously learned implementation to a new project."""

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4
from zipfile import ZipFile

from live_delivery import ROOT, eventually, ready
from lightbrain.cli import Client
from lightbrain.settings import auth_token, configure_temp


@contextmanager
def service(home):
    with socket.socket() as s:
        s.bind(("127.0.0.1",0))
        url = "http://127.0.0.1:" + str(s.getsockname()[1])
    configure_temp(home)
    client = Client(url, auth_token(home))
    profile = os.environ.get("LB_MODEL_ENV") or str(ROOT.parent / "prototype-machine/incubator/.env")
    env = {**os.environ, "LB_MODEL_ENV": profile, "LB_HOME":str(home), "LB_URL":url}
    with (home.parent / "transfer-service.log").open("a", encoding="utf-8") as log:
        command = [sys.executable,"-B","-X","utf8",str(ROOT/"lb.py"),"--home",str(home),"--url",url]
        process = subprocess.Popen(command + ["serve","--with-worker"], cwd=ROOT, env=env,
                                   creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0), stdout=log,stderr=log)
        try:
            eventually(lambda: ready(url), 20)
            yield client
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(15)


def archive(client, case):
    sha = case["delivery"]["archive_id"]
    with client.request("/cases/"+case["id"]+"/artifacts/"+sha) as res:
        data = res.read()
    assert hashlib.sha256(data).hexdigest() == sha
    return ZipFile(io.BytesIO(data))


def start():
    output = ROOT / "交付验证"
    base = json.loads((output/"coding.json").read_text(encoding="utf-8"))
    source_id = next(r["case_id"] for r in base["results"] if r["project"]=="crew-a")
    home = Path(base["state_directory"]).resolve(strict=True)
    assert home.is_relative_to(ROOT)
    with service(home) as cli:
        case = lambda cid: cli.json("/cases/"+cid)
        source = eventually(lambda: (c if (c:=case(source_id)).get("learning_revision")==2 else None), 30)
        brain = source["brain_id"]
        methods = "/brains/"+brain+"/methods"
        catalog = cli.json(methods+"?project=crew-a")
        pick = lambda t: next(m for m in catalog if m["id"].startswith(t) and m["status"]=="active"
                              and m["scope"].get("project_id")=="crew-a"
                              and m.get("source_criterion",{}).get("id")=="two-forward")
        impl, replay = pick("impl-"), pick("check-")
        conditions = [{**c, "argv":[c["argv"][0],"roster.py",*c["argv"][2:]]}
                      for c in source["criteria"] if c["id"] in {"two-forward","left-one"}]
        conditions += [
            {"id":"lead-positive", "kind":"command","argv":["python3","lead.py","--lead","2","A","B","C","D","E"],"stdout_equals":"C\n"},
            {"id":"lead-negative", "kind":"command","argv":["python3","lead.py","--lead","-1","Amy","Bo","Cal"],"stdout_equals":"Cal\n"},
            {"id":"lead-empty", "kind":"command","argv":["python3","lead.py","--lead","9"],"stdout_equals":"No lead\n"}]
        idea = ("为另一组值班人员制作离线的名单轮换和当班领队 CLI。前一个项目已提供已验收的名单轮换程序，"
                "工作台将把它逐字节复用为 roster.py，此文件由系统负责，禁止在开发返回中包含或修改它。"
                "在独立的新文件 lead.py 实现 --lead 步长 姓名...，使用同目录 roster.py --rotate 的行为，"
                "输出轮换后的第一人；支持负步长，名单为空输出 No lead。另附 README 启动与示例，使用 Python 标准库。")
        ref = ["crew-a/"+impl["id"], "crew-a/"+replay["id"]]
        request = {"project":"crew-b","idea":idea,"criteria":conditions,"reuse_methods":ref,
                   "event_id":"live-transfer-"+uuid4().hex}
        resume = os.environ.get("LB_TRANSFER_CASE_ID")
        dest = case(resume) if resume else cli.json("/brains/"+brain+"/cases",request)
        cid = dest["id"]
        assert dest["request"]["reuse_methods"]==ref and dest["request"]["criteria"]==conditions
        retried = set()
        def terminal():
            c = case(cid)
            error = str(c.get("error","")).lower()
            if (c["state"]=="failed" and ("timed out" in error or "http 50" in error or "http 429" in error)
                    and len(retried)<2 and c["revision"] not in retried):
                retried.add(c["revision"])
                cli.json("/cases/"+cid+"/events",{"kind":"retry"})
                return None
            if c["state"] in {"failed","cancelled","needs_input"}:
                raise RuntimeError(f"{cid} {c['state']} {c.get('error') or c.get('decisions')}")
            return c if c["state"]=="delivered" and c.get("learning_revision")==2 else None
        dest = eventually(terminal, 700)
        jobs = cli.json("/cases/"+cid+"/jobs")
        reuse = next(j for j in jobs if j["kind"]=="reuse_implementation" and j["receipt"]["ok"])
        assert reuse["payload"]["target_path"]=="roster.py" and reuse["payload"]["method_id"]==impl["id"]
        assert all(j["payload"]["protected_files"].get("roster.py")==impl["content_sha256"]
                   for j in jobs if j["kind"]=="develop")
        with archive(cli,source) as a, archive(cli,dest) as b:
            before, after = a.read("shift.py"), b.read("roster.py")
            assert before == after and hashlib.sha256(after).hexdigest() == impl["content_sha256"]
            assert b.read("lead.py").strip() and "README.md" in b.namelist()
        export = next(j for j in jobs if j["id"]==dest["delivery"]["export_job"])
        assert export["receipt"]["ok"] and export["receipt"]["archive_sha256"]==dest["delivery"]["archive_id"]
        measured = {x["id"]:x["observed"]["stdout"] for x in export["receipt"]["checks"] if x["ok"]}
        assert measured == {c["id"]:c["stdout_equals"] for c in conditions}
        replays = [j for j in jobs if j["kind"]=="method_verify" and j["receipt"]["ok"]
                   and j["payload"]["method_revision"]["id"]==replay["id"]]
        assert replays, "the earlier project's check must actually execute under the new project"
        assert all(j["receipt"]["candidate_sha"]==dest["delivery"]["candidate_sha"] for j in replays)
        promotion = []
        for method in (impl,replay):
            path = methods+"/"+method["id"]
            promoted = cli.json(path+"/promote",{"project":"crew-a","other_case_id":cid})
            assert promoted["scope"]=={"brain_id":brain} and promoted["status"]=="active"
            accessible = cli.json(methods+"?project=unrelated-empty-project")
            assert any(m["id"]==method["id"] and m["status"]=="active" for m in accessible)
            revoked = cli.json(path+"/revoke",{"project":None})
            assert revoked["scope"]==promoted["scope"] and revoked["revision"]>promoted["revision"]
            access = cli.json(methods+"?project=unrelated-empty-project")
            assert next(m for m in access if m["id"]==method["id"])["status"]=="revoked"
            promotion.append({"method_id":method["id"],"source_scope":method["scope"],
                              "promoted_revision":promoted["revision"],"revoked_revision":revoked["revision"],
                              "scope":promoted["scope"],"positive_receipts":promoted["support_ids"],
                              "counterexamples":promoted["counterexamples"]})
        report = {"scope":"real configured model, second project, independent export, promote and revoke",
                  "source_case":source_id,"destination_case":cid,"source_project":"crew-a","destination_project":"crew-b",
                  "source_sha":source["delivery"]["candidate_sha"],"destination_sha":dest["delivery"]["candidate_sha"],
                  "source_archive":source["delivery"]["archive_id"],"destination_archive":dest["delivery"]["archive_id"],
                  "source_file":"shift.py","reused_file":"roster.py","identical_file_sha256":impl["content_sha256"],
                  "new_file":"lead.py","owner_grant":ref,"reuse_job":reuse["id"],
                  "models":[dest["plan_model"]["model"],dest["code_model"]["model"]],
                  "all_jobs":[{"id":j["id"],"kind":j["kind"],"ok":j["receipt"]["ok"]} for j in jobs],
                  "source_check_replays":[{"id":j["id"],"method":replay["id"],
                      "candidate_sha":j["receipt"]["candidate_sha"],
                      "observed_stdout":[x["observed"]["stdout"] for x in j["receipt"]["checks"]]}
                      for j in replays],
                  "export_job":export["id"],"measured_stdout":measured,"promotion":promotion,
                  "verified_at_unix":time.time()}
        (output/"transfer.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(report,ensure_ascii=False), flush=True)


if __name__=="__main__":
    start()
