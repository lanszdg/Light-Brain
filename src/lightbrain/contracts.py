from hashlib import sha256
import json
import re


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",",":"), allow_nan=False).encode()).hexdigest()


def criteria_list(rows):
    if not isinstance(rows,list) or not 1 <= len(rows) <= 24:
        raise ValueError("需要 1 至 24 项可独立执行的验收条件")
    ids=set()
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get("id"),str) or not row["id"] or row["id"] in ids:
            raise ValueError("验收条件 ID 应唯一")
        ids.add(row["id"])
        kind=row.get("kind")
        if kind not in {"command","http","file","json","browser"}:
            raise ValueError("未知验收条件类型")
        if kind in {"command","http"}:
            argv=row.get("argv")
            if not isinstance(argv,list) or not argv or not all(isinstance(p,str) for p in argv):
                raise ValueError("可执行验收需要 argv 参数列表")
        if kind=="http" and (not row.get("requests") or not isinstance(row.get("port"),int)):
            raise ValueError("HTTP 验收需要端口和实际请求")
        if kind=="browser":
            from .browser_checks import validate_criterion
            validate_criterion(row)
        if kind=="command" and not (row.get("stdout_contains") or "stdout_equals" in row or "json_equals" in row or row.get("expect_exit",0)!=0):
            raise ValueError("命令验收须指定可观察的输出或错误状态")
    if not any(c["kind"] in {"http","command","browser"} for c in rows):
        raise ValueError("需要至少一个实际运行正常入口的验收条件")
    return rows


def validate_plan(raw, case):
    text=case["idea"]
    quote=raw.get("intent_quote","")
    if not isinstance(quote,str) or not quote or quote not in text:
        raise ValueError("方案必须引用委托中的原话")
    if raw.get("mode")=="review":
        if not case.get("input_source"):
            raise ValueError("只读评审需要源码基线")
        if case.get("owner_criteria"):
            raise ValueError("委托指定了运行验收条件，须交付可执行候选")
        return {"mode":"review","intent_quote":quote}
    actions=raw.get("actions")
    if not isinstance(actions,list) or not 1<=len(actions)<=8:
        raise ValueError("方案需给出 1 至 8 项开发操作")
    seen=set()
    for op in actions:
        if not re.fullmatch(r"[\w.-]{1,96}",str(op.get("id",""))) or op["id"] in seen or not op.get("instruction"):
            raise ValueError("开发操作需要唯一 ID 和具体指令")
        if not isinstance(op.get("requires",[]),list) or not set(op.get("requires",[]))<=seen:
            raise ValueError("前置操作未绑定")
        seen.add(op["id"])
    criteria=criteria_list(case.get("owner_criteria") or raw.get("criteria"))
    entry=raw.get("entrypoint")
    if not isinstance(entry,list) or not entry or not all(isinstance(v,str) for v in entry):
        raise ValueError("缺少实际启动入口")
    decisions=raw.get("decisions") or []
    for d in decisions:
        if not isinstance(d,dict) or not d.get("question") or not isinstance(d.get("choices"),list) or len(d["choices"])<2:
            raise ValueError("未决取舍需有可区分的选项")
    from .inquiry import validate
    inquiry=validate(raw.get("inquiry"),actions)
    templates=[]
    if raw.get("templates"):
        from .recipes import catalog
        by_id={r["id"]:r for r in catalog()}
        for tid in dict.fromkeys(raw["templates"]):
            if tid not in by_id:
                raise ValueError("模板不在本次版本目录中")
            templates.append({k:by_id[tid][k] for k in ("id","version","digest")})
    return {"mode":"coding","title":str(raw.get("title","")),"intent_quote":quote,"approach":raw.get("approach",""),"inquiry":inquiry,
            "entrypoint":entry,"actions":actions,"criteria":criteria,"decisions":decisions[:4],
            "uses_methods":raw.get("uses_methods") or [],"templates":templates,"visual":raw.get("visual") is True}
