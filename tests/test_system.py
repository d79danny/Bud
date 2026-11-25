import asyncio
import json
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from budai.backup import copy_data
from budai.config import MODEL, Preferences
from budai.files import extract, modify, read_document, search, write_text
from budai.ollama import Ollama
from budai.process import Terminals, command
from budai.store import Store


class SystemTests(unittest.IsolatedAsyncioTestCase):
    async def test_subprocess_captures_output_exit_and_timeout(self):
        result = await command(["/bin/bash", "-c", "printf out; printf err >&2; exit 17"])
        self.assertEqual((result["stdout"], result["stderr"], result["exit_code"]), ("out", "err", 17))
        timeout = await command(["/bin/bash", "-c", "sleep 30 & wait"], timeout=0.2)
        self.assertTrue(timeout["timed_out"])
        self.assertNotEqual(timeout["exit_code"], 0)

    async def test_pseudo_terminal_reads_input_on_controlling_tty(self):
        terminal = Terminals()
        self.addAsyncCleanup(terminal.close)
        await terminal.start(7, "read -r value </dev/tty; printf 'received %s' \"$value\"", Path.cwd())
        terminal.write(7, "typed text\n")
        for _ in range(100):
            item = terminal.status(7)
            if item["exit_code"] is not None and "received" in item["output"]:
                break
            await asyncio.sleep(0.015)
        self.assertEqual(item["exit_code"], 0, item)
        self.assertEqual(item["output"], "received typed text")

    async def test_ollama_adapter_uses_the_exact_model_and_measured_metrics(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def send_data(self, data):
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                requests.append(self.path)
                self.send_data(json.dumps({"models": [{"name": MODEL}]}).encode())

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["content-length"])))
                requests.append((self.path, data))
                if self.path == "/api/show":
                    payload = {"capabilities": ["tools"], "details": {"quantization_level": "Q3_K_M"}}
                else:
                    payload = {"message": {"content": "model fixture"}, "eval_count": 5,
                               "eval_duration": 1_000_000_000, "done": True}
                self.send_data((json.dumps(payload) + "\n").encode())

            def log_message(self, format, *args):
                return None

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.03})
            thread.start()
            try:
                ollama = Ollama(f"http://127.0.0.1:{server.server_port}")
                self.assertTrue((await ollama.status())["installed"])
                messages = [{"role": "user", "content": "hello"}]
                chunks = [chunk async for chunk in ollama.stream(messages, [], Preferences())]
                self.assertTrue(chunks[0]["done"])
                self.assertEqual(ollama.metrics["tokens_per_second"], 5)
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(MODEL, "hf.co/OBLITERATUS/Qwen3.8-27B-OBLITERATED:Q3_K_M")
        self.assertEqual(requests[-1][1]["model"], MODEL)
        self.assertTrue(requests[-1][1]["stream"])

    def test_file_changes_zip_and_duplicate_search(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = root / "note.txt"
            self.assertTrue(write_text(str(item), "initial")["verified"])
            result = write_text(str(item), "updated", root / "backups")
            self.assertEqual(Path(result["backup"]).read_text(), "initial")
            self.assertEqual(read_document(item), "updated")
            copy = root / "copy.txt"
            self.assertTrue(modify("copy", str(item), str(copy))["verified"])
            self.assertEqual(len(search(str(root), ".txt", duplicates=True)["results"][0]), 2)
            bundle = root / "bundle.zip"
            self.assertTrue(modify("archive", str(item), str(bundle))["verified"])
            target = root / "expanded"
            self.assertTrue(extract(bundle, target)["verified"])
            self.assertEqual((target / "note.txt").read_text(), "updated")
            with zipfile.ZipFile(root / "unsafe.zip", "w") as stream:
                stream.writestr("../outside.txt", "unsafe")
            with self.assertRaises(ValueError):
                extract(root / "unsafe.zip", root / "unsafe-target")
            self.assertFalse((root / "outside.txt").exists())

    def test_backup_restore_preserves_records_and_revokes_restored_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "original"
            store = Store(source)
            store.run("INSERT INTO users(name,password) VALUES ('person','stored hash')")
            store.run("INSERT INTO sessions(token,user_id,expires) VALUES ('digest',1,12345678)")
            store.run("INSERT INTO memories(user_id,content) VALUES (1,'remember this')")
            target = copy_data(source, root / "backup")
            restored = Store(copy_data(target, root / "restored", restore=True))
            self.assertEqual(restored.one("SELECT content FROM memories")["content"], "remember this")
            self.assertIsNone(restored.one("SELECT token FROM sessions"))
            self.assertEqual((root / "restored").stat().st_mode & 0o777, 0o700)
