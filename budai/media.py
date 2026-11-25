import asyncio
import copy
import hashlib
import json
import mimetypes
import re
import time
import uuid
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from .artifacts import SAFE_INLINE, MIB


class Input(BaseModel):
    label: str
    node: str
    name: str
    type: str = Field(pattern="^(string|integer|number|boolean|image|video)$")


class Workflow(BaseModel):
    title: str
    kind: str = Field(pattern="^(image|video)$")
    graph: str = Field(pattern=r"^[\w.-]+\.json$")
    inputs: dict[str, Input]
    hardware: str | None = None


class Media:
    def __init__(self, config, store, artifacts, ollama, gpu, errors):
        self.url = config.comfy
        self.directory = config.data / "workflows"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.store, self.artifacts, self.ollama, self.gpu, self.errors = store, artifacts, ollama, gpu, errors
        self.jobs = {}
        self.store.run("UPDATE media_jobs SET status='interrupted' WHERE status IN ('queued','running')")

    async def request(self, path, *, payload=None, params=None, files=None):
        async with httpx.AsyncClient(timeout=httpx.Timeout(35, connect=5), trust_env=False) as client:
            response = await client.request("POST" if payload is not None or files else "GET",
                                            self.url + path, json=payload, params=params, files=files)
            if response.is_error:
                raise RuntimeError(f"ComfyUI {path} HTTP {response.status_code}: {response.text[:4000]}")
            return response.json()

    def workflows(self):
        available = []
        for path in sorted(self.directory.glob("*.workflow.json")):
            try:
                info = Workflow.model_validate_json(path.read_text())
                if not (self.directory / info.graph).is_file():
                    raise FileNotFoundError(info.graph)
                available.append({"id": path.name[:-len(".workflow.json")], **info.model_dump()})
            except (ValueError, OSError) as error:
                available.append({"id": path.name, "error": str(error)})
        return available

    def workflow(self, name):
        if not re.fullmatch(r"[\w.-]{1,80}", name):
            raise ValueError("Invalid workflow ID")
        info = Workflow.model_validate_json((self.directory / f"{name}.workflow.json").read_text())
        graph = json.loads((self.directory / info.graph).read_text())
        if not isinstance(graph, dict) or not graph:
            raise ValueError("Export the workflow in ComfyUI's API format")
        return info, graph

    def fingerprint(self, name):
        info, graph = self.workflow(name)
        return self.graph_digest(info, graph)

    @staticmethod
    def graph_digest(info, graph):
        data = json.dumps({"definition": info.model_dump(), "graph": graph}, sort_keys=True).encode()
        return hashlib.sha256(data).hexdigest()

    async def validate(self, graph):
        object_info = await self.request("/object_info")
        if len(graph) > 250:
            raise ValueError("Workflow has more than 250 nodes")
        for node in graph.values():
            name = node["class_type"]
            if name not in object_info:
                raise ValueError(f"The connected ComfyUI lacks node {name}")
            sections = object_info[name].get("input", {})
            definitions = {**sections.get("required", {}), **sections.get("optional", {})}
            for key, value in node.get("inputs", {}).items():
                options = definitions.get(key, [None])[0]
                if isinstance(options, list) and isinstance(value, str) and value not in options:
                    raise ValueError(f"{name}.{key}: {value} is unavailable in ComfyUI")
        return {"nodes": len(graph), "compatible": True}

    async def prepare(self, account, info, graph, values):
        if set(values) - set(info.inputs):
            raise ValueError("A supplied parameter is missing from the workflow descriptor")
        prepared = copy.deepcopy(graph)
        uploads = []
        types = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "image": str, "video": str}
        for key, value in values.items():
            field = info.inputs[key]
            if not isinstance(value, types[field.type]) or (field.type != "boolean" and isinstance(value, bool)):
                raise ValueError(f"{field.label} requires {field.type}")
            if field.type in {"image", "video"}:
                item = self.artifacts.entry(account, value)
                allowed = {"image/png", "image/jpeg", "image/webp"} if field.type == "image" else {"video/mp4", "video/webm"}
                if item["mime"] not in allowed or item["bytes"] > 16 * MIB:
                    raise ValueError(f"Reference {field.type} must use a supported format under 16 MiB")
                uploads.append((field, value, item))
            else:
                prepared[field.node]["inputs"][field.name] = value
        for field, value, item in uploads:
            path = self.artifacts.path(account, value)
            content = path.read_bytes()
            if not self.check_media(content, item["mime"]):
                raise ValueError(f"{item['name']} does not match its declared media format")
            file = (f"bud-{value}{Path(item['name']).suffix}", content, item["mime"])
            remote = await self.request("/upload/image", files={"image": file, "overwrite": (None, "false")})
            prepared[field.node]["inputs"][field.name] = (
                f"{remote['subfolder']}/{remote['name']}" if remote.get("subfolder") else remote["name"]
            )
        await self.validate(prepared)
        return prepared

    def submit(self, account, name, values, expected=None):
        info, graph = self.workflow(name)
        if expected is not None and self.graph_digest(info, graph) != expected:
            raise RuntimeError("The scheduled workflow changed since approval")
        identifier = uuid.uuid4().hex
        self.store.run("INSERT INTO media_jobs(id,user_id,workflow,status) VALUES (?,?,?,'queued')",
                       (identifier, account, name))
        self.jobs[identifier] = (account, asyncio.create_task(self.execute(account, identifier, info, graph, values)))
        return identifier

    def entry(self, account, identifier):
        row = self.store.one("SELECT * FROM media_jobs WHERE id=? AND user_id=?", (identifier, account))
        if not row:
            raise FileNotFoundError("Generation job is unavailable")
        row["outputs"] = json.loads(row["outputs"] or "[]")
        row["progress"] = json.loads(row["progress"] or "null")
        return row

    @staticmethod
    def check_media(data, mime):
        known = {
            "image/png": data[:8] == b"\x89PNG\r\n\x1a\n",
            "image/jpeg": data[:2] == b"\xff\xd8",
            "image/webp": data[:4] == b"RIFF" and data[8:12] == b"WEBP",
            "video/mp4": data[4:8] == b"ftyp",
            "video/webm": data[:4] == b"\x1a\x45\xdf\xa3",
            "audio/wav": data[:4] == b"RIFF" and data[8:12] == b"WAVE",
            "audio/ogg": data[:4] == b"OggS",
            "audio/mpeg": data[:3] == b"ID3" or bool(data and data[0] == 0xFF),
        }
        return known.get(mime, False)

    async def save_output(self, account, item):
        name = item["filename"]
        mime = mimetypes.guess_type(name)[0]
        if mime == "audio/x-wav":
            mime = "audio/wav"
        if mime not in SAFE_INLINE:
            return None
        params = {"filename": name, "subfolder": item.get("subfolder", ""), "type": item.get("type", "output")}
        data = bytearray()
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            async with client.stream("GET", self.url + "/view", params=params) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 128 * MIB:
                        raise ValueError(f"{name} exceeds the 128 MiB media limit")
        if not self.check_media(data, mime):
            raise ValueError(f"ComfyUI returned an invalid {mime} file header for {name}")
        return self.artifacts.save(account, name, data, "generation", mime=mime, limit=128 * MIB)

    async def execute(self, account, identifier, info, graph, values):
        remote = None
        try:
            async with self.gpu:
                self.store.run("UPDATE media_jobs SET status='running',progress=? WHERE id=?",
                               (json.dumps({"phase": "validating workflow"}), identifier))
                graph = await self.prepare(account, info, graph, values)
                if self.store.preferences(account).share_gpu:
                    try:
                        await self.ollama.unload()
                    except (httpx.HTTPError, RuntimeError) as error:
                        self.errors.capture(account, "Ollama GPU coordination", error)
                result = await self.request("/prompt", payload={"prompt": graph, "client_id": identifier})
                remote = result["prompt_id"]
                self.store.run("UPDATE media_jobs SET remote_id=?,progress=? WHERE id=?",
                               (remote, json.dumps({"phase": "submitted"}), identifier))
                previous = None
                deadline = time.monotonic() + 3600
                while time.monotonic() < deadline:
                    data = (await self.request(f"/history/{remote}")).get(remote)
                    if data:
                        status = data.get("status", {})
                        if status.get("status_str") == "error":
                            raise RuntimeError(f"ComfyUI workflow failed: {status.get('messages')}")
                        if status.get("completed"):
                            outputs = []
                            for node in data.get("outputs", {}).values():
                                for key in ("images", "gifs", "videos", "audio"):
                                    for item in node.get(key, []):
                                        if entry := await self.save_output(account, item):
                                            outputs.append(entry)
                            expected = info.kind + "/"
                            if not any(item["mime"].startswith(expected) for item in outputs):
                                raise RuntimeError(f"ComfyUI completed without a verifiable {info.kind} output")
                            self.store.run("UPDATE media_jobs SET status='completed',outputs=?,progress=? WHERE id=?",
                                           (json.dumps(outputs), json.dumps({"phase": "files verified"}), identifier))
                            return
                    queue = await self.request("/queue")
                    pending = [item[1] for item in queue.get("queue_pending", [])]
                    running = {item[1] for item in queue.get("queue_running", [])}
                    progress = {"phase": "executing" if remote in running else "waiting for ComfyUI"}
                    if remote in pending:
                        progress["queue_position"] = pending.index(remote) + 1
                    if progress != previous:
                        self.store.run("UPDATE media_jobs SET progress=? WHERE id=?",
                                       (json.dumps(progress), identifier))
                        previous = progress
                    await asyncio.sleep(1)
                raise TimeoutError("ComfyUI job exceeded its one-hour observation limit")
        except asyncio.CancelledError:
            if remote:
                try:
                    await self.request("/queue", payload={"delete": [remote]})
                    queue = await self.request("/queue")
                    if any(item[1] == remote for item in queue.get("queue_running", [])):
                        await self.request("/interrupt", payload={"prompt_id": remote})
                except Exception as error:
                    self.errors.capture(account, "ComfyUI cancellation", error)
            self.store.run("UPDATE media_jobs SET status='cancelled' WHERE id=?", (identifier,))
        except Exception as error:
            detail = self.errors.capture(account, "ComfyUI", error)
            self.store.run("UPDATE media_jobs SET status='failed',error=? WHERE id=?", (detail, identifier))
        finally:
            self.jobs.pop(identifier, None)

    async def wait(self, account, identifier):
        try:
            task = self.jobs.get(identifier)
            if task:
                await asyncio.shield(task[1])
        except asyncio.CancelledError:
            await self.stop(account, identifier)
            raise
        return self.entry(account, identifier)

    async def stop(self, account, identifier=None):
        tasks = [task for job, (owner, task) in self.jobs.items()
                 if owner == account and (identifier is None or job == identifier)]
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for job, (owner, task) in list(self.jobs.items()):
            if owner == account and (identifier is None or job == identifier) and task.cancelled():
                self.jobs.pop(job)
                self.store.run("UPDATE media_jobs SET status='cancelled' WHERE id=?", (job,))
