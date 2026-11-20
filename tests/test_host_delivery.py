from pathlib import Path
import hashlib
import tempfile
import unittest

from lightbrain.host.git_workspace import git
from lightbrain.host.runner import criteria_digest, handle


ROOT = Path(__file__).resolve().parents[1] / ".runtime" / "host-tests"


class HostDelivery(unittest.TestCase):
    def setUp(self):
        ROOT.mkdir(parents=True,exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT,ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ctx = {"case_id":"test", "workdir":str(self.root/"jobs"), "artifacts":str(self.root/"assets")}

    def run_ok(self,kind,payload):
        r = handle(kind,payload,self.ctx)
        self.assertTrue(r["ok"],r)
        return r

    def test_failure_repair_standalone_and_staging_rollback(self):
        rule = [{"id":"delta", "kind":"command","argv":["python","delta.py"],
                 "stdin":"10 3","stdout_equals":"7\n"}]
        v1 = self.run_ok("develop",{"files":{"delta.py":"x,y=map(int,input().split());print(x+y)"}})
        failed = handle("verify",{**v1,"criteria":rule},self.ctx)
        self.assertFalse(failed["ok"])
        self.assertEqual("13\n",failed["checks"][0]["observed"]["stdout"])
        v2 = self.run_ok("develop",{**v1,"files":{"delta.py":"x,y=map(int,input().split());print(x-y)"}})
        self.assertEqual(v1["candidate_sha"],v2["base_sha"])
        frozen = {"criteria":rule,"criteria_digest":criteria_digest(rule)}
        self.run_ok("verify",{**v2,**frozen})
        build = self.run_ok("build",v2)
        proof = self.run_ok("verify_export",{**build,**frozen})
        self.assertEqual(v2["candidate_sha"],proof["candidate_sha"])
        self.assertEqual("bubblewrap",proof["environment"]["executor"])
        self.assertFalse(handle("verify_export",{**build,**frozen,"archive_sha256":"0"*64},self.ctx)["ok"])
        r1 = self.run_ok("release",build)
        r2 = self.run_ok("release",build)
        self.assertEqual(r1["deployment_id"],r2["deployment_id"])
        old = self.run_ok("build",v1)
        rollback = self.run_ok("release",{**old, "operation_id":"rollback-at-user-request"})
        self.assertTrue(rollback["active"])
        self.assertEqual(rollback["candidate_sha"],v1["candidate_sha"])
        self.assertFalse(self.run_ok("release",build)["active"])

    def test_http_works_on_loopback_without_host_files(self):
        app = ("from http.server import BaseHTTPRequestHandler,HTTPServer\n"
               "from pathlib import Path\n"
               "class H(BaseHTTPRequestHandler):\n"
               " def do_GET(self):\n"
               "  self.send_response(200);self.end_headers();self.wfile.write(b'false' if Path('/mnt/d').exists() else b'true')\n"
               "HTTPServer(('127.0.0.1',8147),H).serve_forever()\n")
        v = self.run_ok("develop",{"files":{"main.py":app,"index.html":"<main>Sample</main>"}})
        preview = self.run_ok("preview",v)
        self.assertFalse((Path(preview["preview_dir"])/".git").exists())
        spec = {"id":"serve","kind":"http","argv":["python","main.py"],"port":8147,
                "requests":[{"method":"GET","path":"/","json_equals":True}]}
        self.run_ok("verify",{**v,"criteria":[spec]})
        self.assertFalse(handle("verify",{**v,"criteria":[spec],"criteria_digest":"123"},self.ctx)["ok"])
        self.assertFalse(handle("verify",{**v,"criteria":[spec], "candidate_sha":v["base_sha"]},self.ctx)["ok"])

    def test_true_baseline_dirty_changes_and_forbidden_paths(self):
        original = self.root/"input"
        original.mkdir()
        git(original,"init","--initial-branch=main")
        (original/"keep.txt").write_text("committed")
        git(original,"add","--all")
        git(original,"commit","-m","baseline")
        (original/"keep.txt").write_text("user edits")
        (original/"user.txt").write_text("untracked")
        original_status = git(original,"status","--porcelain")
        v = self.run_ok("develop",{"baseline":str(original),"files":{"version.json":'{"v":2}'}})
        self.assertEqual(original_status,git(original,"status","--porcelain"))
        self.run_ok("verify",{**v,"criteria":[
            {"id":"keep","kind":"file","path":"keep.txt","contains":"user edits"},
            {"id":"untracked","kind":"file","path":"user.txt","contains":"untracked"},
            {"id":"version","kind":"json","path":"version.json","pointer":"/v","equals":2}]})
        self.assertFalse(handle("develop",{**v,"files":{"../escape":"no"}},self.ctx)["ok"])

    def test_unapproved_provider_or_executor_cannot_be_invoked(self):
        v=self.run_ok("develop",{"files":{"index.html":"preview"}})
        build=self.run_ok("build",v)
        self.assertFalse(handle("release",build,{**self.ctx,"release_mode":"github-draft"})["ok"])
        self.assertFalse(handle("develop",{"task":"x","engine":"openhands"},self.ctx)["ok"])

    def test_reused_files_have_replayable_ownership(self):
        v=self.run_ok("develop",{"files":{"Widget/model.py":"answer=42\n"}})
        h=hashlib.sha256(b"answer=42\n").hexdigest()
        for changes in ({"widget/new":"new"},{"Widget/model.py":"other"},{"Widget":"file"},
                        {"a/b":"one","A/c":"two"}):
            self.assertFalse(handle("develop",{**v,"create_only":True,"files":changes},self.ctx)["ok"])
        self.run_ok("develop",{**v,"create_only":True,"files":{"Widget/more":"yes"}})
        guarded={**v,"protected_files":{"Widget/model.py":h}}
        self.assertFalse(handle("develop",{**guarded,"delete":["Widget/model.py"]},self.ctx)["ok"])
        self.run_ok("develop",{**guarded,"files":{"example.py":"print(42)"}})
