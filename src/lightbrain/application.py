from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path, PurePosixPath
from threading import RLock, Thread, Event
import hmac
import logging
import mimetypes
import tempfile
import zipfile

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .controller import Controller
from .contracts import criteria_list, digest
from .settings import configure_temp, worker_key, controller_lock
from .store import Store, Conflict, NotFound
from .studio import mount_studio


def create_app(home:Path,token:str):
    home=Path(home).resolve()
    configure_temp(home)
    s=Store(home)
    control=Controller(s)
    from .live_preview import PreviewManager
    previews=PreviewManager(s)
    lock=RLock()
    stop=Event()

    def cycle():
        while not stop.is_set():
            try:
                with lock:
                    control.tick()
            except Exception:
                logging.exception("controller tick failed")
            stop.wait(0.4)

    @asynccontextmanager
    async def lifespan(app):
        with controller_lock(home):
            thread=Thread(target=cycle,daemon=True,name="lb-controller")
            thread.start()
            try:
                yield
            finally:
                stop.set();thread.join(5)
                previews.close()

    app=FastAPI(title="Light-brain",lifespan=lifespan,docs_url=None,redoc_url=None)
    mount_studio(app)

    def credential(secret):
        def auth(authorization:str=Header("")):
            if not hmac.compare_digest(authorization,"Bearer "+secret):
                raise HTTPException(401,"invalid credentials")
        return [Depends(auth)]
    owner=credential(token)
    worker=credential(worker_key(token))

    @app.exception_handler(ValueError)
    async def invalid(req,exc):
        return JSONResponse({"detail":str(exc)},status_code=409 if isinstance(exc,Conflict) else 422)

    @app.exception_handler(NotFound)
    async def missing(req,exc):
        return JSONResponse({"detail":str(exc)},status_code=404)

    @app.get("/health")
    def health():
        return {"ok":True,"service":"lightbrain"}

    @app.post("/brains/{brain}/cases",dependencies=owner,status_code=202)
    def create(brain:str,body:dict):
        idea=body.get("idea")
        if not isinstance(idea,str) or not 1<=len(idea.strip())<=20000:
            raise ValueError("请输入 1 至 20000 字的委托")
        if body.get("criteria") is not None:
            criteria_list(body["criteria"])
        fields={"idea","project","baseline","criteria","model","event_id","simulate_release","executor","reuse_methods","max_model_jobs"}
        if set(body)-fields:
            raise ValueError("未知输入字段")
        if body.get("executor","model") not in {"model","openhands"}:
            raise ValueError("未知开发执行器")
        maximum=body.get("max_model_jobs",16)
        if type(maximum)!=int or not 1<=maximum<=128:
            raise ValueError("模型作业预算须为 1 至 128")
        refs=body.get("reuse_methods",[])
        if not isinstance(refs,list) or len(refs)>32 or not all(isinstance(x,str) and 0<len(x)<=200 for x in refs):
            raise ValueError("复用授权需要方法 ID 列表")
        with lock:
            case=s.create_case(brain,body.get("project") or "default",body,body.get("event_id"))
            if case["state"]=="new":
                control.initialize(case)
        return s.get_case(case["id"])

    def presentation(c):
        if c.get("design_id"):
            key=hmac.new(token.encode(),(c["id"]+c["design_id"]).encode(),sha256).hexdigest()
            c={**c,"design_url":f"/view/{c['id']}/{c['design_id']}/{key}/index.html"}
        d=c.get("delivery") or {}
        if d.get("archive_id") and d.get("preview_url"):
            key=hmac.new(token.encode(),(c["id"]+d["archive_id"]).encode(),sha256).hexdigest()
            c={**c,"delivery":{**d,"preview_url":f"/view/{c['id']}/{d['archive_id']}/{key}/index.html"}}
        return c

    @app.get("/cases/{cid}",dependencies=owner)
    def get(cid:str):
        return presentation(s.get_case(cid))

    @app.get("/brains/{brain}/cases",dependencies=owner)
    def cases(brain:str):
        return [presentation(c) for c in s.list_cases(brain)]

    @app.get("/cases/{cid}/events",dependencies=owner)
    def events(cid:str,after:int=0):
        return s.list_events(cid,after)

    @app.get("/cases/{cid}/jobs",dependencies=owner)
    def jobs(cid:str):
        return s.list_jobs(cid)

    @app.get("/cases/{cid}/graph",dependencies=owner)
    def graph(cid:str,revision:int|None=None):
        return s.graph_snapshot(cid,revision)

    @app.get("/cases/{cid}/versions",dependencies=owner)
    def versions(cid:str):
        versions=[]
        for row in s.list_events(cid):
            if row["event_type"]!="delivered":
                continue
            delivery=row["body"].get("patch",{}).get("delivery") or {}
            archive=delivery.get("archive_id")
            if not archive or not delivery.get("export_job"):
                continue
            version={k:delivery.get(k) for k in ("archive_id","candidate_sha","goal_version","entrypoint","export_job")}
            if delivery.get("preview_url"):
                key=hmac.new(token.encode(),(cid+archive).encode(),sha256).hexdigest()
                version["preview_url"]=f"/view/{cid}/{archive}/{key}/index.html"
            versions.append(version)
        return list(reversed(versions))

    @app.post("/cases/{cid}/events",dependencies=owner)
    def event(cid:str,body:dict):
        kind=body.get("kind")
        with lock:
            c=s.get_case(cid)
            request_id=body.get("event_id")
            if request_id is not None:
                if not isinstance(request_id,str) or not 1<=len(request_id)<=96 or not request_id.replace("-","").isalnum():
                    raise ValueError("无效的委托操作标识")
                event_key=f"owner:{cid}:{request_id}"
                recorded=next((row for row in s.list_events(cid) if row["event_id"].startswith(event_key+":")),None)
                if recorded:
                    if recorded["body"].get("patch",{}).get("owner_operation") != digest(body):
                        raise Conflict("该操作标识已用于其他输入")
                    return c
            else:
                event_key=None
            operation={"owner_operation":digest(body)} if event_key else {}
            if kind=="retry" and c.get("epoch") is None:
                if c["state"]!="failed":
                    raise Conflict("当前无需重试")
                c=control.commit(c,"retry_intake",{"state":"new","error":None})
                return control.initialize(c)
            if kind=="retry":
                if c["state"]!="failed":
                    raise Conflict("只有已失败委托可重新发起")
                patch={"state":"running","active":None,"error":None,"plan_attempts":0}
                if c.get("repairs",0)>=3:
                    patch["repairs"]=c["repairs"]+1
            elif kind=="switch_model":
                if c["state"] not in {"running","failed","needs_input"} or not body.get("model"):
                    raise ValueError("切换需提供进行中委托和目标模型名")
                patch={"model":body["model"],"active":None,"state":"needs_input" if c["state"]=="needs_input" else "running"}
            elif kind in {"revise","decision"}:
                change=body.get("text")
                if not isinstance(change,str) or not change.strip():
                    raise ValueError("请给出修订内容")
                if kind=="decision" and c["state"]!="needs_input":
                    raise Conflict("当前没有待裁定的选择")
                if kind=="decision" and (c.get("inquiry_step") or {}).get("kind")=="ask":
                    from .inquiry import record_answer
                    from uuid import uuid4
                    fact=record_answer(c["plan"]["inquiry"],c["inquiry_step"],change,str(uuid4()))
                    return control.commit(c,"owner:branch_decision",{"state":"running","active":None,
                        "observations":c.get("observations",[])+[fact],"decisions":[],**operation},event_key=event_key)
                idea=c["idea"]+"\n"+change
                if len(idea)>60000:
                    raise ValueError("委托累计修改过长，请以最新工程为基线开启新委托")
                conditions=body.get("criteria",c.get("owner_criteria"))
                if conditions is not None:
                    criteria_list(conditions)
                patch={"idea":idea,"active":None,"plan":None,"criteria":None,"facts":[],
                       "input_source":c.get("candidate") or c.get("input_source"),
                       "owner_criteria":conditions,"error":None,"fault":None,"repairs":0,"decisions":[],
                       "delivery":None,"learning":None,"learning_revision":0,"archive_id":None,"baseline_job":None,"baseline_fault":None,"inquiry_step":None,"observations":[],
                       "design_id":None,"template_receipts":[],"protected_files":{},
                       "plan_attempts":0,"planning_feedback":"","model_jobs":0,"transient_failures":0,"retry_after":0,
                       "simulation":None,"verified_job":None,"export_job":None,"has_preview":False,"design_sha":None,
                       "state":"running","goal_version":c["goal_version"]+1}
            elif kind=="cancel":
                patch={"state":"cancelled","active":None}
            else:
                raise ValueError("未知委托事件")
            patch["epoch"]=c.get("epoch",0)+1
            return control.commit(c,"owner:"+kind,{**patch,**operation},event_key=event_key)

    @app.get("/cases/{cid}/artifacts/{artifact}",dependencies=owner)
    def artifact(cid:str,artifact:str):
        return FileResponse(s.read_path(artifact,cid),media_type="application/octet-stream")

    @app.post("/cases/{cid}/channel",dependencies=owner)
    def channel(cid:str,body:dict):
        action=body.get("action")
        if action not in {"stage","rollback"}:
            raise ValueError("可选 stage 或 rollback")
        with lock:
            case=s.get_case(cid)
            if case["state"]!="delivered" or case.get("active"):
                raise Conflict("请等待当前委托交付和渠道作业结束")
            target=(case["delivery"] or {}).get("candidate_sha") if action=="stage" else body.get("candidate_sha")
            history=[e["body"].get("patch",{}).get("delivery") for e in s.list_events(cid) if e["event_type"]=="delivered"]
            selected=next((d for d in reversed(history) if d and d.get("candidate_sha")==target and d.get("export_job")),None)
            if not selected or not s.get_job(selected["export_job"])["receipt"].get("ok"):
                raise ValueError("请选择本委托已独立验收的源码版本")
            return control.dispatch(case,"release",{"purpose":"channel_change","action":action,"archive_id":selected["archive_id"],
                "candidate_sha":target,"operation_id":f"{cid}/{case['revision']}/{action}/{target}"})

    @app.post("/cases/{cid}/live",dependencies=owner)
    def launch_preview(cid:str,body:dict):
        return previews.start(cid,body.get("ttl",600))

    @app.get("/cases/{cid}/live",dependencies=owner)
    def inspect_preview(cid:str):
        return previews.status(cid)

    @app.delete("/cases/{cid}/live",dependencies=owner)
    def stop_preview(cid:str):
        previews.stop(cid)
        return {"running":False}

    @app.get("/view/{cid}/{archive}/{signature}/{name:path}")
    def preview(cid:str,archive:str,signature:str,name:str):
        expected=hmac.new(token.encode(),(cid+archive).encode(),sha256).hexdigest()
        if not hmac.compare_digest(signature,expected):
            raise HTTPException(404)
        name=name or "index.html"
        if "\\" in name or ".." in PurePosixPath(name).parts or name.startswith("/"):
            raise HTTPException(404)
        with zipfile.ZipFile(s.read_path(archive,cid)) as z:
            try:
                info=z.getinfo(name)
            except KeyError:
                raise HTTPException(404) from None
            if info.file_size>16*1024*1024:
                raise HTTPException(413)
            data=z.read(info)
        return Response(data,media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
            headers={"Content-Security-Policy":"sandbox allow-scripts; default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; font-src 'self'; connect-src 'none'; form-action 'none'; base-uri 'none'",
                     "X-Content-Type-Options":"nosniff","Referrer-Policy":"no-referrer"})

    @app.get("/brains/{brain}/methods",dependencies=owner)
    def methods(brain:str,project:str="default"):
        return s.list_methods(brain,project,include_inactive=True)

    @app.post("/brains/{brain}/methods/{mid}/{action}",dependencies=owner)
    def change_method(brain:str,mid:str,action:str,body:dict):
        with lock:
            if action=="revoke":
                return s.revoke_method(brain,body.get("project"),mid)
            if action=="promote":
                if mid.startswith("impl-"):
                    from .implementation_methods import promote_implementation
                    return promote_implementation(s,brain,body["project"],mid,body["other_case_id"])
                from .learning import promote
                return promote(s,brain,body["project"],mid,body["other_case_id"])
            raise ValueError("未知方法操作")

    @app.post("/internal/jobs/claim",dependencies=worker)
    def claim(body:dict):
        with lock:
            for _ in range(16):
                job=s.claim(body["worker"],body.get("kinds"),lease_seconds=60)
                if not job:
                    return None
                c=s.get_case(job["case_id"])
                if c.get("active")==job["id"] and c["epoch"]==job["payload"]["epoch"]:
                    return job
                s.finish(job["id"],job["attempt"],{"ok":False,"superseded":True},body["worker"])
            return None

    def live(jid,attempt,worker_id):
        j=s.heartbeat(jid,attempt,60,worker_id)
        c=s.get_case(j["case_id"])
        if c.get("active")!=jid or c["epoch"]!=j["payload"]["epoch"]:
            raise Conflict("superseded job")
        return j

    @app.post("/internal/jobs/{jid}/heartbeat",dependencies=worker)
    def heartbeat(jid:str,body:dict):
        live(jid,body["attempt"],body["worker"])
        return {"ok":True}

    @app.get("/internal/jobs/{jid}/artifacts/{key}",dependencies=worker)
    def get_input(jid:str,key:str,attempt:int,worker_id:str):
        job=live(jid,attempt,worker_id)
        if key not in {job["payload"].get("bundle_id"),job["payload"].get("archive_id")}:
            raise HTTPException(403)
        return FileResponse(s.read_path(key,job["case_id"]))

    @app.post("/internal/jobs/{jid}/artifacts",dependencies=worker)
    async def upload(jid:str,request:Request,attempt:int,worker_id:str):
        j=live(jid,attempt,worker_id)
        with tempfile.NamedTemporaryFile(dir=home,suffix=".upload",delete=False) as f:
            path=Path(f.name)
        try:
            size=0
            with path.open("wb") as f:
                async for chunk in request.stream():
                    size+=len(chunk)
                    if size>256*1024*1024:
                        raise HTTPException(413)
                    f.write(chunk)
            live(jid,attempt,worker_id)
            return s.ingest_file(path,j["case_id"],jid)
        finally:
            path.unlink(missing_ok=True)

    @app.post("/internal/jobs/{jid}/receipt",dependencies=worker)
    def finish(jid:str,body:dict):
        with lock:
            s.finish(jid,body["attempt"],body["receipt"],body["worker"])
        return {"ok":True}
    return app
