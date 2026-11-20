from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from lightbrain.store import Conflict, NotFound, Store


class StoreTests(TestCase):
    def setUp(self):
        tmp=TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.store=Store(self.root)
        self.case=self.store.create_case("alpha","red",{"goal":"an editable score"},"user-1")
        self.cid=self.case["id"]

    def test_atomic_idempotent_events(self):
        self.assertEqual(self.cid,self.store.create_case("alpha","red",{"goal":"an editable score"},"user-1")["id"])
        with self.assertRaises(Conflict):
            self.store.create_case("alpha","red","different","user-1")
        jobs=[{"kind":"develop","id":"develop-id"}]
        for _ in range(2):
            self.store.advance(self.cid,1,"work","planned",{"state":"working"},jobs)
        self.assertEqual(len(self.store.list_jobs(self.cid)),1)
        with self.assertRaises(Conflict):
            self.store.advance(self.cid,2,"another","update",{"state":"wrong"},jobs)
        with self.assertRaises(Conflict):
            self.store.advance(self.cid,1,"stale","update",{})
        self.assertEqual(self.store.get_case(self.cid)["state"],"working")
        self.assertEqual([r["revision"] for r in self.store.list_events(self.cid)], [1,2])

    def test_claim_finish_and_resume_after_reboot(self):
        self.store.advance(self.cid,1,"work","planned",{},[{"kind":"develop"}])
        def claim(worker):
            return self.store.claim(worker,["develop"])
        with ThreadPoolExecutor(max_workers=6) as pool:
            claims=list(pool.map(claim,(str(i) for i in range(6))))
        [job]=[x for x in claims if x]
        self.assertIsNone(self.store.claim("other",["verify"]))
        with patch("lightbrain.store.database.time.time",return_value=job["lease_until"]+10):
            with self.assertRaises(Conflict):
                self.store.heartbeat(job["id"],1)
            renewed=self.store.claim("new-worker",lease_seconds=100)
            self.assertEqual(renewed["attempt"],2)
            with self.assertRaises(Conflict):
                self.store.finish(job["id"],1,{"ok":True})
            self.store.finish(job["id"],2,{"ok":True},worker="new-worker")
            self.store.finish(job["id"],2,{"ok":True},worker="new-worker")
            with self.assertRaises(Conflict):
                self.store.finish(job["id"],2,{"ok":False})
        store=Store(self.root)
        [receipt]=store.pending_receipts()
        self.assertEqual(receipt["receipt"],{"ok":True})
        store.advance(self.cid,2,"accepted","receipt",{"state":"accepted"},consumed_job=job["id"])
        self.assertEqual(store.pending_receipts(),[])
        with self.assertRaises(Conflict):
            store.advance(self.cid,3,"reuse","receipt",{},consumed_job=job["id"])

    def test_graph_history_scope_and_frozen_criteria(self):
        nodes=[{"id":"a"},{"id":"b"}]
        edge={"id":"e","source":"a","target":"b","type":"enables"}
        first={"id":"visible","version":1,"expect":"title appears"}
        self.store.advance(self.cid,1,"g1","fact",{},graph_delta={"nodes":nodes,"edges":[edge]},criteria=[first])
        with self.assertRaises(Conflict):
            self.store.advance(self.cid,2,"bad","change",{},criteria=[{**first,"expect":"anything"}])
        with self.assertRaises(Conflict):
            self.store.advance(self.cid,2,"bad","remove",{},graph_delta={"remove_nodes":["a"]})
        changed={"id":"a","label":"updated"}
        self.store.advance(self.cid,2,"g2","fact",{},graph_delta={"nodes":[changed],"remove_edges":["e"]})
        self.assertEqual(self.store.graph_snapshot(self.cid,2)["edges"],[edge])
        self.assertEqual(self.store.graph_snapshot(self.cid)["nodes"][0],changed)
        self.assertEqual(self.store.graph_snapshot(self.cid)["edges"],[])
        self.assertEqual(self.store.list_criteria(self.cid)[0]["frozen_at"],2)
        separate=self.store.create_case("beta","red",{},"user-1")
        self.assertEqual(self.store.graph_snapshot(separate["id"])["nodes"],[])

    def test_artifacts_restrict_case_access(self):
        file=self.root / "file"
        file.write_bytes(b"hello")
        other=self.store.create_case("alpha","blue",{})
        obj=self.store.ingest_file(file,self.cid)
        self.assertEqual(obj["sha256"],sha256(b"hello").hexdigest())
        self.assertEqual(self.store.read_path(obj["sha256"],self.cid).read_bytes(),b"hello")
        with self.assertRaises(NotFound):
            self.store.read_path(obj["sha256"],other["id"])
        with self.assertRaises(NotFound):
            self.store.read_path("../file")
        self.store.ingest_file(file,other["id"])
        self.assertEqual(len(list((self.root/"objects").rglob(obj["sha256"]))),1)

    def test_method_versions_project_scope_and_snapshot_cas(self):
        m=self.store.register_method("alpha","red",{"id":"replay","revision":1})
        self.assertEqual(m,self.store.register_method("alpha","red",m))
        self.assertEqual(self.store.list_methods("alpha","blue"),[])
        self.assertEqual(self.store.list_methods("beta","red"),[])
        self.store.register_method("alpha",None,{"id":"shared","revision":1})
        self.assertEqual(self.store.list_methods("alpha","blue")[0]["id"],"shared")
        self.assertEqual(self.store.revoke_method("alpha","red","replay")["revision"],2)
        self.assertEqual([m["id"] for m in self.store.list_methods("alpha","red")],["shared"])
        self.assertEqual(len(self.store.method_history("alpha","red","replay")),2)
        with self.assertRaises(Conflict):
            self.store.advance(self.cid,1,"stale","decision",{},expected_brain_rev=self.case["brain_revision"])
        with self.assertRaises(Conflict):
            self.store.register_method("alpha","blue",m)
