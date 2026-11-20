from uuid import uuid4
import time
from .contracts import criteria_list, digest, validate_plan
from .core import solve
from .store import Conflict


def op(id, kind, requires, provides, payload=None):
    return {"id":id,"job_kind":kind,"requires":requires,"provides":provides,"payload":payload or {}}


class Controller:
    def __init__(self,store):
        self.store=store

    def initialize(self,case):
        source=None
        if case["request"].get("baseline"):
            from .repository import baseline
            try:
                source=baseline(self.store,case["id"],case["request"]["baseline"])
            except (OSError,ValueError) as exc:
                return self.commit(case,"baseline_unavailable",{"state":"failed","error":str(exc)})
        return self.start(case,source)

    def start(self,case,source=None):
        req=case["request"]
        if case["state"]!="new":
            return case
        return self.commit(case,"intake",{"state":"running","idea":req["idea"],"epoch":1,"goal_version":1,
               "input_source":source,"candidate":source,"owner_criteria":req.get("criteria"),
               "facts":[],"repairs":0,"model":req.get("model"),"plan":None,"active":None})

    def commit(self,case,tag,patch,*,job=None,consume=None,criteria=None,extra=None,event_key=None):
        event_id=(event_key or (consume["id"] if consume else str(uuid4())))+":"+tag
        jobs=[]
        if job:
            jid=str(uuid4())
            job={"id":jid,**job}
            jobs=[job]
            patch={**patch,"active":jid}
        nodes=[{"id":"goal","type":"goal","version":patch.get("goal_version",case.get("goal_version",1)),
                "text":patch.get("idea",case.get("idea",case["request"]["idea"]))}]
        edges=[]
        if job:
            nodes.append({"id":jid,"type":"job","kind":job["kind"],"epoch":job["payload"]["epoch"]})
            edges.append({"id":"dispatch:"+jid,"source":"goal","target":jid,"type":"authorizes"})
            payload=job["payload"]
            selection=patch.get("finding") or {}
            if selection.get("chosen"):
                op_id="selected:"+jid
                nodes.append({"id":op_id,"type":"bound_operation","operator":selection["chosen"],
                    "routes":selection["routes"],"support":selection["supports"],"provides":payload.get("provides",[]),
                    "method_revision":payload.get("method_revision")})
                edges.append({"id":op_id+":dispatch","source":op_id,"target":jid,"type":"realized_by"})
            if payload.get("candidate_sha"):
                sha="source:"+payload["candidate_sha"]
                nodes.append({"id":sha,"type":"git_commit","sha":payload["candidate_sha"]})
                edges.append({"id":"input:"+jid,"source":sha,"target":jid,"type":"input_version"})
        if consume:
            node={"id":"receipt:"+consume["id"],"type":"receipt","ok":consume["receipt"].get("ok"),
                  "candidate_sha":consume["receipt"].get("candidate_sha")}
            nodes.append(node)
            edges.append({"id":"observed:"+consume["id"],"source":consume["id"],"target":node["id"],"type":"reported"})
            observed=consume["receipt"]
            if observed.get("candidate_sha"):
                sha="source:"+observed["candidate_sha"]
                nodes.append({"id":sha,"type":"git_commit","sha":observed["candidate_sha"]})
                edges.append({"id":"observed_version:"+consume["id"],"source":node["id"],"target":sha,"type":"concerns_version"})
                for condition in observed.get("checks",[]):
                    evidence="check:"+consume["id"]+":"+condition["id"]
                    nodes.append({"id":evidence,"type":"check_result","condition":condition["id"],"passed":condition["ok"],
                        "criteria_digest":observed.get("criteria_digest"),"goal_version":consume["payload"]["goal_version"]})
                    edges.append({"id":evidence+":source","source":node["id"],"target":evidence,"type":"observed"})
            if job:
                edges.append({"id":"reason:"+jid,"source":node["id"],"target":jid,"type":"supports"})
        if extra:
            nodes+=extra["nodes"]
            edges+=extra["edges"]
        if tag=="delivered" and patch.get("delivery",{}).get("export_job"):
            final=patch["delivery"]
            nodes.append({"id":"delivery:"+str(final["goal_version"]),"type":"delivery","sha":final["candidate_sha"],"archive_id":final["archive_id"]})
            edges.append({"id":"accepted:"+str(final["goal_version"]),"source":"receipt:"+final["export_job"],
                "target":"delivery:"+str(final["goal_version"]),"type":"accepted_by"})
        return self.store.advance(case["id"],case["revision"],event_id,tag,patch,jobs=jobs,
               graph_delta={"nodes":nodes,"edges":edges},consumed_job=consume["id"] if consume else None,
               criteria=criteria,expected_brain_rev=case["brain_revision"])

    def dispatch(self,c,kind,payload,patch=None):
        if kind in {"plan","review","develop","design"}:
            counter=c.get("model_jobs",0)
            if counter>=c["request"].get("max_model_jobs",16):
                return self.commit(c,"budget_exhausted",{"state":"failed","active":None,"error":"已用尽本委托的模型作业额度"})
            patch={**(patch or {}),"model_jobs":counter+1}
        return self.commit(c,"dispatch:"+kind,patch or {},job={"kind":kind,"payload":{
            **payload,"epoch":c["epoch"],"goal_version":c["goal_version"]}})

    def next(self,c):
        if c.get("active") or c["state"]!="running" or c.get("retry_after",0)>time.time():
            return c
        source=c.get("candidate") or {}
        common={**source,"model":c.get("model"),"idea":c["idea"],"goal_version":c["goal_version"],
                "executor":c["request"].get("executor","model")}
        if not c.get("plan"):
            catalog=self.store.list_methods(c["brain_id"],c["project_id"])
            return self.dispatch(c,"plan",{**common,"owner_criteria":c.get("owner_criteria"),"methods":[
                {"id":m["id"],"description":m.get("description"),"effects":m.get("operator",{}).get("provides")}
                for m in catalog],"feedback":c.get("planning_feedback","")})
        plan=c["plan"]
        if plan["mode"]=="review":
            return self.dispatch(c,"review",common)
        actions=plan["actions"]
        if plan.get("inquiry"):
            from .inquiry import next_action, active_actions
            reference=c.get("input_source") or source
            move=next_action(plan["inquiry"],c.get("observations",[]),reference.get("candidate_sha"))
            if move["kind"]=="observe":
                return self.dispatch(c,"inquiry_probe",{**common,**reference,**move},{"inquiry_step":move})
            if move["kind"]=="ask":
                return self.commit(c,"ask_owner",{"state":"needs_input","inquiry_step":move,"decisions":[move]})
            if move["kind"]=="unresolved":
                return self.commit(c,"inquiry_unresolved",{"state":"needs_input","inquiry_step":move,
                    "decisions":[{"question":"请明确这个取舍，或补充可观察的依据。","reason":move["reason"]}]})
            actions=active_actions(actions,plan["inquiry"],move)
            if c.get("inquiry_step")!=move:
                return self.commit(c,"branch_selected",{"inquiry_step":move,"decisions":[]})
        if c.get("input_source") and not c.get("baseline_job"):
            return self.dispatch(c,"verify",{**common,**c["input_source"],"purpose":"baseline",
                "criteria":c["criteria"],"criteria_digest":digest(c["criteria"]),"provides":[]})
        code_effects=["code:"+a["id"] for a in actions]
        ops=[op("develop:"+a["id"],"develop",["code:"+x for x in a.get("requires",[])],["code:"+a["id"]],
                {"instruction":a["instruction"]}) for a in actions]
        from .implementation_methods import bind_implementations
        assets,asset_facts,asset_goals,asset_bindings=bind_implementations(self.store,c,c["criteria"],actions)
        for operation in ops:
            operation["requires"]+=asset_goals
        required=[*code_effects]
        starters=[]
        if plan.get("templates") and not c.get("input_source"):
            starters.append(op("template","template",[],["starter"],{"templates":plan["templates"]}))
        if plan.get("visual") and not c.get("input_source"):
            starters.append(op("design","design",["starter"] if starters else [],["sketch"]))
        if starters:
            for operation in ops:
                operation["requires"]+=starters[-1]["provides"]
            ops=starters+ops
        if c.get("fault"):
            repair_key="repair:"+str(c["repairs"])
            ops.append(op(repair_key,"develop",required,[repair_key],{"instruction":"修复独立验收发现的实际问题","failure":c["fault"]}))
            required=[repair_key]
        methods=[];more_facts=[];method_goals=[];bindings={}
        uses=[*plan["uses_methods"],*(c["request"].get("reuse_methods") or [])]
        if uses:
            from .learning import bind_methods
            methods,more_facts,method_goals,bindings=bind_methods(self.store,c,c["criteria"],uses)
        ops += [op("verify","verify",required,["checked_candidate"]),
                op("build","build",["checked_candidate",*method_goals],["archive"]),
                op("export","verify_export",["archive"],["standalone"])]
        target="standalone"
        if c["request"].get("simulate_release"):
            ops.append(op("stage","release",["standalone"],["staged"]))
            target="staged"
        problem={"goal":[target],"operators":ops,"brain_id":c["brain_id"],"project_id":c["project_id"],
                 "permissions":["verify","read","sandbox"],"bindings":{**bindings,**asset_bindings}}
        found=solve(problem,c["facts"]+list(more_facts)+list(asset_facts),[*methods,*assets],budget=1024)
        if found["satisfied"]:
            delivery={"candidate_sha":source["candidate_sha"],"bundle_id":source["bundle_id"],"archive_id":c["archive_id"],
                      "entrypoint":plan["entrypoint"],"verification_job":c["verified_job"],"export_job":c["export_job"],
                      "criteria_digest":digest(c["criteria"]),"goal_version":c["goal_version"]}
            if c.get("has_preview"):
                delivery["preview_url"]="/cases/"+c["id"]+"/preview/"
            if c.get("simulation"):
                delivery["simulation"]=c["simulation"]
            return self.commit(c,"delivered",{"state":"delivered","delivery":delivery})
        if not found["ready"]:
            return self.commit(c,"unsolved",{"state":"failed","error":"缺少可执行输入","finding":found})
        action=found["ready"][0]
        p={**common,"criteria":c["criteria"],"criteria_digest":digest(c["criteria"]),"entrypoint":plan["entrypoint"],
           "approach":plan["approach"],"uses":action["id"],"provides":action["provides"],
           **action["payload"]}
        if action["kind"]=="develop" and c.get("baseline_fault") and "failure" not in p:
            p["failure"]=c["baseline_fault"]
        if c.get("protected_files"):
            p["protected_files"]=c["protected_files"]
        if action["kind"] in {"method_verify","reuse_implementation"}:
            if action["kind"]=="method_verify":
                p["criteria_digest"]=digest(p["criteria"])
            p["method_revision"]=action["method_revision"]
        if c.get("archive_id"):
            p["archive_id"]=c["archive_id"]
        return self.dispatch(c,action["kind"],p,{"finding":{"routes":found["routes"],"chosen":action["id"],
                             "supports":action["support_ids"],"gaps":found["gaps"]}})

    def consume(self,c,job):
        result=job["receipt"]; p=job["payload"]; kind=job["kind"]
        if c.get("active")!=job["id"] or p["epoch"]!=c.get("epoch"):
            return self.commit(c,"superseded",{},consume=job)
        if kind=="release" and p.get("purpose")=="channel_change":
            ok=(result.get("ok") is True and result.get("candidate_sha")==p["candidate_sha"]
                and result.get("archive_sha256")==p["archive_id"] and result.get("public") is False)
            patch={"active":None,"channel_receipt":{"ok":ok,"job":job["id"],"candidate_sha":p["candidate_sha"],
                "archive_id":p["archive_id"],"action":p["action"],"deployment_id":result.get("deployment_id"),
                "error":result.get("error")}}
            return self.commit(c,"channel:"+p["action"],patch,consume=job)
        if kind in {"method_verify","reuse_implementation"}:
            from .implementation_methods import bind_implementations
            from .learning import bind_methods
            if kind=="reuse_implementation":
                authorized,*_=bind_implementations(self.store,c,c["criteria"])
            else:
                authorized,*_=bind_methods(self.store,c,c["criteria"],[*c["plan"]["uses_methods"],*(c["request"].get("reuse_methods") or [])])
            expected=p["method_revision"]
            if not any(m["id"]==expected["id"] and m["revision"]==expected["revision"]
                       and m["implementation_digest"]==expected["digest"] for m in authorized):
                return self.commit(c,"method_withdrawn",{"active":None},consume=job)
        if kind=="inquiry_probe":
            from .inquiry import record_observation
            fact=record_observation(c["plan"]["inquiry"],c["inquiry_step"],result,job["id"])
            return self.commit(c,"branch_observation",{"active":None,"observations":c.get("observations",[])+[fact]},consume=job)
        if kind=="verify" and p.get("purpose")=="baseline":
            if (result.get("candidate_sha")!=p["candidate_sha"] or result.get("criteria_digest")!=p["criteria_digest"]
                    or not result.get("checks")):
                return self.commit(c,"baseline_unavailable",{"active":None,"state":"failed",
                    "error":result.get("error","未取得基线实测")},consume=job)
            failed=[r for r in result["checks"] if not r["ok"]]
            return self.commit(c,"baseline_checked",{"baseline_job":job["id"],"active":None,
                "baseline_fault":{"candidate_sha":p["candidate_sha"],"checks":failed} if failed else None},consume=job)
        patch={"active":None}
        frozen=None; extra=None
        ok=result.get("ok") is True
        if ok and kind in {"verify","method_verify","verify_export","build","release"}:
            ok=bool(c.get("candidate")) and result.get("candidate_sha")==c["candidate"]["candidate_sha"]
        if ok and kind in {"verify","method_verify","build"}:
            ok=result.get("bundle_sha256")==p.get("bundle_id")
        if ok and kind in {"verify_export","release"}:
            ok=result.get("archive_sha256")==p.get("archive_id")
        if kind in {"verify","method_verify","verify_export"}:
            ok=ok and result.get("criteria_digest")==p["criteria_digest"]
            checks=result.get("checks",[])
            ok=ok and [x.get("id") for x in checks]==[x["id"] for x in p["criteria"]] and all(x.get("ok") is True for x in checks)
        if not ok:
            if result.get("retryable") and kind in {"plan","develop","design","review"} and c.get("transient_failures",0)<2:
                count=c.get("transient_failures",0)+1
                patch.update(transient_failures=count,retry_after=time.time()+2**count)
            elif result.get("retryable"):
                patch.update(state="failed",error=result.get("error","模型暂不可用"))
            elif kind=="plan" and c.get("plan_attempts",0)<1:
                patch.update(plan_attempts=1,planning_feedback=result.get("error","生成完整可靠的 JSON 方案"))
            elif kind in {"verify","method_verify","verify_export"} and result.get("checks") and c["repairs"]<3:
                # Invalidate all observations referring to the old binary. Keep the development history.
                patch.update(repairs=c["repairs"]+1,fault={"checks":result["checks"],"error":result.get("error"),
                    "candidate_sha":p["candidate_sha"]},facts=[f for f in c["facts"] if f["key"].startswith(("code:","implemented:")) or f["key"] in {"sketch","starter"}],
                    archive_id=None)
            else:
                patch.update(state="failed",error=result.get("error") or "回执或验收结果与冻结条件不符")
            return self.commit(c,"failed:"+kind,patch,consume=job)
        patch.update(transient_failures=0,retry_after=0)
        if kind=="plan":
            try:
                plan=validate_plan(result["proposal"],c)
            except (ValueError,KeyError,TypeError) as exc:
                count=c.get("plan_attempts",0)+1
                patch.update(plan_attempts=count,planning_feedback=str(exc))
                if count>=2:
                    patch.update(state="failed",error=str(exc))
                return self.commit(c,"invalid_plan",patch,consume=job)
            patch.update(plan=plan,plan_model=result.get("model"))
            if plan.get("decisions"):
                patch.update(state="needs_input",decisions=plan["decisions"])
            if plan["mode"]=="coding":
                patch["criteria"]=plan["criteria"]
                frozen=[{**v,"version":c["goal_version"],"authority":"owner" if c.get("owner_criteria") else "inferred",
                         "intent_quote":plan["intent_quote"]} for v in plan["criteria"]]
                extra=self.plan_graph(plan,c["goal_version"])
        elif kind=="review":
            patch.update(state="delivered",delivery={"review":result["review"],"candidate_sha":p["candidate_sha"]})
        else:
            facts=c["facts"]+[{"id":job["id"]+":"+key,"key":key,"source":"receipt"} for key in p["provides"]]
            patch["facts"]=facts
            if kind in {"develop","reuse_implementation","design","template"}:
                artifact=result["bundle_id"]
                if result.get("bundle_sha256")!=artifact:
                    raise ValueError("候选包摘要不符")
                from .repository import validate_candidate
                validate_candidate(self.store,c["id"],artifact,result["candidate_sha"],(c.get("candidate") or {}).get("candidate_sha"))
                patch.update(candidate={"bundle_id":artifact,"candidate_sha":result["candidate_sha"]})
                if kind=="reuse_implementation":
                    patch["protected_files"]={**c.get("protected_files",{}),p["target_path"]:p["source_sha256"]}
                elif kind=="design":
                    self.store.read_path(result["archive_id"],c["id"])
                    patch.update(design_id=result["archive_id"],design_sha=result["candidate_sha"])
                elif kind=="template":
                    patch["template_receipts"]=result["template_receipts"]
                else:
                    patch["code_model"]=result.get("model")
            elif kind=="verify":
                patch["verified_job"]=job["id"]
            elif kind=="build":
                self.store.read_path(result["archive_id"],c["id"])
                patch.update(archive_id=result["archive_id"],has_preview=result.get("has_preview",False))
            elif kind=="verify_export":
                patch["export_job"]=job["id"]
            elif kind=="release":
                patch["simulation"]={"job":job["id"],"mode":"simulated","candidate_sha":p["candidate_sha"],
                    "deployment_id":result.get("deployment_id"),"archive_sha256":result.get("archive_sha256"),"active":result.get("active")}
        return self.commit(c,"received:"+kind,patch,consume=job,criteria=frozen,extra=extra)

    @staticmethod
    def plan_graph(plan,version):
        nodes=[];edges=[]
        prefix=f"v{version}:"
        for a in plan["actions"]:
            node=prefix+"action:"+a["id"]
            nodes.append({"id":node,"type":"operator","instruction":a["instruction"]})
            edges.append({"id":node+":goal","source":"goal","target":node,"type":"intended"})
            for req in a.get("requires",[]):
                edges.append({"id":node+":needs:"+req,"source":prefix+"action:"+req,"target":node,"type":"requires"})
        for c in plan["criteria"]:
            node=prefix+"criterion:"+c["id"]
            nodes.append({"id":node,"type":"criterion","condition":c})
            edges.append({"id":node+":goal","source":"goal","target":node,"type":"acceptance"})
        if plan.get("inquiry"):
            for b in plan["inquiry"]["branches"]:
                node=prefix+"branch:"+b["id"]
                nodes.append({"id":node,"type":"representation","domain":plan["inquiry"]["domain"],"predictions":b["predictions"]})
                edges.append({"id":node+":goal","source":"goal","target":node,"type":"interprets"})
                for aid in b.get("action_ids",[]):
                    edges.append({"id":node+":"+aid,"source":node,"target":prefix+"action:"+aid,"type":"selects"})
        return {"nodes":nodes,"edges":edges}

    def tick(self):
        for job in self.store.pending_receipts():
            try:
                self.consume(self.store.get_case(job["case_id"]),job)
            except Conflict:
                continue
            except (ValueError,KeyError,TypeError) as exc:
                case=self.store.get_case(job["case_id"])
                self.commit(case,"invalid_receipt",{"state":"failed","active":None,"error":str(exc)},consume=job)
        for c in self.store.list_cases():
            try:
                if c["state"]=="new":
                    self.initialize(c)
                elif c["state"]=="running":
                    self.next(c)
                elif c["state"]=="delivered" and c.get("export_job") and c.get("learning_revision")!=2:
                    from .learning import learn_from_case
                    from .implementation_methods import learn_implementations
                    learned=[*learn_from_case(self.store,c),*learn_implementations(self.store,c)]
                    self.commit(self.store.get_case(c["id"]),"learned",{"learning":[m["id"] for m in learned],"learning_revision":2})
            except Conflict:
                continue
            except (ValueError,TypeError,KeyError) as exc:
                key="learning" if c["state"]=="delivered" else "error"
                update={key:{"error":str(exc)},"learning_revision":2} if key=="learning" else {"state":"failed","error":str(exc)}
                self.commit(self.store.get_case(c["id"]),"unresolved",update)
