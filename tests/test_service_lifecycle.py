"""Regression tests across HTTP, processes, Git, isolated verification and exported programs."""

import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zipfile import ZipFile

from lightbrain.settings import worker_key


ROOT = Path(__file__).resolve().parents[1]
ARGS = ["python3", "app.py", "7", "8"]
BRANCH = "Create a formatter for our labels"
MODEL_KEY = "local-protocol-regression"


def check(text, argv=ARGS):
    return {"id": "observed", "kind": "command", "argv": argv, "stdout_equals": text + "\n"}


class Model(BaseHTTPRequestHandler):
    """A protocol fixture. The repair can only run when actual fault observations arrive."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system, request = data["messages"][0]["content"], json.loads(data["messages"][1]["content"])
        planning = "design a local" in system
        idea = request["idea"]
        self.server.calls.append((data["model"], "plan" if planning else "develop", request))
        if planning:
            actions = [{"id": "app", "requires": [], "instruction": idea}]
            proposal = {"title": "Regression candidate", "intent_quote": idea, "approach": "Python CLI",
                        "entrypoint": ["python3", "app.py"], "actions": actions,
                        "criteria": request["owner_criteria"]}
            if BRANCH in idea:
                domain = "implementation" if "existing policy" in idea else "intent"
                choices = ["Upper", "Lower"]
                actions[:] = [{"id": k.lower(), "instruction": k, "requires": []} for k in choices]
                probe = {"id": "policy", "cost": 1}
                if domain == "intent":
                    probe.update(question="Which case should a label use?", choices=choices)
                else:
                    probe.update(read="stdout", criterion={"id": "policy-read", "kind": "command",
                                                           "argv": ["python3", "policy.py"], "expect_exit": 0})
                proposal["inquiry"] = {"domain": domain, "probes": [probe], "branches": [
                    {"id": k.lower(), "action_ids": [k.lower()], "predictions": {"policy": [k]}}
                    for k in choices]}
        else:
            if BRANCH in idea:
                transform = "upper" if request["instruction"] == "Upper" else "lower"
                line = f"print('formatter' if sys.argv[1]=='--version' else sys.argv[1].{transform}())"
            elif "difference" in idea:
                line = "print(int(sys.argv[1])-int(sys.argv[2]))"
            elif "multiply" in idea and any(row.get("observed", {}).get("stdout") == "15\n"
                                           for row in request.get("failure", {}).get("checks", [])):
                line = "print(int(sys.argv[1])*int(sys.argv[2]))"
            else:
                line = "print(sum(map(int,sys.argv[1:])))"
            proposal = {"files": {"app.py": "import sys\n" + line + "\n",
                                   "README.md": "Run python3 app.py with the desired arguments."},
                        "delete": [], "summary": "Updated the command-line operation."}
        result = {"model": data["model"], "choices": [{"finish_reason": "stop", "message":
                  {"content": json.dumps(proposal)}}]}
        blob = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT, prefix="lifecycle-")
        self.tmp = Path(temp.name)
        self.addCleanup(temp.cleanup)
        self.model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        self.model.calls = []
        self.addCleanup(self.model.server_close)
        threading.Thread(target=self.model.serve_forever, daemon=True).start()
        self.addCleanup(self.model.shutdown)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        self.env = {**os.environ, "LB_URL": self.url, "LB_HOME": str(self.tmp / "store"),
                    "LB_MODEL_BASE_URL": f"http://127.0.0.1:{self.model.server_port}/v1",
                    "LB_MODEL_NAME": "first-model", "LB_MODEL_API_KEY": "fixture", "LB_TOKEN": MODEL_KEY,
                    "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        self.start_process("serve")
        self.await_(lambda: self.http("/health").get("ok"), timeout=12)

    def command(self, *args):
        return [sys.executable, "-B", str(ROOT / "lb.py"), *args]

    def start_process(self, command):
        log = (self.tmp / (command + ".log")).open("ab")
        self.addCleanup(log.close)
        proc = subprocess.Popen(self.command(command), env=self.env, cwd=ROOT, stdout=log, stderr=log,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        def stop():
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=10)
        self.addCleanup(stop)

    def await_(self, fn, timeout=65):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = fn()
            except URLError:
                result = None
            if result:
                return result
            time.sleep(.12)
        logs = [p.read_text(encoding="utf-8", errors="replace")[-900:] for p in self.tmp.glob("*.log")]
        self.fail(f"timed out: {logs}")

    def http(self, path, body=None, *, worker=False):
        secret = worker_key(MODEL_KEY) if worker else MODEL_KEY
        req = Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                      headers={"Content-Type":"application/json", "Authorization":"Bearer " + secret})
        with urlopen(req, timeout=8) as response:
            return json.load(response)

    def submit(self, idea, condition, **attrs):
        return self.http("/brains/test/cases", {"idea":idea, "criteria":[condition], **attrs})["case_id"]

    def finished(self, cid):
        c = self.await_(lambda: (s if (s:=self.http("/cases/"+cid))["state"] in
                                 {"failed","needs_input","cancelled","delivered"} else None))
        self.assertEqual(c["state"], "delivered", c.get("error",c.get("decisions")))
        return c

    def export_run(self, cid, args):
        # Only this test's own fixed Python fixture is executed here.
        dest = self.tmp / (cid + ".zip")
        reply = subprocess.check_output(self.command("download", cid, str(dest)), cwd=ROOT,
                                        env=self.env, encoding="utf-8", timeout=20)
        self.assertEqual(json.loads(reply)["sha256"], sha256(dest.read_bytes()).hexdigest())
        with ZipFile(io.BytesIO(dest.read_bytes())) as z:
            program = self.tmp / ("export-" + cid)
            program.mkdir()
            (program / "app.py").write_bytes(z.read("app.py"))
        return subprocess.check_output([sys.executable, "-I", "-B", "app.py", *args], cwd=program,
                                       timeout=8, encoding="utf-8").strip()

    def test_independent_failure_repairs_wrong_behaviour_and_exports(self):
        self.start_process("worker")
        cid = self.submit("Please multiply two integers", check("56"))
        case = self.finished(cid)
        jobs = self.http("/cases/"+cid+"/jobs")
        verified = [j for j in jobs if j["kind"] == "verify"]
        self.assertEqual([j["receipt"]["ok"] for j in verified], [False,True])
        bad, good = [j["receipt"] for j in verified]
        self.assertEqual(bad["checks"][0]["observed"]["stdout"], "15\n")
        self.assertEqual(good["checks"][0]["observed"]["stdout"], "56\n")
        self.assertEqual(bad["criteria_digest"], good["criteria_digest"])
        self.assertNotEqual(bad["candidate_sha"], good["candidate_sha"])
        self.assertEqual(case["delivery"]["candidate_sha"], good["candidate_sha"])
        repairs = [d["failure"] for _,kind,d in self.model.calls if kind=="develop" and d.get("failure")]
        self.assertEqual(repairs[0]["candidate_sha"], bad["candidate_sha"])
        self.assertEqual(self.export_run(cid, ["3","11"]), "33")

    def test_channel_rolls_back_to_verified_prior_delivery(self):
        self.start_process("worker")
        cid=self.submit("Add the two arguments",check("15"),simulate_release=True)
        first=self.finished(cid)["delivery"]
        self.http("/cases/"+cid+"/events",{"kind":"revise","text":"Now take the difference of the two inputs.",
            "criteria":[check("-1")]})
        second=self.finished(cid)["delivery"]
        self.assertNotEqual(first["candidate_sha"],second["candidate_sha"])
        self.http("/cases/"+cid+"/channel",{"action":"rollback","candidate_sha":first["candidate_sha"]})
        result=self.await_(lambda:self.http("/cases/"+cid).get("channel_receipt"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["archive_id"],first["archive_id"])
        self.assertEqual(self.http("/cases/"+cid)["delivery"]["candidate_sha"],second["candidate_sha"])

    def test_superseded_real_receipt_and_hot_model_change(self):
        cid = self.submit("Sum two integers", check("15"))
        # Drain exactly two real worker processes so we can hold the next verifier reply in flight.
        for kind in ("plan", "develop"):
            self.await_(lambda: any(j["kind"]==kind and j["status"]=="queued"
                                    for j in self.http("/cases/"+cid+"/jobs")))
            subprocess.run(self.command("worker","--once"), cwd=ROOT, env=self.env, timeout=35, check=True)
        self.await_(lambda: any(j["kind"]=="verify" for j in self.http("/cases/"+cid+"/jobs")))
        held = self.http("/internal/jobs/claim", {"worker":"held","kinds":["verify"]}, worker=True)
        self.assertEqual(held["payload"]["goal_version"],1)
        # The external process downloads the old bundle, independently verifies it, then stops before POST.
        request = self.tmp / "held.json"
        request.write_text(json.dumps(held),encoding="utf-8")
        script = ("import json,sys,os;from pathlib import Path;from lightbrain.worker import execute,Transport;"
                  "from lightbrain.settings import configure_temp;"
                  "root=Path(sys.argv[1]);configure_temp(root);"
                  "print(json.dumps(execute(json.loads((root/'held.json').read_text()),root,"
                  "Transport(os.environ['LB_URL'],os.environ['LB_TOKEN']),'held')))")
        real = json.loads(subprocess.check_output([sys.executable,"-B","-c",script,str(self.tmp)],
                          env=self.env, cwd=ROOT, timeout=40, encoding="utf-8"))
        self.assertTrue(real["ok"])
        url = "/cases/"+cid+"/events"
        self.http(url, {"kind":"revise", "text":"Change the operation to difference", "criteria":[check("-1")]})
        self.http(url, {"kind":"switch_model", "model":"second-model"})
        auth = {"worker":"held","attempt":held["attempt"]}
        with self.assertRaises(HTTPError) as conflict:
            self.http("/internal/jobs/"+held["id"]+"/heartbeat",auth,worker=True)
        self.assertEqual(conflict.exception.code,409)
        self.http("/internal/jobs/"+held["id"]+"/receipt",{**auth,"receipt":real},worker=True)
        self.await_(lambda: any(e["event_type"]=="superseded" for e in self.http(url)))
        self.assertFalse(self.http("/cases/"+cid).get("delivery"))
        self.start_process("worker")
        current = self.finished(cid)
        self.assertEqual(current["delivery"]["goal_version"],2)
        self.assertNotEqual(current["delivery"]["candidate_sha"],real["candidate_sha"])
        self.assertNotEqual(current["delivery"]["verification_job"],held["id"])
        self.assertEqual(self.export_run(cid,["12","5"]),"7")
        revised = [(name,kind) for name,kind,body in self.model.calls if "difference" in body["idea"]]
        self.assertEqual(revised,[("second-model","plan"),("second-model","develop")])

    def test_owner_selects_one_branch_and_only_its_code_runs(self):
        self.start_process("worker")
        cid = self.submit(BRANCH, check("formatter",["python3","app.py","--version"]))
        pending = self.await_(lambda: (s if (s:=self.http("/cases/"+cid))["state"]=="needs_input" else None))
        self.assertEqual(pending["inquiry_step"]["choices"], ["Upper","Lower"])
        self.assertEqual([kind for _,kind,_ in self.model.calls],["plan"])
        self.http("/cases/"+cid+"/events",{"kind":"decision","text":"Upper"})
        case = self.finished(cid)
        self.assertEqual(case["inquiry_step"]["branch_id"],"upper")
        self.assertEqual(case["observations"][0]["source"],"user")
        instructions = [req["instruction"] for _,k,req in self.model.calls if k=="develop"]
        self.assertEqual(instructions,["Upper"])
        self.assertEqual(self.export_run(cid,["miX"]),"MIX")

    def test_real_repository_probe_selects_implementation(self):
        repo=self.tmp/"existing"
        repo.mkdir()
        (repo/"policy.py").write_text("print('Lower')",encoding="utf-8")
        (repo/"app.py").write_text("print('formatter')",encoding="utf-8")
        git=["git","-c","user.email=fixture@localhost","-c","user.name=Fixture","-c","commit.gpgsign=false"]
        for args in (["init","--template="],["add","."],["commit","-m","Baseline"]):
            subprocess.run([*git,*args],cwd=repo,check=True,capture_output=True)
        sha=subprocess.check_output(git+["rev-parse","HEAD"],cwd=repo,encoding="ascii").strip()
        self.start_process("worker")
        cid=self.submit(BRANCH+" using the existing policy",check("formatter",["python3","app.py","--version"]),
                        baseline=str(repo))
        case=self.finished(cid)
        observation=case["observations"][0]
        self.assertEqual((observation["source"],observation["candidate_sha"],observation["value"]),
                         ("verifier",sha,"Lower"))
        self.assertEqual(case["inquiry_step"]["branch_id"],"lower")
        self.assertEqual([d["instruction"] for _,kind,d in self.model.calls if kind=="develop"],["Lower"])
        self.assertEqual(self.export_run(cid,["MiX"]),"mix")
