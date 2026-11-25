import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import files
from .process import command


class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Empty(Parameters):
    """No arguments are required."""


class LocalPath(Parameters):
    path: str = Field(min_length=1, max_length=4096)


class FileList(LocalPath):
    offset: int = Field(default=0, ge=0, le=100000)


class FileSearch(LocalPath):
    query: str = Field(default="", max_length=256)
    duplicates: bool = False


class FileRead(LocalPath):
    offset: int = Field(default=0, ge=0, le=32000)
    limit: int = Field(default=6000, ge=1, le=16000)


class FileWrite(LocalPath):
    content: str = Field(max_length=128000)
    replace: bool = False


class FileChange(LocalPath):
    operation: Literal["mkdir", "copy", "move", "trash", "delete", "archive", "extract"]
    destination: str | None = Field(default=None, max_length=4096)


class TerminalInput(Parameters):
    command: str = Field(min_length=1, max_length=16000)
    cwd: str = Field(default="~/", max_length=4096)
    timeout: int = Field(default=60, ge=1, le=3600)


class Region(Parameters):
    x: int = Field(ge=-32768, le=32768)
    y: int = Field(ge=-32768, le=32768)
    width: int = Field(ge=1, le=16384)
    height: int = Field(ge=1, le=16384)


class Capture(Parameters):
    scope: Literal["screen", "monitor", "window", "region"] = "screen"
    monitor: str | None = Field(default=None, max_length=120)
    region: Region | None = None
    ocr: bool = True


class Clipboard(Parameters):
    text: str | None = Field(default=None, max_length=24000)


class DesktopInput(Parameters):
    operation: Literal["move", "click", "right", "middle", "double", "drag", "scroll_up", "scroll_down",
                       "keys", "type"]
    x: int | None = Field(default=None, ge=-32768, le=32768)
    y: int | None = Field(default=None, ge=-32768, le=32768)
    to_x: int | None = Field(default=None, ge=-32768, le=32768)
    to_y: int | None = Field(default=None, ge=-32768, le=32768)
    value: str | None = Field(default=None, max_length=8000)
    count: int = Field(default=1, ge=1, le=20)


class WindowInput(Parameters):
    operation: Literal["open", "workspace", "close", "switch", "maximize", "minimize", "resize"]
    identifier: str | None = Field(default=None, max_length=30)
    geometry: Region | None = None
    workspace: int | None = Field(default=None, ge=0, le=100)
    application: str | None = Field(default=None, max_length=160)


class BrowserInput(Parameters):
    operation: Literal["start", "stop", "open", "new", "back", "forward", "refresh", "select", "close",
                       "scroll", "click", "type", "upload", "inspect"]
    url: str | None = Field(default=None, max_length=4096)
    tab: int | None = Field(default=None, ge=0, le=100)
    x: int | None = Field(default=None, ge=-10000, le=10000)
    y: int | None = Field(default=None, ge=-10000, le=10000)
    text: str | None = Field(default=None, max_length=8000)
    selector: str | None = Field(default=None, max_length=1000)
    artifact: str | None = Field(default=None, max_length=100)
    visible: bool | None = None


class WebAddress(Parameters):
    url: str = Field(min_length=1, max_length=4096)


class Research(Parameters):
    query: str = Field(min_length=1, max_length=500)


class Download(Parameters):
    selector: str = Field(min_length=1, max_length=1000)


class Generate(Parameters):
    workflow: str = Field(min_length=1, max_length=80)
    values: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=50)


class Remember(Parameters):
    text: str = Field(min_length=1, max_length=2500)
    category: Literal["semantic", "procedural", "observation", "error"] = "semantic"
    confidence: float = Field(default=0.6, ge=0, le=0.9)


class Delegate(Parameters):
    role: Literal["research", "computer", "coding", "memory", "verification", "diagnosis"]
    assignment: str = Field(min_length=1, max_length=5000)


class Finish(Parameters):
    summary: str = Field(min_length=1, max_length=2500)
    evidence: list[int] = Field(min_length=1, max_length=16)


class Learned(Parameters):
    id: int = Field(gt=0)


class Proposal(Parameters):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["workflow", "python"]
    proposal: dict


class Grants(Parameters):
    files: list[str] = Field(default_factory=list, max_length=8)
    desktop: bool = False
    browser: bool = False
    terminal: bool = False
    media: bool = False

    def normalized(self):
        return self.model_copy(update={"files": [str(files.local_path(path).resolve()) for path in self.files]})


@dataclass(frozen=True)
class Tool:
    schema: type
    description: str
    category: str = ""
    approval: bool = False

    def declaration(self, name):
        schema = self.schema.model_json_schema()
        definitions = schema.pop("$defs", {})

        def expand(value):
            if isinstance(value, list):
                return [expand(item) for item in value]
            if not isinstance(value, dict):
                return value
            if "$ref" in value:
                return expand(definitions[value["$ref"].split("/")[-1]])
            return {key: expand(item) for key, item in value.items() if key != "title"}

        schema = expand(schema)
        return {"type": "function", "function": {"name": name, "description": self.description,
                                                "parameters": schema}}


TOOLS = {
    "system_hardware": Tool(Empty, "Read and verify current CPU, GPU, memory, storage and network measurements"),
    "file_list": Tool(FileList, "List an accessible absolute directory or ~/ path", "files"),
    "file_read": Tool(FileRead, "Extract actual text from an absolute local file or ~/ path", "files"),
    "file_search": Tool(FileSearch, "Search an absolute directory for matching filenames or duplicate files", "files"),
    "file_write": Tool(FileWrite, "Create or replace text at an exact absolute path. Replacements make a backup.",
                       "files", True),
    "file_change": Tool(FileChange, "Create directory, copy, move, Trash, delete, ZIP, or extract a ZIP",
                        "files", True),
    "desktop_capture": Tool(Capture, "Capture an enabled display, monitor, region or window with optional OCR",
                            "desktop"),
    "desktop_windows": Tool(Empty, "Inspect the active window and open windows", "desktop"),
    "desktop_accessibility": Tool(Empty, "Inspect the desktop accessibility tree where AT-SPI is enabled", "desktop"),
    "desktop_clipboard": Tool(Clipboard, "Read or replace the local clipboard", "desktop", True),
    "desktop_input": Tool(DesktopInput, "Send approved pointer or keyboard input and inspect resulting state",
                          "desktop", True),
    "desktop_window": Tool(WindowInput, "Open, close, switch, resize or move an existing application window",
                           "desktop", True),
    "terminal_run": Tool(TerminalInput, "Run an exact approved shell command and verify its operating system exit code",
                         "terminal", True),
    "web_fetch": Tool(WebAddress, "Fetch a real HTTP(S) document and return source URL, actual text and links", "browser"),
    "browser_search": Tool(Research, "Search the web and return actual research links", "browser"),
    "browser_action": Tool(BrowserInput, "Navigate or inspect a controlled browser. Form interactions require approval.",
                           "browser"),
    "browser_download": Tool(Download, "Download a file from a specific link in the current browser tab",
                             "browser", True),
    "media_workflows": Tool(Empty, "List installed ComfyUI workflows and their verified input declarations", "media"),
    "media_generate": Tool(Generate, "Execute a reviewed local ComfyUI workflow and receive its actual output files",
                           "media", True),
    "remember": Tool(Remember, "Record user-provided information with a source and confidence level", approval=True),
    "agent_delegate": Tool(Delegate, "Ask a specialized internal agent to investigate a bounded subtask", "agents"),
    "task_complete": Tool(Finish, "Record the scheduled task outcome with successful tool action IDs as evidence", "task"),
    "improvement_propose": Tool(Proposal, "Propose a versioned read-only workflow or Python source for user review"),
    "workflow_run": Tool(Learned, "Execute a previously validated and enabled read-only reusable workflow"),
}


async def block(function, *args, **kwargs):
    work = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        # File operations already in flight must reach a consistent state before the run closes.
        await asyncio.gather(work, return_exceptions=True)
        raise


class Executor:
    def __init__(self, services):
        self.services = services

    def permitted_path(self, path, run):
        resolved = files.local_path(path).resolve()
        data = self.services.config.data
        if resolved == data or resolved.is_relative_to(data):
            raise PermissionError("Use BudAI's settings and authenticated exports to access its private data")
        return resolved

    def granted_path(self, path, run):
        resolved = self.permitted_path(path, run)
        return any(resolved == Path(root) or resolved.is_relative_to(root) for root in run.grants.files)

    def available(self, run, subset=None):
        all_names = []
        for name, tool in TOOLS.items():
            if subset is not None and name not in subset:
                continue
            if not tool.category or tool.category == "files" and run.grants.files:
                all_names.append(name)
            elif tool.category in {"desktop", "browser", "media", "terminal"} and getattr(run.grants, tool.category):
                all_names.append(name)
            elif tool.category == "agents" and run.preferences.allow_agents:
                all_names.append(name)
            elif tool.category == "task" and run.task_id:
                all_names.append(name)
        return all_names

    def prepare(self, run, name, arguments, allowed):
        if name not in allowed:
            raise PermissionError(f"{name} is unavailable to this request")
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        data = TOOLS[name].schema.model_validate(arguments).model_dump()
        for key in ("path", "destination", "cwd"):
            if data.get(key):
                data[key] = str(self.permitted_path(data[key], run))
        if name in {"file_list", "file_read", "file_search"} and not self.granted_path(data["path"], run):
            raise PermissionError(f"{data['path']} is outside the file paths selected for this request")
        if name == "file_change" and self.services.config.data.is_relative_to(Path(data["path"])):
            raise PermissionError("This operation would also affect BudAI's private data")
        if name == "desktop_capture" and data["scope"] == "region" and not data["region"]:
            raise ValueError("Regional capture requires coordinates and dimensions")
        if name == "desktop_capture" and data["scope"] != "region":
            data["region"] = None
        return data

    def approval(self, name, data):
        return TOOLS[name].approval or (
            name == "browser_action" and data["operation"] in {"type", "upload", "click"}
        )

    async def perform(self, run, name, data):
        services = self.services
        account = run.account
        if name == "system_hardware":
            return await asyncio.to_thread(services.monitor.snapshot)
        if name == "file_list":
            return await asyncio.to_thread(files.list_directory, **data)
        if name == "file_search":
            return await asyncio.to_thread(files.search, data["path"], data["query"], duplicates=data["duplicates"])
        if name == "file_read":
            path = files.local_path(data["path"])
            text = await asyncio.to_thread(files.read_document, path)
            end = data["offset"] + data["limit"]
            return {"file": files.properties(path), "text": text[data["offset"]:end],
                    "next_offset": end if end < len(text) else None}
        if name == "file_write":
            backups = services.config.data / "file-backups" / str(account) if data["replace"] else None
            return await block(files.write_text, data["path"], data["content"], backups)
        if name == "file_change":
            return await block(files.modify, data["operation"], data["path"], data["destination"])
        if name == "terminal_run":
            return await command(["/bin/bash", "-lc", data["command"]], cwd=data["cwd"],
                                 timeout=data["timeout"], check=True)
        if name == "desktop_capture":
            observed, _, _ = await services.desktop.capture(account, **data)
            return observed
        if name == "desktop_windows":
            return await services.desktop.windows()
        if name == "desktop_accessibility":
            return await services.desktop.accessibility()
        if name == "desktop_clipboard":
            if data["text"] is not None:
                return await services.desktop.set_clipboard(account, data["text"])
            return {"text": await services.desktop.clipboard()}
        if name == "desktop_input":
            result = await services.desktop.input(**data)
            observed, _, _ = await services.desktop.capture(account, ocr=True, persist=False)
            return dict(result, observation=observed)
        if name == "desktop_window":
            return await services.desktop.window(**data)
        if name == "web_fetch":
            return await services.browser.fetch(data["url"])
        if name == "browser_search":
            return await services.browser.search(account, data["query"])
        if name == "browser_action":
            return await services.browser.action(account, **data)
        if name == "browser_download":
            return await services.browser.download(account, data["selector"])
        if name == "media_workflows":
            return {"workflows": services.media.workflows()}
        if name == "media_generate":
            identifier = services.media.submit(account, data["workflow"], data["values"])
            result = await services.media.wait(account, identifier)
            if result["status"] != "completed":
                raise RuntimeError(result["error"] or f"ComfyUI job {result['status']}")
            return result
        if name == "remember":
            identifier = services.memory.record(
                account, data["text"], data["category"], "Bud suggestion", data["confidence"], run.conversation
            )
            return {"memory_id": identifier, "verified": True}
        if name == "agent_delegate":
            return await services.runner.agent(run, **data)
        if name == "improvement_propose":
            identifier = services.improvements.propose(account, **data)
            return {"proposal_id": identifier, "state": "proposed", "verified": True}
        if name == "workflow_run":
            return await services.improvements.run(run, self, data["id"])
        if name == "task_complete":
            return run.verify_outcome(data)
        raise LookupError(name)
