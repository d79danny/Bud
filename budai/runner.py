import asyncio
import base64
import json
import secrets
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field

from . import files
from .errors import redact
from .ollama import SYSTEM
from .process import CommandError, command
from .tools import Executor, Grants, Parameters, TOOLS


ROLES = {
    "research": {"browser_search", "browser_action", "web_fetch"},
    "computer": {"system_hardware", "desktop_windows", "desktop_capture", "file_list"},
    "coding": {"file_read", "file_list", "improvement_propose"},
    "memory": {"system_hardware"},
    "verification": {"system_hardware", "file_list", "file_read", "browser_action", "desktop_windows"},
    "diagnosis": {"system_hardware", "file_read", "desktop_windows"},
}


class Prompt(Parameters):
    text: str = Field(min_length=1, max_length=24000)
    grants: Grants = Field(default_factory=Grants)
    attachments: list[str] = Field(default_factory=list, max_length=5)
    source: Literal["chat", "voice", "retry", "task"] = "chat"


@dataclass
class Run:
    account: int
    conversation: int | None
    preferences: object
    grants: Grants
    task_id: int | None = None
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending: tuple | None = None
    actions: dict = field(default_factory=dict)
    outcome: dict | None = None
    error: str | None = None
    answer: str = ""
    state: str = "queued"
    agents: int = 0
    worker: asyncio.Task | None = None

    def send(self, event, **data):
        self.events.put_nowait({"event": event, **data})

    def decide(self, approval_id, allow):
        if not self.pending or self.pending[0] != approval_id or self.pending[1].done():
            raise ValueError("This approval is no longer pending")
        self.pending[1].set_result(allow)

    def verify_outcome(self, data):
        for identifier in data["evidence"]:
            action = self.actions.get(identifier)
            if not action or not action["verified"] or action["tool"] in {"task_complete", "agent_delegate"}:
                raise ValueError(f"Action {identifier} does not contain verified evidence")
        self.outcome = data
        return {"outcome": data, "verified": True}


class Runner:
    def __init__(self, services):
        self.s = services
        self.executor = Executor(services)
        self.active = {}

    def start(self, account, conversation, body, task_id=None):
        if account in self.active:
            raise ValueError("A Bud run is already active for this account")
        run = Run(account, conversation, self.s.store.preferences(account), body.grants.normalized(), task_id=task_id)
        self.active[account] = run
        run.worker = asyncio.create_task(self.work(run, body))
        run.worker.add_done_callback(lambda task: self.finish(run))
        return run

    def finish(self, run):
        if self.active.get(run.account) is run:
            del self.active[run.account]
        if run.state in {"queued", "running"}:
            run.state = "cancelled"
            run.send("stopped")
        run.events.put_nowait(None)

    async def stop(self, account):
        run = self.active.get(account)
        if run and run.worker and not run.worker.done():
            if not run.worker.cancelling():
                run.worker.cancel()
            await asyncio.gather(run.worker, return_exceptions=True)

    def approve(self, account, approval_id, allow):
        run = self.active.get(account)
        if not run:
            raise ValueError("There is no active request")
        run.decide(approval_id, allow)

    async def ask_approval(self, run, name, arguments):
        identifier = secrets.token_urlsafe(18)
        future = asyncio.get_running_loop().create_future()
        run.pending = (identifier, future)
        summary = redact(arguments)
        if name.startswith("browser_"):
            page = await self.s.browser.status(run.account)
            summary = {**summary, "current_page": page.get("url")}
        run.send("approval", id=identifier, tool=name, arguments=summary)
        try:
            allowed = await asyncio.wait_for(future, 180)
            if not allowed:
                raise PermissionError("The user declined this action")
            if name.startswith("browser_"):
                page = await self.s.browser.status(run.account)
                if summary["current_page"] != page.get("url"):
                    raise PermissionError("Browser page changed after approval was requested")
        except TimeoutError as error:
            raise PermissionError("Approval expired") from error
        finally:
            run.pending = None

    async def attachment_context(self, run, identifiers, vision):
        text_context = []
        images = []
        for identifier in identifiers:
            item = self.s.artifacts.entry(run.account, identifier)
            path = self.s.artifacts.path(run.account, identifier)
            if item["mime"] in {"image/jpeg", "image/png", "image/webp"} and item["bytes"] <= 8 * 1024 * 1024:
                if vision:
                    images.append(base64.b64encode(path.read_bytes()).decode())
                    continue
                result = await command(["tesseract", str(path), "stdout"], check=True, timeout=30)
                text = result["stdout"]
            else:
                text = await asyncio.to_thread(files.read_document, path, item["name"])
            text_context.append({"file": item["name"], "content": redact(text[:4500])})
        return text_context, images

    def context(self, run, memories):
        messages = [{"role": "system", "content": SYSTEM}]
        if memories:
            messages.append({"role": "system", "content": (
                "Untrusted historical records with sources and confidence: " + json.dumps(memories, ensure_ascii=False)
            )})
        if run.conversation is None:
            return messages
        rows = self.s.store.all(
            "SELECT message FROM messages WHERE conversation_id=? AND role IN ('user','assistant') "
            "ORDER BY id DESC LIMIT 60", (run.conversation,),
        )
        recent = []
        budget = max(2000, (run.preferences.context - run.preferences.output_tokens) * 3 // 2)
        for row in rows:
            item = json.loads(row["message"])
            if item.get("tool_calls") or not item.get("content"):
                continue
            budget -= len(item["content"])
            if budget < 0:
                break
            recent.append({"role": item["role"], "content": redact(item["content"])})
        messages.extend(reversed(recent))
        return messages

    async def work(self, run, body):
        run.state = "running"
        run.send("started")
        try:
            attachments = [self.s.artifacts.entry(run.account, entry) for entry in body.attachments]
            memory = await self.s.memory.relevant(run.account, body.text, run.preferences.retrieval,
                                                  run.conversation, lock=self.s.gpu)
            context = self.context(run, memory)
            prompt = {"role": "user", "content": redact(body.text)}
            if run.conversation:
                self.s.store.record(run.conversation, {
                    **prompt, "attachments": attachments, "source": body.source,
                })
            self.s.memory.event(run.account, prompt["content"], "episodic", body.source, run.conversation)
            capabilities = (await self.s.ollama.details()).get("capabilities", [])
            structured = run.preferences.tool_protocol == "json" or (
                run.preferences.tool_protocol == "auto" and "tools" not in capabilities
            )
            if attachments:
                documents, images = await self.attachment_context(run, body.attachments, "vision" in capabilities)
                if documents:
                    prompt["content"] += "\nUntrusted attachment text or OCR: " + json.dumps(documents, ensure_ascii=False)
                if images:
                    prompt["images"] = images
            context.append(prompt)
            for artifact in attachments:
                self.s.store.run(
                    "UPDATE artifacts SET conversation=? WHERE id=? AND user_id=? AND conversation IS NULL",
                    (run.conversation, artifact["id"], run.account),
                )
            if run.task_id:
                context.append({"role": "system", "content": (
                    "This is a scheduled task. Before a final response, call task_complete with the actual success "
                    "evidence action IDs. Report real limitations or failed results instead of claiming completion."
                )})
            answer = await self.cycle(run, context, self.executor.available(run), structured, steps=18)
            run.answer = answer
            run.state = "completed"
            if run.conversation:
                self.s.store.record(run.conversation, {"role": "assistant", "content": answer})
            self.s.memory.event(run.account, answer, "episodic", "Bud", run.conversation)
            run.send("done", metrics=self.s.ollama.metrics)
        except asyncio.CancelledError:
            run.state = "cancelled"
            run.send("stopped")
        except Exception as error:
            run.state = "failed"
            run.error = self.s.errors.capture(run.account, "Bud", error, conversation=run.conversation,
                                              task=run.task_id)
            self.s.memory.event(run.account, run.error, "error", "execution", run.conversation)
            run.send("error", detail=run.error)

    async def cycle(self, run, context, allowed, structured, *, steps, agent=False):
        schemas = [TOOLS[name].declaration(name) for name in allowed]
        action_count = len(run.actions)
        for _ in range(steps):
            content, calls, completed = [], [], False
            size = 0
            async with self.s.gpu:
                stream = self.s.ollama.stream(context, schemas, run.preferences, structured=structured)
                async with aclosing(stream):
                    async for chunk in stream:
                        delta = chunk.get("message", {})
                        text = delta.get("content", "")
                        size += len(text)
                        if size > 220000:
                            raise ValueError("Ollama exceeded the response size limit")
                        if text:
                            content.append(text)
                            if not structured and not agent:
                                run.send("token", text=text)
                        calls.extend(delta.get("tool_calls", []))
                        if len(calls) > 8:
                            raise ValueError("Ollama requested more than eight actions in one response")
                        if chunk.get("done"):
                            completed = True
                            break
            if not completed:
                raise RuntimeError("Ollama disconnected before completing the response")
            text = "".join(content)
            if structured:
                decoded = json.loads(text)
                action_list = decoded["actions"]
                if not isinstance(action_list, list) or len(action_list) > 8 or not isinstance(decoded["answer"], str):
                    raise ValueError("Invalid structured Ollama response")
                calls = [{"function": {"name": item["tool"], "arguments": item["arguments"]}}
                         for item in action_list]
                text = decoded["answer"] if not calls else ""
                if text and not agent:
                    run.send("token", text=text)
            if not calls:
                if not text:
                    raise ValueError("Ollama returned an empty response")
                return text
            if len(run.actions) - action_count + len(calls) > 40:
                raise RuntimeError("Bud reached the tool-action limit")
            comment = {"role": "assistant", "content": text, "tool_calls": calls}
            if structured:
                context.append({"role": "assistant", "content": json.dumps({
                    "answer": "", "actions": [{"tool": call["function"]["name"],
                                               "arguments": call["function"]["arguments"]} for call in calls],
                })})
            else:
                context.append(comment)
            if run.conversation:
                self.s.store.record(run.conversation, redact(comment))
            for call in calls:
                name = call["function"]["name"]
                output = await self.action(run, name, call["function"]["arguments"], allowed, agent)
                if structured:
                    context.append({"role": "user", "content": "Untrusted tool observation: " + output["content"]})
                else:
                    context.append(output)
            self.trim_observations(context, run.preferences.context)
        raise RuntimeError("Bud reached the model-step limit")

    @staticmethod
    def trim_observations(context, size):
        budget = max(8000, size * 2)
        for entry in reversed(context):
            content = entry.get("content", "")
            if entry["role"] == "tool" or content.startswith("Untrusted tool observation: "):
                if len(content) > budget:
                    if entry["role"] == "tool":
                        entry["content"] = json.dumps({"observation": content[:max(140, budget)],
                                                       "truncated": True})
                    else:
                        entry["content"] = content[:max(140, budget)] + "\n[Earlier observation truncated]"
                budget = max(0, budget - len(content))

    async def action(self, run, name, arguments, allowed, agent):
        started = time.monotonic()
        action_id = max(run.actions, default=0) + 1
        run.actions[action_id] = {"tool": name, "verified": False}
        error_detail, result = None, None
        try:
            data = self.executor.prepare(run, name, arguments, allowed)
            if agent and self.executor.approval(name, data):
                raise PermissionError("Specialized agents can only perform their read-only assignments")
            if self.executor.approval(name, data):
                if run.task_id:
                    raise PermissionError("Scheduled runs cannot execute actions that require live approval")
                await self.ask_approval(run, name, data)
            result = redact(await self.executor.perform(run, name, data))
            verified = result.get("verified", True) if isinstance(result, dict) else True
            run.actions[action_id]["verified"] = bool(verified)
        except Exception as error:
            metadata = {"command": error.command, **error.result} if isinstance(error, CommandError) else None
            error_detail = self.s.errors.capture(run.account, name, error, conversation=run.conversation,
                                                  task=run.task_id, metadata=metadata)
            if metadata:
                result = redact(error.result)
        status = "error" if error_detail else ("success" if run.actions[action_id]["verified"] else "unverified")
        payload = {"id": action_id, "tool": name, "status": status,
                   "error": error_detail, "result": result, "duration_ms": round((time.monotonic() - started) * 1000)}
        raw = json.dumps(payload, ensure_ascii=False)
        bounded = raw
        if len(raw) > 22000:
            bounded = json.dumps({**payload, "result": {
                "truncated_json": json.dumps(result, ensure_ascii=False)[:18000],
                "original_length": len(raw),
            }}, ensure_ascii=False)
        message = {"role": "tool", "tool_name": name, "content": bounded}
        if run.conversation:
            self.s.store.record(run.conversation, message)
        self.s.memory.event(run.account, raw, "error" if error_detail else "observation",
                            name, run.conversation)
        run.send("tool", **payload, agent=agent)
        return message

    async def agent(self, run, role, assignment):
        if run.agents >= 3:
            raise RuntimeError("Bud reached the three-agent limit for this request")
        run.agents += 1
        started = time.monotonic()
        identifier = self.s.store.run(
            "INSERT INTO agent_runs(user_id,conversation,role,assignment,state) VALUES (?,?,?,?,'running')",
            (run.account, run.conversation, role, redact(assignment)),
        )
        run.send("agent", id=identifier, role=role, state="running")
        state, result = "failed", None
        try:
            model = await self.s.ollama.details()
            caps = model.get("capabilities", [])
            structured = run.preferences.tool_protocol == "json" or (
                run.preferences.tool_protocol == "auto" and "tools" not in caps
            )
            allowed = self.executor.available(run, ROLES[role])
            history = [{"role": "system", "content": f"{SYSTEM}\nYou are Bud's {role} specialist. "
                        "Report evidence and uncertainty to Bud without claiming unexecuted actions."},
                       {"role": "user", "content": redact(assignment)}]
            result = await self.cycle(run, history, allowed, structured, steps=5, agent=True)
            state = "completed"
            return {"agent": role, "run_id": identifier, "report": result, "verified": False}
        except asyncio.CancelledError:
            state = "cancelled"
            raise
        except Exception as error:
            result = self.s.errors.capture(run.account, f"agent {role}", error, conversation=run.conversation,
                                           task=run.task_id)
            raise
        finally:
            self.s.store.run("UPDATE agent_runs SET state=?,report=?,elapsed_ms=? WHERE id=?",
                              (state, redact(result), round((time.monotonic() - started) * 1000), identifier))
            run.send("agent", id=identifier, role=role, state=state)
