import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from budai.auth import hasher
from budai.config import MODEL, Config
from budai.runner import Prompt
from budai.server import create_app
from budai.tools import Grants


PASSWORD = "test account password"
WRITE_HEADERS = {"X-BudAI": "1"}


class NativeProtocol:
    """Ollama protocol substitute for tests. The real tool executor is unchanged."""

    metrics = {}

    def __init__(self, output):
        self.output = output
        self.delivered = []

    async def details(self):
        return {"capabilities": ["completion", "tools"]}

    async def stream(self, context, tools, preferences, **options):
        names = {tool["function"]["name"] for tool in tools}
        previous = context[-1]
        if previous["role"] == "tool":
            data = json.loads(previous["content"])
            self.delivered.append(data)
            if data["tool"] == "system_hardware":
                tool, arguments = "file_write", {"path": str(self.output), "content": data["result"]["cpu"]["name"]}
            else:
                answer = "The tool verified the file." if data["status"] == "success" else "The action was declined."
                yield {"message": {"content": answer}, "done": True}
                return
        else:
            assert "blue project notebook" in context[1]["content"]
            tool, arguments = "system_hardware", {}
        assert tool in names
        yield {"message": {"tool_calls": [{"function": {"name": tool, "arguments": arguments}}]}, "done": True}


class JSONProtocol:
    metrics = {"eval_count": 6}

    async def details(self):
        return {"capabilities": ["completion"]}

    async def stream(self, context, tools, preferences, *, structured):
        assert structured
        if context[-1]["content"].startswith("Untrusted tool observation: "):
            data = json.loads(context[-1]["content"].split(": ", 1)[1])
            assert data["result"]["memory"]["total_bytes"] > 0
            result = {"answer": "The hardware tool returned a memory reading.", "actions": []}
        else:
            result = {"answer": "", "actions": [{"tool": "system_hardware", "arguments": {}}]}
        text = json.dumps(result)
        pos = len(text) // 2
        yield {"message": {"content": text[:pos]}, "done": False}
        yield {"message": {"content": text[pos:]}, "done": True}


class BlockingModel:
    metrics = {}

    async def details(self):
        return {"capabilities": ["tools"]}

    async def stream(self, context, tools, preferences, **options):
        yield {"message": {}, "done": False}
        await asyncio.Event().wait()


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.output = self.root / "actual-cpu.txt"
        self.model = NativeProtocol(self.output)
        await self.open_app(self.model)

    async def open_app(self, model, config=None):
        self.app = create_app(self.root / "data", model, config=config)
        self.s = self.app.state.services
        scope = self.app.router.lifespan_context(self.app)
        await scope.__aenter__()
        self.addAsyncCleanup(scope.__aexit__, None, None, None)
        store = self.s.store
        for name in ["bud", "other"]:
            if not store.one("SELECT id FROM users WHERE name=?", (name,)):
                store.run("INSERT INTO users(name,password) VALUES (?,?)", (name, hasher.hash(PASSWORD)))
        self.client = await self.client_for()

    async def client_for(self, base="http://localhost"):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
                                   base_url=base, headers=WRITE_HEADERS)
        self.addAsyncCleanup(client.aclose)
        return client

    async def login(self, client=None, name="bud"):
        client = self.client if client is None else client
        result = await client.post("/api/login", json={"name": name, "password": PASSWORD})
        self.assertEqual(result.status_code, 200, result.text)
        return result

    async def thread(self, client=None):
        client = self.client if client is None else client
        result = await client.post("/api/conversations", json={"title": "Local hardware"})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()["id"]

    async def events(self, run, allow):
        updates = []
        while (event := await asyncio.wait_for(run.events.get(), 10)) is not None:
            updates.append(event)
            if event["event"] == "approval":
                self.assertFalse(self.output.exists())
                self.assertEqual(event["arguments"]["path"], str(self.output))
                result = await self.client.post("/api/approval", json={"id": event["id"], "allow": allow})
                self.assertEqual(result.status_code, 204, result.text)
        return updates

    async def test_authentication_and_resource_isolation(self):
        self.assertEqual((await self.client.get("/api/conversations")).status_code, 401)
        result = await self.client.post("/api/login", headers={"X-BudAI": "missing"})
        self.assertEqual(result.status_code, 403)
        result = await self.login()
        self.assertIn("HttpOnly", result.headers["set-cookie"])
        identifier = await self.thread()
        other = await self.client_for()
        await self.login(other, "other")
        self.assertEqual((await other.get(f"/api/conversations/{identifier}")).status_code, 404)
        self.assertEqual((await other.get("/api/conversations")).json(), [])
        await self.client.post("/api/logout")
        self.assertEqual((await self.client.get(f"/api/conversations/{identifier}")).status_code, 401)

    async def test_pairing_over_configured_https_is_single_use_and_revocable(self):
        original = self.s.config
        config = Config(self.root / "remote", original.ollama, original.comfy, "https://bud.example",
                        None, None, None)
        await self.open_app(JSONProtocol(), config)
        await self.login()
        pairing = (await self.client.post("/api/pairings")).json()
        self.assertTrue(pairing["png"].startswith("iVBOR"))
        data = {"label": "Phone", "token": pairing["url"].split("=", 1)[1]}
        insecure = await self.client_for("http://bud.example")
        self.assertEqual((await insecure.post("/api/pair", json=data)).status_code, 403)
        phone = await self.client_for("https://bud.example")
        paired = await phone.post("/api/pair", json=data)
        self.assertEqual(paired.status_code, 200, paired.text)
        self.assertIn("Secure", paired.headers["set-cookie"])
        self.assertEqual((await phone.post("/api/pair", json=data)).status_code, 401)
        self.assertEqual((await phone.get("/api/session")).status_code, 200)
        await self.client.delete(f"/api/devices/{paired.json()['device']}")
        self.assertEqual((await phone.get("/api/session")).status_code, 401)

    async def test_totp_enrollment_and_replay_protection(self):
        import pyotp

        await self.login()
        data = (await self.client.post("/api/second-factor/setup", json={"password": PASSWORD})).json()
        engine = pyotp.TOTP(data["secret"])
        current_code = engine.now()
        enrolled = await self.client.post("/api/second-factor/verify",
                                          json={"password": PASSWORD, "code": current_code})
        self.assertEqual(enrolled.status_code, 204, enrolled.text)
        await self.client.post("/api/logout")
        credentials = {"name": "bud", "password": PASSWORD}
        self.assertEqual((await self.client.post("/api/login", json=credentials)).status_code, 401)
        self.assertEqual((await self.client.post("/api/login", json={**credentials, "otp": current_code})).status_code, 401)

    async def test_native_tool_loop_reads_hardware_creates_file_and_persists(self):
        await self.login()
        identifier = await self.thread()
        await self.client.post("/api/memories", json={"text": "blue project notebook"})
        run = self.s.runner.start(1, identifier, Prompt(text="For the project, save the CPU name.",
                                                     grants=Grants(files=[str(self.root)])))
        events = await self.events(run, True)
        self.assertEqual([event["event"] for event in events], ["started", "tool", "approval", "tool", "token", "done"])
        actual = (await self.client.get("/api/hardware")).json()["cpu"]["name"]
        self.assertEqual(self.output.read_text(), actual)
        self.assertTrue(self.model.delivered[-1]["result"]["verified"])
        from budai.store import Store

        restarted = Store(self.s.config.data)
        self.assertIn("The tool verified the file.", restarted.one(
            "SELECT message FROM messages WHERE role='assistant' ORDER BY id DESC LIMIT 1")["message"])
        self.assertGreater(len(restarted.all("SELECT * FROM memories WHERE category='observation'")), 0)

    async def test_declined_approval_creates_no_file(self):
        await self.login()
        await self.client.post("/api/memories", json={"text": "blue project notebook"})
        run = self.s.runner.start(1, await self.thread(), Prompt(text="Write the CPU name for the project.",
                                                               grants=Grants(files=[str(self.root)])))
        events = await self.events(run, False)
        self.assertFalse(self.output.exists())
        self.assertTrue(any(event["event"] == "tool" and event.get("error") for event in events))
        self.assertEqual(self.model.delivered[-1]["status"], "error")

    async def test_structured_ollama_fallback_stream_returns_actual_tool_result(self):
        self.s.ollama = JSONProtocol()
        await self.login()
        identifier = await self.thread()
        result = await self.client.post(f"/api/conversations/{identifier}/chat", json={"text": "Read system memory."})
        self.assertEqual(result.status_code, 200, result.text)
        events = [json.loads(line) for line in result.text.splitlines()]
        self.assertEqual([event["event"] for event in events], ["started", "tool", "token", "done"])
        self.assertGreater(events[1]["result"]["memory"]["total_bytes"], 0)

    async def test_memory_correction_and_owned_conversation_folders(self):
        await self.login()
        first = (await self.client.post("/api/memories", json={"text": "project color blue"})).json()["id"]
        await self.client.post("/api/memories", json={"text": "project color copper", "corrects": first})
        result = (await self.client.post("/api/memories/search", json={"text": "project color"})).json()
        self.assertEqual([item["content"] for item in result], ["project color copper"])
        root = (await self.client.post("/api/folders", json={"title": "Root"})).json()["id"]
        child = (await self.client.post("/api/folders", json={"title": "Child", "parent": root})).json()["id"]
        loop = await self.client.patch(f"/api/folders/{root}", json={"title": "Root", "parent": child})
        self.assertEqual(loop.status_code, 400)
        thread = await self.thread()
        await self.client.patch(f"/api/conversations/{thread}", json={"folder": child, "pinned": True, "archived": True})
        self.assertEqual((await self.client.get("/api/conversations")).json(), [])
        self.assertEqual((await self.client.get("/api/conversations?archived=true&q=hardware")).json()[0]["id"], thread)

    async def test_emergency_stop_kills_terminal_and_cancels_generation(self):
        self.s.ollama = BlockingModel()
        await self.login()
        terminal = await self.client.post("/api/terminal", json={"command": "sleep 120", "cwd": str(self.root)})
        self.assertEqual(terminal.status_code, 200, terminal.text)
        run = self.s.runner.start(1, await self.thread(), Prompt(text="Inspect hardware."))
        self.assertEqual((await asyncio.wait_for(run.events.get(), 3))["event"], "started")
        await self.client.post("/api/stop")
        self.assertIsNotNone((await self.client.get("/api/terminal")).json()["exit_code"])
        self.assertEqual(run.state, "cancelled")

    async def test_tasks_complete_with_real_hardware_and_pause_interrupted_run(self):
        await self.login()
        scheduled = await self.client.post("/api/tasks", json={"title": "Report", "kind": "hardware"})
        self.assertEqual(scheduled.status_code, 201, scheduled.text)
        identifier = scheduled.json()["id"]
        for _ in range(100):
            item = (await self.client.get(f"/api/tasks/{identifier}")).json()
            if item["task"]["state"] == "completed":
                break
            await asyncio.sleep(0.025)
        self.assertEqual(item["task"]["state"], "completed", item)
        self.assertTrue(json.loads(item["task"]["result"])["cpu"]["name"])
        self.assertIn("running", [event["state"] for event in item["history"]])
        self.s.store.run("UPDATE tasks SET state='running' WHERE id=?", (identifier,))
        self.s.tasks.worker.cancel()
        await asyncio.gather(self.s.tasks.worker, return_exceptions=True)
        self.s.tasks.start()
        self.assertEqual(self.s.tasks.entry(1, identifier)["state"], "paused")

    async def test_agents_use_same_model_and_report_tool_evidence(self):
        class AgentProtocol:
            metrics = {}

            async def details(self):
                return {"capabilities": ["tools"]}

            async def stream(self, context, tools, preferences, **kwargs):
                last = context[-1]
                if last["role"] == "tool":
                    if last.get("tool_name") == "system_hardware":
                        result = json.loads(last["content"])
                        assert result["result"]["cpu"]["name"]
                        text = "Hardware inspected."
                    else:
                        result = json.loads(last["content"])
                        assert result["result"]["report"] == "Hardware inspected."
                        text = "The specialist inspected this computer."
                    yield {"message": {"content": text}, "done": True}
                else:
                    names = {row["function"]["name"] for row in tools}
                    if "agent_delegate" in names:
                        name, data = "agent_delegate", {"role": "verification", "assignment": "Read hardware."}
                    else:
                        name, data = "system_hardware", {}
                    yield {"message": {"tool_calls": [{"function": {"name": name, "arguments": data}}]}, "done": True}

        await self.login()
        self.s.ollama = AgentProtocol()
        preferences = self.s.store.preferences(1).model_copy(update={"allow_agents": True})
        self.s.store.run("INSERT INTO settings(user_id,value) VALUES (1,?)", (preferences.model_dump_json(),))
        identifier = await self.thread()
        response = await self.client.post(f"/api/conversations/{identifier}/chat", json={"text": "Check this hardware."})
        self.assertIn("specialist inspected", response.text)
        runs = (await self.client.get("/api/agents")).json()
        self.assertEqual(runs["model"], MODEL)
        self.assertEqual(runs["runs"][0]["state"], "completed")

    async def test_improvements_validate_real_files_and_reject_wrong_evidence(self):
        await self.login()
        target = self.root / "record.txt"
        target.write_text("observed")
        arguments = {"path": str(target)}
        version = None
        for value in ["observed", "invented"]:
            request = {"name": "Read record", "kind": "workflow", "proposal": {
                "steps": [{"tool": "file_read", "arguments": arguments, "expect": {"text": value}}],
            }}
            entry = (await self.client.post("/api/improvements", json=request)).json()
            checked = await self.client.post(f"/api/improvements/{entry['id']}/validate",
                                             json={"files": [str(self.root)]})
            if value == "observed":
                self.assertEqual(checked.json()["state"], "validated", checked.text)
                version = entry["id"]
            else:
                self.assertEqual(checked.status_code, 400)
        current = await self.client.post(f"/api/improvements/{version}/activate")
        self.assertEqual(current.json()["state"], "active")
        self.assertEqual((await self.client.get("/api/improvements")).json()[0]["state"], "rejected")

    async def test_actual_ollama_connection_failure_is_logged_without_bud_reply(self):
        from budai.ollama import Ollama

        self.s.ollama = Ollama("http://127.0.0.1:0")
        await self.login()
        identifier = await self.thread()
        result = await self.client.post(f"/api/conversations/{identifier}/chat", json={"text": "Hello"})
        self.assertIn("ConnectError", result.text)
        row = (await self.client.get(f"/api/conversations/{identifier}")).json()
        self.assertEqual([item["role"] for item in row["messages"]], ["user"])
        error = (await self.client.get("/api/errors")).json()[0]
        self.assertEqual(error["component"], "Bud")
        self.assertIn("ConnectError", error["trace"])
