import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import tempfile
from threading import Thread, Event
import time
from urllib import request, error
from urllib.parse import urlencode
from uuid import uuid4
import zipfile

from .settings import configure_temp, worker_key
from .repository import source_context
from .model import ask, PLAN, CODE, TemporaryModelError


class Transport:
    def __init__(self,url,token):
        self.url=url.rstrip("/")
        self.headers={"Authorization":"Bearer "+worker_key(token)}

    def post(self,path,body):
        req=request.Request(self.url+path,data=json.dumps(body).encode(),headers={**self.headers,"Content-Type":"application/json"})
        with request.urlopen(req,timeout=30) as res:
            return json.load(res)

    def download(self,path,target,digest):
        h=hashlib.sha256()
        with request.urlopen(request.Request(self.url+path,headers=self.headers),timeout=90) as res, target.open("wb") as f:
            while chunk:=res.read(1024*1024):
                f.write(chunk);h.update(chunk)
        if h.hexdigest()!=digest:
            raise ValueError("工件传输校验失败")

    def upload(self,path,source):
        with Path(source).open("rb") as f:
            req=request.Request(self.url+path,data=f,headers={**self.headers,"Content-Type":"application/octet-stream",
                "Content-Length":str(Path(source).stat().st_size)})
            with request.urlopen(req,timeout=90) as res:
                return json.load(res)["sha256"]


def execute(job,directory,client,wid):
    from .host.runner import handle
    kind=job["kind"];p=dict(job["payload"])
    base="/internal/jobs/"+job["id"]+"/artifacts"
    query="?"+urlencode({"attempt":job["attempt"],"worker_id":wid})
    directory=Path(directory)
    ctx={"case_id":job["case_id"],"workdir":str(directory),"artifacts":str(directory/"out"),"release_mode":"simulate",
         "enable_openhands":os.getenv("LB_ENABLE_OPENHANDS")=="1","release_store":str(directory.parent/"channels"),
         "openhands_backend":os.getenv("LB_OPENHANDS_BACKEND","bubblewrap"),"openhands_iterations":20}
    for key,filename in (("bundle","input.bundle"),("archive","export.zip")):
        if p.get(key+"_id"):
            target=directory/filename
            client.download(base+"/"+p[key+"_id"]+query,target,p[key+"_id"])
            p[key]=str(target)
            p[key+"_sha256"]=p[key+"_id"]
    model_info={}
    if kind=="reuse_implementation":
        from .implementation_methods import materialize
        p["files"]=materialize(p)
    if kind in {"plan","develop","review","design"}:
        source=source_context(p["bundle"],p["candidate_sha"],directory) if p.get("bundle") else {}
        data={k:p[k] for k in ("idea","owner_criteria","criteria","approach","instruction","failure","methods","feedback","protected_files") if p.get(k)}
        data["source"]=source
        if kind=="plan":
            from .inquiry import PROMPT
            from .recipes import catalog
            data["available_templates"]=catalog()
            proposal,model_info=ask(PLAN+PROMPT+"\nSet visual:true for a user-facing website. Set templates to applicable IDs from available_templates. Existing repositories keep their own design system.",data,p.get("model"))
            return {"ok":True,"proposal":proposal,"model":model_info}
        if kind=="review":
            result,model_info=ask("Review the repository for this commission. Return JSON {summary: string, findings: [{path,line,quote,detail}]}. Reference exact source, do not claim tests ran.",data,p.get("model"))
            for row in result["findings"]:
                lines=source[row["path"]].splitlines()
                n=row["line"]
                if type(n)!=int or not 1<=n<=len(lines) or not row["quote"] or row["quote"].strip() not in lines[n-1]:
                    raise ValueError("评审引用未能在基线源码指定行核对")
            return {"ok":True,"review":result,"model":model_info,"candidate_sha":p["candidate_sha"]}
        if kind=="design":
            proposal,model_info=ask("Produce the first interactive visual design for this confirmed product. Return JSON {html:string}. Make one offline, usable, responsive index.html. Include realistic initial data and the central user interaction, keyboard support, reduced motion, inline styles/scripts. Respect existing template assets and any browser acceptance selectors. No remote resources.",data,p.get("model"))
            html=proposal.get("html")
            if not isinstance(html,str) or "<html" not in html.lower():
                raise ValueError("视觉试演需要有效的 HTML 页面")
            p["files"]={"index.html":html}
        elif p.get("executor")=="openhands":
            p.update(engine="openhands",task=json.dumps({k:v for k,v in data.items() if k!="source"},ensure_ascii=False))
        else:
            proposal,model_info=ask(CODE,data,p.get("model"))
            files=proposal["files"]
            if not isinstance(files,dict) or len(files)>180 or not all(isinstance(v,str) for v in files.values()):
                raise ValueError("编码模型必须交付相对路径到完整文件内容的映射")
            p.update(files=files,delete=proposal.get("delete",[]))
            for filename,checksum in p.get("protected_files",{}).items():
                if filename in files and hashlib.sha256(files[filename].encode()).hexdigest()==checksum:
                    del files[filename]
                if filename in files or filename in p["delete"]:
                    raise ValueError("已核准复用的实现被重写，请保留该文件并在外围完成适配")
    if kind=="template":
        from .recipes import render
        material=render(p["templates"],directory)
        p.update(files=material["files"],create_only=True)
    host_kind={"method_verify":"verify","inquiry_probe":"verify","reuse_implementation":"develop",
               "design":"develop","template":"develop"}.get(kind,kind)
    receipt=handle(host_kind,p,ctx)
    if kind=="template" and receipt["ok"]:
        receipt["template_receipts"]=material["receipts"]
    if kind=="design" and receipt["ok"]:
        design=handle("build",{**receipt,"entrypoint":["index.html"]},ctx)
        if not design["ok"]: return design
        receipt.update(archive=design["archive"],archive_sha256=design["archive_sha256"])
    for row in receipt.get("checks",[]):
        ob=row.get("observed",{})
        if ob.get("screenshot_path"):
            file=Path(ob.pop("screenshot_path")).resolve(strict=True)
            if not file.is_relative_to(directory.resolve()):
                raise ValueError("截图输出超出作业目录")
            evidence=client.upload(base+query,file)
            if evidence!=ob.get("screenshot_sha256"):
                raise ValueError("截图传输校验失败")
            ob["screenshot_id"]=evidence
    if model_info:
        receipt["model"]=model_info
    elif receipt.get("development",{}).get("model"):
        engine=receipt["development"]
        receipt["model"]={"requested_model":p.get("model") or engine["model"],"model":engine["model"],"executor":engine["executor"]}
    for key in ("bundle","archive"):
        if key in receipt:
            source=Path(receipt.pop(key)).resolve()
            if not source.is_relative_to(directory.resolve()):
                raise ValueError("输出文件超出作业目录")
            if key=="archive":
                with zipfile.ZipFile(source) as z:
                    receipt["has_preview"]="index.html" in z.namelist()
            digest=client.upload(base+query,source)
            if digest!=receipt[key+"_sha256"]:
                raise ValueError("返回工件摘要不匹配")
            receipt[key+"_id"]=digest
    return receipt


def run_worker(url,token,home,once=False):
    root=Path(home).resolve()/"worker"
    root.mkdir(parents=True,exist_ok=True)
    configure_temp(root)
    client=Transport(url,token)
    wid=socket.gethostname()+"-"+uuid4().hex[:10]
    while True:
        try:
            job=client.post("/internal/jobs/claim",{"worker":wid})
        except (OSError,error.HTTPError):
            if once: raise
            time.sleep(1);continue
        if job:
            base="/internal/jobs/"+job["id"]
            lease={"worker":wid,"attempt":job["attempt"]}
            stop=Event()
            def heartbeat():
                while not stop.wait(12):
                    try: client.post(base+"/heartbeat",lease)
                    except OSError: return
            pulse=Thread(target=heartbeat,daemon=True)
            pulse.start()
            try:
                with tempfile.TemporaryDirectory(prefix="job-",dir=root,ignore_cleanup_errors=True) as work:
                    result=execute(job,work,client,wid)
            except Exception as exc:
                result={"ok":False,"error":f"{type(exc).__name__}: {exc}"[:1800],"retryable":isinstance(exc,TemporaryModelError)}
            finally:
                stop.set();pulse.join(2)
            for attempt in range(3):
                try:
                    client.post(base+"/receipt",{**lease,"receipt":result})
                    break
                except error.HTTPError as exc:
                    if exc.code in (401,403,404,409) or attempt==2: break
                except OSError:
                    if attempt==2: break
                time.sleep(0.5)
        elif not once:
            time.sleep(0.45)
        if once: return
