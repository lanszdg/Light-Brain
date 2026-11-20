import tempfile
import unittest
from pathlib import Path

from lightbrain.core import solve
from lightbrain.host.runner import handle
from lightbrain.implementation_methods import (
    bind_implementations, learn_implementations, materialize, promote_implementation,
)
from lightbrain.learning import checksum
from lightbrain.store import Store


class ImplementationTransferTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / ".runtime" / "implementation-tests"
        root.mkdir(parents=True,exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root,ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = Store(self.base / "state")
        self.ctx = {"workdir":self.base / "work","artifacts":self.base / "out"}

    def create(self, project, script, grants=()):
        case = self.store.create_case("brain", project, {"idea":"Multiply stdin by 2","reuse_methods":list(grants)})
        criterion = {"id":"cli", "kind":"command", "argv":["python3",script], "stdin":"12",
                     "expect_exit":0,"stdout_contains":"24"}
        return self.store.advance(case["id"],case["revision"],"criteria:"+project,"criteria",
                                  {"criteria":[criterion],"goal_version":1},criteria=[{**criterion,"version":1}])

    def call(self, kind, p):
        receipt = handle(kind,p,self.ctx)
        self.assertTrue(receipt["ok"], receipt)
        return receipt

    def deliver(self, case, developed):
        cid, criteria = case["id"],case["criteria"]
        bid = self.store.ingest_file(developed["bundle"],cid)["id"]
        sha = developed["candidate_sha"]
        base = {"goal_version":1,"candidate_sha":sha,"criteria":criteria,"criteria_digest":checksum(criteria),"bundle_id":bid}
        artifact = self.call("build",{**base,"bundle":developed["bundle"]})
        aid = self.store.ingest_file(artifact["archive"],cid)["id"]
        payloads = {"verify":base, "verify_export":{**base,"archive_id":aid}}
        case = self.store.advance(cid,case["revision"],"check:"+cid,"check",{},
                                  jobs=[{"kind":kind,"payload":p} for kind,p in payloads.items()])
        evidence = {}
        for kind in payloads:
            job = self.store.claim("trusted",[kind])
            raw = self.call(kind,{**job["payload"],"bundle":developed["bundle"],"archive":artifact["archive"],
                                  "archive_sha256":aid})
            self.store.finish(job["id"],job["attempt"],raw,"trusted")
            evidence[kind] = job["id"]
        return self.store.advance(cid,case["revision"],"delivered:"+cid,"delivery",{"state":"delivered","delivery":{
            "candidate_sha":sha,"archive_id":aid,"export_job":evidence["verify_export"]}})

    def test_materialized_source_runs_in_a_different_authorized_project(self):
        origin = self.create("source","double.py")
        code = "value = int(input())\nprint(2 * value)\n"
        origin = self.deliver(origin,self.call("develop",{"files":{"double.py":code}}))
        [method] = learn_implementations(self.store,origin)
        mid = method["id"]
        self.store.advance(origin["id"],origin["revision"],"next-version","reopen",
                           {"state":"running","goal_version":2,"delivery":None,"criteria":[]})
        # A plan cannot elevate itself to owner; only create_case's stored grant is used.
        ungranted = self.create("ungranted","utility.py")
        spoof = {**ungranted,"request":{"reuse_methods":["source/"+mid]}}
        self.assertEqual(bind_implementations(self.store,spoof,ungranted["criteria"])[0], [])

        second = self.create("target","scale.py",["source/"+mid])
        methods,facts,goals,bindings = bind_implementations(self.store,second,second["criteria"])
        result = solve({"goal":goals,"bindings":bindings,"brain_id":"brain","project_id":"target"},facts,methods)
        [reuse] = result["ready"]
        self.assertEqual(reuse["kind"], "reuse_implementation")
        files = materialize(reuse["payload"])
        self.assertEqual(files, {"scale.py":code})
        with self.assertRaises(ValueError):
            materialize({**reuse["payload"],"source_text":"print(24)"})
        start = self.call("develop",{"files":{"README.md":"An existing project without the scale command"}})
        candidate = self.call("develop",{**start,"create_only":True,"files":files})
        refused = handle("develop",{**candidate,"create_only":True,"files":{"scale.py":"print('overwrite')"}},self.ctx)
        self.assertFalse(refused["ok"])
        second = self.deliver(second,candidate)
        broad = promote_implementation(self.store,"brain","source",mid,second["id"])
        self.assertEqual(broad["scope"],{"brain_id":"brain"})
        self.assertEqual(len(broad["support_ids"]),2)
        third = self.create("third","times.py",[mid])
        self.assertTrue(bind_implementations(self.store,third,third["criteria"])[0])
        self.assertFalse(bind_implementations(self.store,third,[{"kind":"file"}])[0])
        self.store.revoke_method("brain",None,mid)
        self.assertFalse(bind_implementations(self.store,third,third["criteria"])[0])

    def test_local_import_or_runtime_dependency_cannot_be_extracted(self):
        source = self.create("dependent","entry.py")
        src = {"entry.py":"from helper import double\nprint(double(int(input())))\n",
               "helper.py":"def double(x):\n    return x*2\n"}
        source = self.deliver(source,self.call("develop",{"files":src}))
        self.assertEqual(learn_implementations(self.store,source), [])
