import unittest
from copy import deepcopy

from lightbrain.core import qualify_method, solve


def op(name, inputs, outputs, **options):
    return {"id": name, "job_kind": "run", "requires": inputs, "provides": outputs,
            "payload": {"entry": name}, **options}


def proposal(*ops, goal="delivered", **options):
    return {"goal": [goal], "operators": list(ops), **options}


def jobs(result):
    return [o["id"] for o in result["ready"]]


class DerivationTests(unittest.TestCase):
    def test_composition_follows_evidence_not_predictions(self):
        plan = proposal(op("final", ["head"], ["delivered"]), op("implement", ["brief"], ["head"]))
        a = solve(plan, ["brief", {"key":"head", "source":"prediction"}])
        self.assertEqual(a["routes"][0]["steps"], ["implement", "final"])
        self.assertEqual(jobs(a), ["implement"])
        b = solve(plan, ["brief", {"id": "r3", "key": "head"}])
        self.assertEqual(jobs(b), ["final"])
        self.assertEqual(b["ready"][0]["support_ids"], ["r3"])
        self.assertTrue(solve(plan, ["delivered"])["satisfied"])

    def test_key_qualifiers_are_required(self):
        typed = {"key":"angle", "unit":"degrees", "object":"phone", "version":2}
        plan = proposal(op("orient", [typed], ["delivered"]))
        self.assertEqual(jobs(solve(plan, [{**typed, "unit":"radians"}])), [])
        self.assertEqual(jobs(solve(plan, [{**typed, "object":"watch"}])), [])
        self.assertEqual(jobs(solve(plan, [{**typed, "version":1}])), [])
        self.assertEqual(jobs(solve(plan, [typed])), ["orient"])

    def test_unsupported_cycles_do_not_ground_themselves(self):
        plan = proposal(op("x", ["y"], ["delivered"]), op("y", ["delivered"], ["y"]))
        result = solve(plan, [])
        self.assertEqual(result["cycles"], ["delivered"])
        self.assertEqual(jobs(result), [])
        self.assertEqual(jobs(solve(plan, ["y"])), ["x"])
        self.assertTrue(solve(plan, [], budget=1)["budget_exhausted"])

    def test_independent_support_and_withdrawal(self):
        plan = proposal(op("run", ["eligible"], ["delivered"]))
        first = {"key": "one", "id": "one"}
        second = {"key": "two", "id": "two"}
        x = {"key": "eligible", "id": "three", "supports": [["one"], ["two"]]}
        facts = [first, second, x]
        self.assertEqual(jobs(solve(plan, facts)), ["run"])
        facts.append({"id": "one", "status": "retracted"})
        self.assertEqual(jobs(solve(plan, facts)), ["run"])
        facts.append({"id": "two", "status": "retracted"})
        self.assertEqual(jobs(solve(plan, facts)), [])
        x["supports"] = [["three"]]
        self.assertEqual(jobs(solve(plan, [x])), [])

    def test_bounds_permissions_and_counterevidence(self):
        plan = proposal(op("deploy", ["review"], ["delivered"], permissions=["publish"]), case_id="A")
        self.assertEqual(jobs(solve(plan, ["review"])), [])
        plan["permissions"] = ["publish"]
        self.assertEqual(jobs(solve(plan, [{"key":"review", "case_id":"B"}])), [])
        result = solve(plan, ["review", {"key": "review", "polarity": "negative", "id": "fail"}])
        self.assertEqual(jobs(result), [])
        self.assertEqual(result["evidence"][0]["refutations"], ["fail"])

    def test_alternative_route_eliminates_irrelevant_gap(self):
        plan = proposal(op("absent", ["no-input"], ["delivered"]), op("works", [], ["delivered"]))
        result = solve(plan, [])
        self.assertEqual(result["gaps"], [])
        self.assertEqual(jobs(result), ["works"])

    def test_finite_binding_and_canonical_types(self):
        plan = proposal(op("edit", ["source:${app}"], ["package:${app}"],
                           parameters=["app"], payload={"file":"source:${app}"}),
                        goal="package:one", domains={"app":["one", "two"]})
        result = solve(plan, ["source:one", "source:two"])
        self.assertEqual(len(result["ready"]), 1)
        self.assertEqual(result["ready"][0]["payload"]["file"], "source:one")


class DiscriminationTests(unittest.TestCase):
    def setUp(self):
        self.plan = proposal(op("use-relative", [], ["delivered"], branch="relative"),
                             case_id="one", branches=[
                                 {"id": "north", "domain":"intent", "predictions":{"turn":["up"], "theme":["up"]}},
                                 {"id": "relative", "domain":"intent", "predictions":{"turn":["left"], "theme":["up"]}},
                             ], probes=[{"id":"turn", "cost":2}, {"id":"theme", "cost":1}])

    def test_solve_discriminates_and_revises_under_authority(self):
        open_result = solve(self.plan, [])
        self.assertEqual(open_result["probe"]["id"], "turn")
        self.assertEqual(jobs(open_result), [])
        fact = {"probe_id": "turn", "value":"left", "domain":"intent", "source":"device", "id":"p"}
        self.assertEqual(solve(self.plan, [fact])["branches"], ["north", "relative"])
        receipt = {**fact, "source":"user"}
        resolved = solve(self.plan, [receipt])
        self.assertEqual(resolved["rejected_branches"], {"north":["p"]})
        self.assertEqual(jobs(resolved), ["use-relative"])
        self.assertEqual(jobs(solve(self.plan, [{**receipt, "case_id":"elsewhere"}])), [])

    def test_overlap_unknown_and_conflicting_observations(self):
        self.plan["branches"].append({"id":"unknown", "domain":"intent", "predictions":{}})
        self.assertEqual(solve(self.plan, [])["probe"]["guaranteed_eliminations"], 1)
        self.plan["branches"][0]["predictions"]["turn"] = ["up", "left"]
        self.assertIsNone(solve(self.plan, [])["probe"])
        inconsistent = [{"probe_id":"turn", "domain":"intent", "source":"user", "value":v, "id":v}
                        for v in ("left", "up")]
        result = solve(self.plan, inconsistent)
        self.assertEqual(len(result["branches"]), 3)
        self.assertEqual(sorted(result["conflicting_observations"]["turn"]), ["left", "up"])


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.plan = proposal(goal="verified:second", project_id="same-project", brain_id="my-brain",
                             bindings={"name":"second"}, permissions=["read"])
        self.method = {"id":"inspect", "scope":{"brain_id":"my-brain", "project_id":"same-project"},
                       "revision":1, "parameters":["name"], "permissions":["read"], "exclude_facts":["unsupported"],
                       "operator":op("inspect", ["data:${name}"], ["verified:${name}"],
                                     payload={"entry":"inspect.py", "input":"${name}"})}
        self.replays = [{"id":"r1", "case":"positive", "passed":True, "project_id":"same-project"},
                        {"id":"r2", "case":"negative", "passed":True, "project_id":"same-project"}]

    def test_learn_bind_exclude_and_withdraw(self):
        usable = qualify_method(self.method, self.replays)
        before = deepcopy(usable)
        self.assertEqual(solve(self.plan, ["data:second"])["gaps"], ["verified:second"])
        result = solve(self.plan, ["data:second"], [usable])
        self.assertEqual(jobs(result), ["method:inspect@1"])
        self.assertEqual(result["ready"][0]["payload"], {"entry":"inspect.py", "input":"second"})
        self.assertEqual(jobs(solve(self.plan, ["data:second", "unsupported"], [usable])), [])
        self.assertEqual(jobs(solve({**self.plan, "permissions":[]}, ["data:second"], [usable])), [])
        self.assertEqual(jobs(solve({**self.plan, "project_id":"foreign"}, ["data:second"], [usable])), [])
        withdrawn = {**usable, "status":"withdrawn", "revision": 2}
        self.assertEqual(jobs(solve(self.plan, ["data:second"], [withdrawn, usable])), [])
        self.assertEqual(usable, before)
        usable["operator"]["payload"]["entry"] = "changed.py"
        self.assertEqual(jobs(solve(self.plan, ["data:second"], [usable])), [])

    def test_promotion_requires_independent_qualified_replays(self):
        shared = {**self.method, "scope":{"brain_id":"my-brain"}}
        with self.assertRaises(ValueError):
            qualify_method(shared, self.replays)
        with self.assertRaises(ValueError):
            qualify_method(self.method, self.replays[:1])
        with self.assertRaises(ValueError):
            qualify_method(self.method, self.replays + self.replays)
        extra = {"id":"independent","case":"positive","project_id":"other","passed":True}
        promoted = qualify_method(shared, self.replays + [extra])
        result = solve({**self.plan,"project_id":"other"}, ["data:second"], [promoted])
        self.assertEqual(jobs(result), ["method:inspect@1"])
        local = qualify_method(self.method, self.replays)
        result = solve(self.plan, ["data:second"], [promoted, local])
        self.assertEqual(result["ready"][0]["method_revision"]["scope"]["project_id"], "same-project")
        retired = {**local, "revision":2, "status":"revoked"}
        self.assertEqual(jobs(solve(self.plan, ["data:second"], [promoted,local,retired])), [])


if __name__ == "__main__":
    unittest.main()
