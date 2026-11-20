from http.client import HTTPConnection
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import urlsplit

from lightbrain.contracts import digest
from lightbrain.host.runner import handle
from lightbrain.live_preview import PreviewManager
from lightbrain.store import Store


APP = '''from http.server import BaseHTTPRequestHandler, HTTPServer
import os
counter = 0
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if self.path == "/env":
            self.wfile.write(str(bool(os.environ.get("LB_TOKEN"))).encode())
        else:
            self.wfile.write(str(counter).encode())
    def do_POST(self):
        global counter
        counter += int(self.rfile.read(int(self.headers["Content-Length"])))
        self.do_GET()
HTTPServer(("127.0.0.1", 8124), Handler).serve_forever()
'''


class LivePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root=Path(__file__).resolve().parents[1] / ".runtime" / "live-preview-tests"
        root.mkdir(parents=True, exist_ok=True)
        cls.temp=tempfile.TemporaryDirectory(dir=root,ignore_cleanup_errors=True)
        cls.addClassCleanup(cls.temp.cleanup)
        path=Path(cls.temp.name)
        cls.store=Store(path/"state")
        cls.manager=PreviewManager(cls.store)
        cls.addClassCleanup(cls.manager.close)
        cls.case=cls.store.create_case("b","p",{"idea":"interactive counter"})
        case=cls.case
        rule={"id":"counter","kind":"http","argv":["python3","app.py"],"port":8124,
              "requests":[{"method":"GET","path":"/","expect_status":200,"contains":"0"}]}
        ctx={"workdir":str(path/"work"),"artifacts":str(path/"out")}
        def run(kind,p):
            result=handle(kind,p,ctx)
            if not result["ok"]:
                raise RuntimeError(result)
            return result
        dev=run("develop",{"files":{"app.py":APP}})
        archive=run("build",dev)
        aid=cls.store.ingest_file(archive["archive"],case["id"])["id"]
        p={"archive_id":aid,"candidate_sha":dev["candidate_sha"],"criteria":[rule],"criteria_digest":digest([rule]),
           "goal_version":1}
        c=cls.store.advance(case["id"],case["revision"],"freeze","freeze",{"criteria":[rule]},criteria=[rule],
                            jobs=[{"kind":"verify_export","payload":p}])
        job=cls.store.claim("test",["verify_export"])
        receipt=run("verify_export",{**p,"archive":archive["archive"],"archive_sha256":aid})
        cls.store.finish(job["id"],job["attempt"],receipt,"test")
        cls.cid=case["id"]
        cls.store.advance(cls.cid,c["revision"],"done","delivery",{"state":"delivered","delivery":{
            "candidate_sha":dev["candidate_sha"],"archive_id":aid,"export_job":job["id"],"entrypoint":rule["argv"]}})

    def tearDown(self):
        self.manager.stop(self.cid)

    def test_dynamic_state_and_origin_controls(self):
        info=self.manager.start(self.cid,25)
        url=urlsplit(info["url"])
        def request(method,path,headers=None,body=None):
            conn=HTTPConnection(url.hostname,url.port,timeout=8)
            try:
                conn.request(method,path,body=body,headers=headers or {})
                r=conn.getresponse()
                return r.status,dict(r.getheaders()),r.read()
            finally:
                conn.close()
        self.assertEqual(request("GET","/")[0],403)
        status,headers,_=request("GET",url.path)
        self.assertEqual(status,303)
        self.assertEqual(request("GET",url.path)[0],403)
        cookie={"Cookie":headers["Set-Cookie"].split(";",1)[0]}
        self.assertEqual(request("GET","/",cookie)[2],b"0")
        status,headers,response=request("POST","/increment",cookie,b"6")
        self.assertEqual((status,response),(200,b"6"))
        self.assertIn("connect-src 'self'",headers["Content-Security-Policy"])
        self.assertEqual(request("GET","/counter",cookie)[2],b"6")
        self.assertEqual(request("GET","/env",cookie)[2],b"False")
        self.assertEqual(request("POST","/",{**cookie,"Origin":"https://elsewhere.invalid"},b"9")[0],403)
        self.assertEqual(request("POST","/",{**cookie,"Content-Type":"multipart/form-data"},b"9")[0],415)
        self.assertIn(request("GET","http://127.0.0.1/",cookie)[0],(403,502))
        self.assertTrue(self.manager.stop(self.cid)=={"running":False})

    def test_preview_times_out_and_rejects_unverified_case(self):
        new=self.store.create_case("b","p",{"idea":"pending"})
        with self.assertRaises(ValueError):
            self.manager.start(new["id"])
        self.assertTrue(self.manager.start(self.cid,1)["running"])
        deadline=time.monotonic()+4
        while self.manager.status(self.cid)["running"] and time.monotonic()<deadline:
            time.sleep(.1)
        self.assertFalse(self.manager.status(self.cid)["running"])
