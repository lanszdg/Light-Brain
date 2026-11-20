"""OpenHands model loop with one bounded workspace tool. No host terminal is exposed."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Literal
from importlib.metadata import version


def execute(request):
    from pydantic import SecretStr
    from openhands.sdk import LLM, Action, Agent, Conversation, Observation, ToolDefinition
    from openhands.sdk.tool import Tool, ToolExecutor, register_tool
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    from lightbrain.host.git_workspace import safe_name,write_changes
    from lightbrain.host.sandbox import observe

    repo = Path(request["repo"]).resolve(strict=True)
    trail = []

    class Edit(Action):
        op: Literal["list","read","write","delete","run"]
        path: str = ""
        text: str = ""
        argv: list[str] = []

    class Result(Observation):
        """SDK content survives event serialization and subsequent model turns."""

    class Editor(ToolExecutor[Edit,Result]):
        def __call__(self, action, conversation=None):
            try:
                output = self.apply(action)
                event = {"op":action.op, "path":action.path}
                if action.op == "run":
                    actual = json.loads(output)
                    event.update(executor=actual["environment"]["executor"],exit_code=actual.get("exit_code"),
                                 saved_files=actual.get("saved_files",[]))
                trail.append(event)
                return Result.from_text(output)
            except (ValueError, OSError, RuntimeError) as exc:
                return Result.from_text(f"Tool failed: {str(exc)[:500]}",is_error=True)

        def apply(self,a):
            if a.op == "list":
                names = []
                for root,dirs,files in os.walk(repo):
                    dirs[:] = [d for d in dirs if d != ".git"]
                    names += [str((Path(root)/f).relative_to(repo)) for f in files]
                return "\n".join(sorted(names)[:500]) or "The project is empty"
            if a.op == "run":
                if not a.argv:
                    raise ValueError("Specify argv for isolated execution")
                with tempfile.TemporaryDirectory(dir=request["scratch"]) as temp:
                    shutil.copytree(repo,temp,dirs_exist_ok=True,ignore=shutil.ignore_patterns(".git"))
                    result = observe(temp,{"kind":"command","argv":a.argv,"export_edits":True}, {})
                    edits = result.pop("edits",{})
                    if edits:
                        paths = [*edits["files"],*edits["delete"]]
                        for name in paths:
                            safe_name(name)
                        write_changes(repo,edits["files"],edits["delete"])
                        result["saved_files"] = paths
                result["stdout"] = result.get("stdout","")[:7500]
                result["stderr"] = result.get("stderr","")[:1500]
                return json.dumps(result,ensure_ascii=False)
            file = repo / safe_name(a.path)
            if a.op == "read":
                with file.open(encoding="utf-8") as f:
                    return f.read(32000)
            if a.op == "write":
                if len(a.text.encode()) > 500000:
                    raise ValueError("One file edit exceeds the allowed size")
                file.parent.mkdir(parents=True,exist_ok=True)
                file.write_bytes(a.text.encode())
            elif a.op == "delete":
                file.unlink(missing_ok=True)
            return f"{a.op} complete: {a.path}"

    class ProjectTool(ToolDefinition[Edit,Result]):
        @classmethod
        def create(cls,conv_state,**params):
            return [cls(description="Inspect and edit this project. list; read or write entire relative path; "
                                    "delete a file; run argv with cwd at the project root inside offline Linux. "
                                    "Successful commands persist their text-file edits in this project. Use relative paths.",
                        action_type=Edit,observation_type=Result,executor=Editor())]

    register_tool(ProjectTool.name,ProjectTool)
    llm = LLM(usage_id="development",model=request["model"],api_key=SecretStr(request["key"]),
              base_url=request["url"],max_output_tokens=12000,num_retries=1)
    agent = Agent(llm=llm,tools=[Tool(name=ProjectTool.name)])
    # The conversation directory only holds SDK state. All project access goes through ProjectTool.
    session = Conversation(agent=agent,workspace=request["scratch"],
                           max_iteration_per_run=min(max(1,request["iterations"]),50))
    try:
        session.send_message("Use the project tool to edit the project. The current directory in run contains the same "
                             "files as list/read/write. Use relative paths. Successful commands persist changes. "
                             "Finish after checking the requested behavior.\n" + request["task"])
        session.run()
    finally:
        session.close()
    if not any(a["op"] in {"write","delete"} or a.get("saved_files") for a in trail):
        raise RuntimeError("The session returned no project edits")
    report = {"sdk_version":version("openhands-sdk"),"model":request["model"],"actions":trail}
    (Path(request["scratch"])/"actions.json").write_text(json.dumps(report,ensure_ascii=False),encoding="utf-8")


if __name__ == "__main__":
    try:
        execute(json.load(sys.stdin))
    except Exception as exc:
        import traceback
        last = traceback.extract_tb(exc.__traceback__)[-1]
        extra = getattr(exc,"name",None) if isinstance(exc,ImportError) else None
        status = getattr(exc,"status_code",None)
        site = f"{Path(last.filename).name}:{last.lineno}"
        detail = " ".join(str(item) for item in (type(exc).__name__,site,extra,status) if item)
        print("LB_SDK_ERROR="+detail)
        raise SystemExit(1)
