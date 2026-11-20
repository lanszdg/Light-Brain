"""Optional isolated, tool-using coding session on the pinned OpenHands worker."""
import os
import json
import subprocess
from pathlib import Path

from . import git_workspace as vcs


IMAGE = "ghcr.io/openhands/agent-server@sha256:bd35862a12dca128715c77b824b6afc2091e3071709032249c4ec39677afc0c1"


def model_config(selected):
    from ..settings import model_settings
    base,key,default = model_settings()
    model = selected or os.environ.get("LB_OPENHANDS_MODEL") or default
    if not isinstance(model,str) or not model:
        raise ValueError("The OpenHands worker requires a model deployment")
    key = os.environ.get("LB_OPENHANDS_API_KEY") or key
    if not key:
        raise ValueError("The OpenHands worker requires a scoped model key")
    return (os.environ.get("LB_OPENHANDS_BASE_URL") or base, key,
            model if model.startswith("openai/") else "openai/"+model)


def run_session(repo, task, context,selected=None):
    if not context.get("enable_openhands"):
        raise ValueError("The worker has not enabled the OpenHands executor")
    backend = context.get("openhands_backend", "bubblewrap")
    if backend == "bubblewrap":
        return local_session(repo, task, context,selected)
    if backend != "docker":
        raise ValueError("Unknown OpenHands workspace backend")
    from pydantic import SecretStr
    from openhands.sdk import LLM, Conversation
    from openhands.tools.preset.default import get_default_agent
    from openhands.workspace import DockerWorkspace

    url,key,model = model_config(selected)
    llm = LLM(usage_id="development",model=model,api_key=SecretStr(key),base_url=url)
    private = vcs.directory(context["workdir"], "agent-")
    base = vcs.export_bundle(repo, private)
    with DockerWorkspace(server_image=IMAGE,platform="linux/amd64") as ws:
        def run(cmd):
            result = ws.execute_command(cmd)
            if result.timeout_occurred or result.exit_code != 0:
                raise RuntimeError("OpenHands candidate Git operation failed")
        if not ws.file_upload(base["bundle"],"/tmp/lb-base.bundle").success:
            raise RuntimeError("Failed to copy authorized baseline to OpenHands")
        run("git clone /tmp/lb-base.bundle /workspace/project")
        agent = get_default_agent(llm=llm, cli_mode=True)
        session = Conversation(agent=agent, workspace=ws, max_iteration_per_run=30)
        try:
            session.send_message("Work in /workspace/project. Implement and test this request:\n" + task)
            session.run()
        finally:
            session.close()
        run("cd /workspace/project && git add -A && git -c user.name=LB -c user.email=lb@localhost commit --allow-empty -m candidate && git bundle create /tmp/lb-candidate.bundle HEAD")
        bundle = private / "candidate.bundle"
        if not ws.file_download("/tmp/lb-candidate.bundle",str(bundle)).success:
            raise RuntimeError("Failed to download candidate")
    refs = vcs.git(repo,"bundle","list-heads",str(bundle),"HEAD").split()
    if len(refs) != 2 or refs[1] != "HEAD":
        raise ValueError("OpenHands returned an invalid candidate bundle")
    result = vcs.checkout(private,bundle,refs[0])
    vcs.git(result,"merge-base","--is-ancestor",base["candidate_sha"],refs[0])
    return result, {"executor":"openhands-docker", "image":IMAGE,"model":model}


def local_session(repo, task, context,selected):
    from .sandbox import ROOT, linux_command, linux_path
    url,key,model = model_config(selected)
    runtime = Path(context.get("openhands_runtime") or os.environ.get("LB_OPENHANDS_RUNTIME") or ROOT/".runtime/openhands").resolve()
    python = runtime/"venv/bin/python"
    if not (runtime/"sdk-runtime.tar").is_file() and not python.parent.is_dir():
        raise RuntimeError("The pinned OpenHands Linux environment is missing")
    scratch = vcs.directory(context["workdir"],"sdk-")
    home = scratch / "home"
    home.mkdir()
    tmp = scratch / "tmp"
    tmp.mkdir()
    request = {"repo":linux_path(repo),"scratch":linux_path(scratch),"task":task,
               "model":model, "key":key,"url":url,
               "iterations":int(context.get("openhands_iterations",20))}
    seconds = min(int(context.get("openhands_timeout",300)),900)
    cmd = linux_command(context) + ["/usr/bin/timeout","-k","2",str(seconds),"/usr/bin/env","-i","PATH=/usr/bin:/bin",
          "HOME="+linux_path(home),"TMPDIR="+linux_path(tmp),"LANG=C.UTF-8",
          "XDG_CACHE_HOME="+linux_path(home/"cache"),"LITELLM_LOCAL_MODEL_COST_MAP=True",
          "HF_HUB_DISABLE_TELEMETRY=1","OTEL_SDK_DISABLED=true","DO_NOT_TRACK=1",
          "TIKTOKEN_CACHE_DIR="+linux_path(runtime/"cache/tiktoken"),
         ]
    session_code = linux_path(Path(__file__).with_name("openhands_local.py"))
    if (runtime/"sdk-runtime.tar").is_file():
        mem = scratch/"ram"
        mem.mkdir()
        cmd += ["unshare","--user","--map-root-user","--mount","--fork","--kill-child",
                "/usr/bin/python3","-I","-B",linux_path(Path(__file__).with_name("sdk_runtime.py")),
                linux_path(runtime/"sdk-runtime.tar"),linux_path(mem),session_code]
    else:
        cmd += [linux_path(runtime)+"/venv/bin/python","-I","-B",session_code]
    result = subprocess.run(cmd,input=json.dumps(request).encode("utf-8"),cwd=scratch,
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                            timeout=seconds+8)
    if result.returncode:
        # Provider exceptions and third-party logs can carry request details.
        marker = next((line for line in result.stdout.decode("utf-8","replace").splitlines()
                       if line.startswith("LB_SDK_ERROR=")), "")
        message = marker.split("=",1)[-1] if marker else "process exited"
        raise RuntimeError(f"OpenHands SDK failed ({result.returncode}, {message[:160]})")
    report = json.loads((scratch/"actions.json").read_text(encoding="utf-8"))
    return repo, {"executor":"openhands-bubblewrap", **report}
