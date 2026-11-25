import asyncio
import json
import time
from typing import Literal

from pydantic import Field

from .runner import Prompt
from .tools import Grants, Parameters


class NewTask(Parameters):
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(default="", max_length=16000)
    kind: Literal["assistant", "hardware", "media"] = "assistant"
    due: int = Field(default=0, ge=0, le=32503680000)
    interval: int | None = Field(default=None, ge=60, le=31536000)
    priority: int = Field(default=0, ge=-20, le=20)
    depends: int | None = Field(default=None, gt=0)
    files: list[str] = Field(default_factory=list, max_length=8)
    browser: bool = False
    workflow: str | None = Field(default=None, max_length=80)
    workflow_values: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=50)


class Scheduler:
    def __init__(self, services):
        self.s = services
        self.store = services.store
        self.active = {}
        self.worker = None

    def start(self):
        interrupted = self.store.all("SELECT id FROM tasks WHERE state='running'")
        for row in interrupted:
            self.change(row["id"], "paused", "Server restarted during execution; review the recorded history")
        self.worker = asyncio.create_task(self.loop())

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        for task in self.active.values():
            task.cancel()
        await asyncio.gather(*self.active.values(), return_exceptions=True)

    async def loop(self):
        while True:
            ready = self.store.all(
                "SELECT t.* FROM tasks t LEFT JOIN tasks d ON t.depends=d.id WHERE t.state IN ('queued','scheduled') "
                "AND t.due<=? AND (t.depends IS NULL OR d.state='completed' OR "
                "(d.state='scheduled' AND d.result IS NOT NULL)) ORDER BY t.priority DESC,t.due,t.id LIMIT 25",
                (int(time.time()),),
            )
            owners = set(self.active) | set(self.s.runner.active)
            for row in ready:
                account = row["user_id"]
                if account in owners:
                    continue
                owners.add(account)
                worker = asyncio.create_task(self.execute(row))
                self.active[account] = worker
                worker.add_done_callback(lambda task, owner=account: self.finished(owner, task))
            await asyncio.sleep(1)

    def finished(self, account, worker):
        if self.active.get(account) is worker:
            self.active.pop(account)

    def create(self, account, body):
        body.title = body.title.strip()
        if not body.title or (body.kind == "assistant" and not body.prompt.strip()):
            raise ValueError("Enter a title and, for an assistant task, an instruction")
        if body.depends:
            self.entry(account, body.depends)
        if body.kind == "media":
            if not body.workflow:
                raise ValueError("Select a reviewed ComfyUI workflow")
            record = {
                "workflow": body.workflow,
                "fingerprint": self.s.media.fingerprint(body.workflow),
                "values": body.workflow_values,
            }
            payload = json.dumps(record)
        else:
            payload = Grants(files=body.files, browser=body.browser).normalized().model_dump_json()
        now = int(time.time())
        due = body.due or now
        state = "scheduled" if due > now else "queued"
        with self.store.connect() as db:
            identifier = db.execute(
                "INSERT INTO tasks(user_id,title,prompt,kind,payload,due,interval,priority,depends,state) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (account, body.title, body.prompt, body.kind, payload, due,
                 body.interval, body.priority, body.depends, state),
            ).lastrowid
            db.execute("INSERT INTO task_events(task,state,detail) VALUES (?,?,?)", (identifier, state, "Task created"))
        return self.entry(account, identifier)

    def entry(self, account, identifier):
        row = self.store.one("SELECT * FROM tasks WHERE id=? AND user_id=?", (identifier, account))
        if not row:
            raise FileNotFoundError("Task not found")
        return row

    def change(self, identifier, state, detail=None, *, result=None, error=None, due=None):
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET state=?,result=coalesce(?,result),error=?,due=coalesce(?,due) WHERE id=?",
                       (state, result, error, due, identifier))
            db.execute("INSERT INTO task_events(task,state,detail) VALUES (?,?,?)", (identifier, state, detail))

    async def execute(self, row):
        identifier, account = row["id"], row["user_id"]
        run = None
        if self.entry(account, identifier)["state"] not in {"queued", "scheduled"}:
            return
        self.change(identifier, "running", "Execution started")
        self.store.run("UPDATE tasks SET attempts=attempts+1 WHERE id=?", (identifier,))
        try:
            if row["kind"] == "hardware":
                data = await asyncio.to_thread(self.s.monitor.snapshot)
                if not data.get("cpu", {}).get("name") or data["memory"]["total_bytes"] <= 0:
                    raise RuntimeError("The hardware report did not contain a verified CPU and memory reading")
                result = json.dumps(data, ensure_ascii=False)
            elif row["kind"] == "media":
                data = json.loads(row["payload"])
                job = self.s.media.submit(account, data["workflow"], data["values"], expected=data["fingerprint"])
                completed = await self.s.media.wait(account, job)
                if completed["status"] != "completed":
                    raise RuntimeError(completed["error"] or f"Generation ended in state {completed['status']}")
                result = json.dumps(completed, ensure_ascii=False)
            else:
                conversation = row["conversation"]
                if not conversation or not self.store.one("SELECT id FROM conversations WHERE id=?", (conversation,)):
                    conversation = self.store.run("INSERT INTO conversations(user_id,title,created) "
                                                  "VALUES (?,?,CURRENT_TIMESTAMP)", (account, row["title"]))
                    self.store.run("UPDATE tasks SET conversation=? WHERE id=?", (conversation, identifier))
                run = self.s.runner.start(account, conversation, Prompt(
                    text=row["prompt"], source="task", grants=Grants.model_validate_json(row["payload"]),
                ), task_id=identifier)
                await asyncio.shield(run.worker)
                if run.state == "cancelled":
                    self.change(identifier, "paused", "Execution stopped")
                    return
                if not run.outcome or run.state != "completed":
                    raise RuntimeError(run.error or "Bud did not verify the task's outcome with actual tool evidence")
                result = json.dumps({"answer": run.answer, "evidence": run.outcome}, ensure_ascii=False)
            self.change(identifier, "completed", result[:12000], result=result)
            if row["interval"]:
                current = int(time.time())
                next_due = max(current + 1, row["due"] + row["interval"])
                self.change(identifier, "scheduled", "Recurring execution scheduled", due=next_due)
        except asyncio.CancelledError:
            if run:
                await self.s.runner.stop(account)
            self.change(identifier, "paused", "Execution interrupted; review recorded history before resuming")
        except Exception as error:
            detail = self.s.errors.capture(account, "scheduler", error, task=identifier)
            self.change(identifier, "failed", detail, error=detail)
    async def control(self, account, identifier, operation):
        row = self.entry(account, identifier)
        if row["state"] == "running":
            if operation not in {"cancel", "pause"}:
                raise ValueError("Stop the running task before trying it again")
            active = self.active.get(account)
            if active:
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)
        if operation == "cancel":
            self.change(identifier, "cancelled", "Cancelled by user")
        elif operation == "pause":
            self.change(identifier, "paused", "Paused by user")
        elif operation in {"resume", "retry"}:
            self.change(identifier, "queued", "Requeued by user", due=int(time.time()))
        else:
            raise ValueError("Unknown task operation")
        return self.entry(account, identifier)

    async def stop(self, account):
        if account in self.active:
            task = self.active[account]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for row in self.store.all("SELECT id FROM tasks WHERE user_id=? AND state IN ('queued','scheduled')", (account,)):
            self.change(row["id"], "paused", "Emergency stop")
