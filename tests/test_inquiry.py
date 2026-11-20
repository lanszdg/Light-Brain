import tempfile
import unittest
from pathlib import Path

from lightbrain.host.runner import handle
from lightbrain.inquiry import active_actions, next_action, record_answer, record_observation, validate


ACTIONS = [{"id":"surface","requires":[]},{"id":"preserve","requires":["surface"]},
           {"id":"convert","requires":["surface"]}]


def two_routes(domain, probes, values):
    return validate({"domain":domain, "probes":probes, "branches":[
        {"id":"old","action_ids":["preserve"],"predictions":{key:[v[0]] for key,v in values.items()}},
        {"id":"new","action_ids":["convert"],"predictions":{key:[v[1]] for key,v in values.items()}}]}, ACTIONS)


class InquiryTests(unittest.TestCase):
    def test_only_an_owner_can_resolve_competing_intents(self):
        inquiry = two_routes("intent", [
            {"id":"theme","question":"Theme?","choices":["blue","red"],"cost":0},
            {"id":"turn","question":"When the phone turns right, where should the arrow point?",
             "choices":["up","left"],"cost":2}], {"theme":["blue","blue"],"turn":["up","left"]})
        choice = next_action(inquiry)
        self.assertEqual(choice["probe_id"], "turn")
        implied = {"id":"r","source":"verifier","domain":"intent","probe_id":"turn","value":"left"}
        self.assertEqual(next_action(inquiry, [implied])["kind"], "ask")
        with self.assertRaises(ValueError):
            record_observation(inquiry,choice,{},"r")
        with self.assertRaises(ValueError):
            record_answer(inquiry,choice,"sideways","r")
        fact = record_answer(inquiry,choice,"left","decision-event")
        selected = next_action(inquiry, [fact])
        self.assertEqual(selected["branch_id"], "new")
        self.assertEqual(selected["rejected_branches"], {"old":["decision-event"]})
        self.assertEqual([a["id"] for a in active_actions(ACTIONS,inquiry,selected)], ["surface","convert"])
        with self.assertRaises(ValueError):
            active_actions([*ACTIONS,{"id":"bad-common","requires":["preserve"]}],inquiry,selected)

    def test_real_versioned_observation_selects_a_compatible_edit(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1],ignore_cleanup_errors=True) as work:
            ctx = {"workdir":work,"artifacts":str(Path(work)/"output")}
            build = handle("develop",{"files":{"entry.py":"print('schema-v1')\n"}},ctx)
            self.assertTrue(build["ok"],build)
            rule = {"id":"read_schema","kind":"command","argv":["python3","entry.py"],"expect_exit":0}
            q = two_routes("implementation", [
                {"id":"theme","criterion":{**rule,"id":"theme"},"read":"exit_code","cost":0},
                {"id":"schema","criterion":rule,"read":"stdout","cost":1}],
                {"theme":[0,0],"schema":["schema-v1","schema-v2"]})
            self.assertEqual(next_action(q)["reason"], "implementation_probe_requires_a_candidate")
            selected = next_action(q, candidate_sha=build["candidate_sha"])
            self.assertEqual(selected["probe_id"], "schema")
            receipt = handle("verify", {"bundle":build["bundle"], **{k:selected[k] for k in
                                      ("candidate_sha","criteria","criteria_digest")}}, ctx)
            self.assertTrue(receipt["ok"],receipt)
            with self.assertRaises(ValueError):
                record_observation(q,selected,{**receipt,"candidate_sha":"other"},"job")
            observed = record_observation(q,selected,receipt,"verifier-job")
            self.assertEqual(observed["value"], "schema-v1")
            resolution = next_action(q,[observed],build["candidate_sha"])
            self.assertEqual(resolution["branch_id"], "old")
            self.assertEqual([a["id"] for a in active_actions(ACTIONS,q,resolution)], ["surface","preserve"])
            self.assertEqual(next_action(q,[observed],"f"*40)["kind"], "observe")

    def test_overlap_stays_undecided_and_invalid_plan_rejected(self):
        q = two_routes("intent",[{"id":"scope","question":"Scope?","choices":["one","many"]}],
                       {"scope":["one","one"]})
        self.assertEqual(next_action(q)["reason"], "no_discriminating_probe")
        q["branches"][0]["action_ids"] = ["missing"]
        with self.assertRaises(ValueError):
            validate(q,ACTIONS)
