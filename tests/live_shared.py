"""Run an opt-in new backend commission and verify contention, ownership and restart."""
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from lightbrain.cli import Client
from lightbrain.settings import auth_token,runtime_home

PROBE="""import concurrent.futures, json, subprocess, sys, time
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
from threading import Barrier
def call(method,path,user=None):
    data=json.dumps({'user':user}).encode() if user else None
    try:
        with urlopen(Request('http://127.0.0.1:8023'+path,data=data,method=method,headers={'Content-Type':'application/json'}),timeout=2) as r:
            return r.status,r.read().decode()
    except HTTPError as e: return e.code,e.read().decode()
def start():
    p=subprocess.Popen([sys.executable,'app.py'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env={**__import__('os').environ,'PORT':'8023'})
    for _ in range(80):
        if p.poll() is not None: raise AssertionError('service stopped before ready')
        try:
            if call('GET','/health')[0]==200: return p
        except URLError: pass
        time.sleep(.05)
    p.kill();p.wait();raise AssertionError('health timeout')
def end(p): p.terminate();p.wait(timeout=2)
p=start()
try:
    barrier=Barrier(2)
    def borrow(user):
        barrier.wait();return user,call('POST','/item/borrow',user)[0]
    with concurrent.futures.ThreadPoolExecutor(2) as pool: results=list(pool.map(borrow,['A','B']))
    assert sorted(x[1] for x in results)==[201,409],results
    winner=next(u for u,s in results if s==201)
    other='B' if winner=='A' else 'A'
    assert call('POST','/item/borrow',winner)[0]==200
finally: end(p)
p=start()
try:
    status,body=call('GET','/item')
    assert status==200 and json.loads(body)['owner']==winner,body
    assert call('POST','/item/return',other)[0]==403
    assert json.loads(call('GET','/item')[1])['owner']==winner
    assert call('POST','/item/return',winner)[0]==200
    assert json.loads(call('GET','/item')[1])['owner'] is None
    print('verified: one winner; idempotent retry; persistent owner; forbidden return; released')
finally: end(p)
"""


def main():
    client=Client(os.environ.get("LB_URL","http://127.0.0.1:48717"),auth_token(runtime_home()))
    idea=("从零开发一个展品借用后端，只用 Python 标准库和 SQLite，本地运行 python3 app.py，用 PORT 环境变量监听，默认 8000。"
          "GET /health 返回 ready。全馆只有一件展品，GET /item 返回 JSON owner（空闲为 null）。"
          "POST /item/borrow 及 /item/return 的 JSON 为 {user:姓名}。同时借用只能一人成功201，别人409，"
          "本人重试200；重启后归属仍在，其他人归还403且不动数据，本人归还200并恢复空闲。不要网页。")
    checks=[{"id":"contention-and-restart","kind":"command","argv":["python3","-c",PROBE],
             "stdout_equals":"verified: one winner; idempotent retry; persistent owner; forbidden return; released\n"},
            {"id":"service","kind":"http","argv":["python3","app.py"],"port":8000,"requests":[{"path":"/health","expect_status":200,"contains":"ready"}]}]
    case=client.json("/brains/studio/cases",{"idea":idea,"criteria":checks,"project":"museum-service","event_id":"new-shared-backend-1"})
    cid=case["id"];deadline=time.monotonic()+840
    print(cid,flush=True)
    while time.monotonic()<deadline:
        case=client.json("/cases/"+cid)
        if case["state"]=="delivered": break
        if case["state"] in {"failed","cancelled","needs_input"}: raise RuntimeError(str((case["state"],case.get("error"),case.get("decisions"))))
        time.sleep(1)
    assert case["state"]=="delivered"
    jobs=client.json("/cases/"+cid+"/jobs")
    final=next(j["receipt"] for j in jobs if j["id"]==case["delivery"]["export_job"])
    assert final["ok"] and final["archive_sha256"]==case["delivery"]["archive_id"] and final["checks"][0]["observed"]["stdout"]==checks[0]["stdout_equals"]
    report={"case_id":cid,"request":idea,"source_sha":case["delivery"]["candidate_sha"],"archive_sha256":final["archive_sha256"],
            "planning_model":case["plan_model"]["model"],"coding_model":case["code_model"]["model"],"repairs":case["repairs"],
            "independent_observations":final["checks"],"jobs":[{"id":j["id"],"kind":j["kind"],"ok":j["receipt"]["ok"]} for j in jobs]}
    folder=ROOT/"交付验证";folder.mkdir(exist_ok=True)
    (folder/"shared-state.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    with client.request("/cases/"+cid+"/artifacts/"+final["archive_sha256"]) as r: archive=r.read()
    import hashlib
    assert hashlib.sha256(archive).hexdigest()==final["archive_sha256"]
    (folder/"展品借用服务.zip").write_bytes(archive)
    print(final["checks"][0]["observed"]["stdout"],flush=True)


if __name__=="__main__":
    main()
