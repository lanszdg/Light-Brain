"""Real Git bundles and independently sandboxed target processes for transfer tests."""

from pathlib import Path
import tempfile
import unittest

from lightbrain.core import solve
from lightbrain.host.runner import handle
from lightbrain.learning import bind_methods, checksum, learn_from_case, promote
from lightbrain.store import Store


class LearningTests(unittest.TestCase):
    def setUp(self):
        run = Path(__file__).resolve().parents[1] / ".runtime" / "learning-tests"
        run.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=run, ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.context = {"workdir":self.root / "work", "artifacts":self.root / "artifacts"}

    def project(self, name, script):
        case = self.store.create_case("brain", name, {"idea":"Double an integer supplied on stdin"})
        c = {"id":"double", "kind":"command", "argv":["python3", script],
             "stdin":"12", "expect_exit":0, "stdout_contains":"24"}
        return self.store.advance(case["id"],case["revision"],name,"freeze", {"criteria":[c],"goal_version":1},
                                  criteria=[{**c,"version":1,"authority":"owner","intent_quote":"Double an integer"}])

    def run_candidate(self, case, code):
        rule = case["criteria"][0]
        script = rule["argv"][1]
        candidate = handle("develop", {"files":{script:code}}, self.context)
        self.assertTrue(candidate["ok"], candidate)
        bundle = self.store.ingest_file(candidate["bundle"], case["id"])["id"]
        data = {"criteria":[rule],"goal_version":1,"criteria_digest":checksum([rule]),
                "candidate_sha":candidate["candidate_sha"],"bundle_id":bundle}
        queued = self.store.advance(case["id"],case["revision"],"start:"+bundle,"verify",
                                    {"candidate":{"candidate_sha":data["candidate_sha"],"bundle_id":bundle}},
                                    jobs=[{"kind":"verify","payload":data}])
        job = self.store.claim("independent", ["verify"])
        result = handle("verify", {**data,"bundle":self.store.read_path(bundle,case["id"])}, self.context)
        self.assertTrue(result.get("checks") and result["checks"][0].get("observed"), result)
        done = self.store.finish(job["id"], job["attempt"], result, "independent")
        final = self.store.advance(case["id"], queued["revision"], "received:"+bundle, "received",
                                   {"state":"delivered" if result["ok"] else "repair"}, consumed_job=job["id"])
        return final, done

    def test_observed_repair_becomes_executable_in_a_new_project(self):
        source = self.project("first", "double.py")
        source, failed = self.run_candidate(source, "print(int(input()))\n")
        self.assertFalse(failed["receipt"]["ok"])
        self.assertEqual(learn_from_case(self.store,source), [])
        source, fixed = self.run_candidate(source, "print(2 * int(input()))\n")
        self.assertTrue(fixed["receipt"]["ok"])
        [method] = learn_from_case(self.store, source)
        mid = method["id"]
        self.assertIn(failed["id"] + "/double", method["regressions_seen"])
        self.assertNotIn(failed["id"] + "/double", method["counterexamples"])

        target = self.project("second", "scale.py")
        with self.assertRaises(ValueError):
            promote(self.store,"brain","first",mid,target["id"])
        target, ran = self.run_candidate(target, "number = int(input())\nprint(number + number)\n")
        self.assertTrue(ran["receipt"]["ok"])
        promoted = promote(self.store,"brain","first",mid,target["id"])
        self.assertEqual(promoted["scope"], {"brain_id":"brain"})
        self.assertEqual(len(promoted["support_ids"]), 2)
        # The new contract covers input 3, but the learned method additionally
        # exercises the earlier failing vector 12 on the new entrypoint.
        new_rule = {**target["criteria"][0], "stdin":"3","stdout_contains":"6"}
        methods, facts, goals, bindings = bind_methods(self.store, target, [new_rule], [mid])
        found = solve({"brain_id":"brain","project_id":"second","goal":goals,"bindings":bindings},
                      facts+["checked_candidate"], methods)
        [action] = found["ready"]
        self.assertEqual(action["kind"], "method_verify")
        [replay] = action["payload"]["criteria"]
        self.assertEqual(replay["argv"][1], "scale.py")
        self.assertEqual(replay["stdin"], "12")
        self.assertEqual(replay["stdout_contains"], "24")
        p = ran["payload"]
        checked = handle("verify", {"criteria":[replay],"bundle":self.store.read_path(p["bundle_id"],target["id"]),
                                    "candidate_sha":p["candidate_sha"]}, self.context)
        self.assertTrue(checked["ok"], checked)
        self.assertEqual(bind_methods(self.store, target, [{"kind":"file"}], [mid])[0], [])
        unrelated = {**target, "request":{"idea":"Square an integer"}}
        self.assertEqual(bind_methods(self.store, unrelated, [new_rule], [mid])[0], [])
        self.store.revoke_method("brain", None, mid)
        self.assertEqual(bind_methods(self.store,target,[new_rule],[mid])[0], [])

    def test_unsupported_or_invented_observation_cannot_qualify(self):
        source = self.project("wrong", "double.py")
        source, failed = self.run_candidate(source, "print('broken')\n")
        self.assertFalse(failed["receipt"]["ok"])
        job = {"kind":"verify", "payload":failed["payload"]}
        new = self.store.advance(source["id"],source["revision"],"forged","verify",{},jobs=[job])
        lease = self.store.claim("verifier",["verify"])
        fake = {**failed["receipt"],"ok":True,
                "checks":[{**failed["receipt"]["checks"][0], "ok":True}]}
        self.store.finish(lease["id"], lease["attempt"], fake,"verifier")
        self.assertEqual(learn_from_case(self.store, new), [])
