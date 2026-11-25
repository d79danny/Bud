import asyncio
import os
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from budai.artifacts import Artifacts
from budai.config import Config
from budai.desktop_control import Desktop
from budai.store import Store
from budai.process import CommandError
from budai.voice import Voice


class HostTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.environ.get("BUDAI_TEST_X11") == "1", "needs a disposable X11 desktop with a window manager")
    async def test_x11_screenshot_ocr_pointer_keyboard_and_windows(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store = Store(directory)
            store.run("INSERT INTO users(name,password) VALUES ('test','unused')")
            desktop = Desktop(Artifacts(store))
            record = directory / "typed"
            source = """
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QVBoxLayout, QWidget
app = QApplication([])
window = QWidget()
window.setWindowTitle("BudAI X11 integration test")
window.resize(600, 240)
layout = QVBoxLayout(window)
label = QLabel("BUDAI SCREEN CHECK")
label.setStyleSheet("font-size: 33px; background: white; color: black;")
entry = QLineEdit()
entry.textChanged.connect(lambda value: Path(sys.argv[1]).write_text(value))
layout.addWidget(label)
layout.addWidget(entry)
entry.setFocus()
window.show()
app.exec()
"""
            app = await asyncio.create_subprocess_exec(sys.executable, "-c", source, str(record))
            try:
                window = None
                for _ in range(75):
                    try:
                        listing = await desktop.windows()
                    except CommandError:
                        await asyncio.sleep(0.075)
                        continue
                    window = next((row for row in listing["windows"] if "BudAI X11 integration" in row["title"]), None)
                    if window:
                        break
                    await asyncio.sleep(0.075)
                self.assertIsNotNone(window)
                await desktop.window("switch", identifier=window["id"])
                observation, data, mime = await desktop.capture(1, scope="window", ocr=True)
                self.assertTrue(data.startswith(b"\xff\xd8"))
                self.assertEqual(mime, "image/jpeg")
                self.assertIn("SCREEN CHECK", observation["ocr"])
                self.assertTrue(await desktop.monitors())
                await desktop.input("move", x=210, y=140)
                await desktop.input("type", value="Real keyboard event")
                for _ in range(40):
                    if record.exists() and record.read_text() == "Real keyboard event":
                        break
                    await asyncio.sleep(0.025)
                self.assertEqual(record.read_text(), "Real keyboard event")
                if shutil.which("xclip"):
                    clipboard = await desktop.set_clipboard(1, "Real clipboard value")
                    self.assertTrue(clipboard["verified"])
                    self.assertEqual(await desktop.clipboard(), "Real clipboard value")
                    await desktop.stop(1)
            finally:
                if app.returncode is None:
                    app.terminate()
                await app.wait()

    @unittest.skipUnless(shutil.which("espeak-ng") or shutil.which("espeak"), "needs a local eSpeak installation")
    async def test_espeak_generates_real_audio_samples(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Config.from_env(temporary)
            store = Store(config.data)
            store.run("INSERT INTO users(name,password) VALUES ('test','unused')")
            artifacts = Artifacts(store)
            item = await Voice(config, artifacts).speak(1, "BudAI audio verification.", 165, "en")
            with wave.open(str(artifacts.path(1, item["id"]))) as audio:
                self.assertGreater(audio.getnframes(), 1000)
                self.assertTrue(any(audio.readframes(audio.getnframes())))
            self.assertEqual(item["mime"], "audio/wav")

    @unittest.skipUnless(os.environ.get("BUDAI_TEST_WHISPER") == "1", "needs a local recognizer, a model and a test WAV")
    async def test_local_speech_recognizer_transcribes_real_audio(self):
        source = Path(os.environ["BUDAI_TEST_WAV"])
        with tempfile.TemporaryDirectory() as temporary:
            config = Config.from_env(temporary)
            artifacts = Artifacts(Store(config.data))
            result = await Voice(config, artifacts).transcribe(source.read_bytes(), "en")
            self.assertTrue(result["text"])
            self.assertGreater(result["segments"], 0)

    @unittest.skipUnless(os.environ.get("BUDAI_TEST_COMFYUI") == "1", "needs a running local ComfyUI instance")
    async def test_real_comfyui_api_generates_image_and_video(self):
        import json
        import subprocess

        from budai.service import Services

        with tempfile.TemporaryDirectory() as temporary:
            config = Config.from_env(temporary)
            service = Services(config)
            self.addAsyncCleanup(service.close)
            service.store.run("INSERT INTO users(name,password) VALUES ('test','unused')")
            service.store.run("INSERT INTO settings(user_id,value) VALUES (1,'{\"share_gpu\":false}')")
            status = await service.media.request("/system_stats")
            self.assertIn("system", status)
            workflows = {
                "image": {"2": {"class_type": "SaveImage", "inputs": {
                    "images": ["1", 0], "filename_prefix": "budai-smoke/image"}}},
                "video": {
                    "2": {"class_type": "RepeatImageBatch", "inputs": {"image": ["1", 0], "amount": 8}},
                    "3": {"class_type": "CreateVideo", "inputs": {"images": ["2", 0], "fps": 8.0}},
                    "4": {"class_type": "SaveVideo", "inputs": {
                        "video": ["3", 0], "filename_prefix": "budai-smoke/video", "format": "auto", "codec": "auto",
                    }},
                },
            }
            for kind, nodes in workflows.items():
                with self.subTest(kind=kind):
                    graph = {"1": {"class_type": "EmptyImage", "inputs": {
                        "width": 64, "height": 64, "batch_size": 1, "color": 0x446688,
                    }}, **nodes}
                    (service.media.directory / (kind + ".json")).write_text(json.dumps(graph))
                    (service.media.directory / (kind + ".workflow.json")).write_text(json.dumps({
                        "title": "BudAI integration fixture", "kind": kind, "graph": kind + ".json", "inputs": {},
                    }))
                    identifier = service.media.submit(1, kind, {})
                    job = await asyncio.wait_for(service.media.wait(1, identifier), 80)
                    self.assertEqual(job["status"], "completed", job)
                    result = job["outputs"][0]
                    self.assertTrue(result["mime"].startswith(kind + "/"))
                    path = service.artifacts.path(1, result["id"])
                    if kind == "image":
                        self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
                    else:
                        info = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                                              capture_output=True, text=True, check=True, timeout=10)
                        stream = json.loads(info.stdout)["streams"][0]
                        self.assertEqual((stream["width"], stream["height"]), (64, 64))
                        self.assertEqual(stream["codec_type"], "video")

            from budai.tasks import NewTask

            scheduled = service.tasks.create(1, NewTask(title="Scheduled image", kind="media", workflow="image"))
            await service.tasks.execute(scheduled)
            self.assertEqual(service.tasks.entry(1, scheduled["id"])["state"], "completed")

            original = result
            graph = {
                "1": {"class_type": "LoadVideo", "inputs": {"file": ""}},
                "2": {"class_type": "SaveVideo", "inputs": {
                    "video": ["1", 0], "filename_prefix": "budai-smoke/reference", "format": "auto", "codec": "auto",
                }},
            }
            (service.media.directory / "reference.json").write_text(json.dumps(graph))
            (service.media.directory / "reference.workflow.json").write_text(json.dumps({
                "title": "Video reference", "kind": "video", "graph": "reference.json", "inputs": {
                    "source": {"label": "Source video", "node": "1", "name": "file", "type": "video"},
                },
            }))
            identifier = service.media.submit(1, "reference", {"source": original["id"]})
            copied = await asyncio.wait_for(service.media.wait(1, identifier), 80)
            self.assertEqual(copied["status"], "completed", copied)
            self.assertEqual(copied["outputs"][0]["mime"], "video/mp4")

    @unittest.skipUnless(os.environ.get("BUDAI_TEST_OLLAMA") == "1", "needs the exact installed Ollama model")
    async def test_exact_ollama_model_selects_tools_and_verifies_a_gpu_file(self):
        import json

        from budai.runner import Prompt
        from budai.service import Services
        from budai.tools import Grants

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = Services(Config.from_env(root / "state"))
            try:
                status = await service.ollama.status()
                self.assertTrue(status["installed"], status)
                gpu = (await asyncio.to_thread(service.monitor.snapshot))["gpu"]["names"]
                if not gpu:
                    self.skipTest("the GPU example requires a host with a reported GPU")
                service.store.run("INSERT INTO users(name,password) VALUES ('test','unused')")
                conversation = service.store.run("INSERT INTO conversations(user_id,title) VALUES (1,'GPU file')")
                path = root / "gpu.txt"
                request = Prompt(
                    text=f"Use the hardware tool to read the first GPU model. Write that model name to {path} "
                         "as a plain text file and verify that it was saved.",
                    grants=Grants(files=[str(root)]),
                )
                run = service.runner.start(1, conversation, request)
                action_records = []
                async with asyncio.timeout(900):
                    while (event := await run.events.get()) is not None:
                        if event["event"] == "approval":
                            permitted = event["tool"] == "file_write" and event["arguments"]["path"] == str(path)
                            service.runner.approve(1, event["id"], permitted)
                        if event["event"] == "tool":
                            action_records.append(event)
                self.assertEqual(run.state, "completed", run.error)
                self.assertTrue(any(item["tool"] == "system_hardware" for item in action_records))
                self.assertTrue(any(item["tool"] == "file_write" and item["status"] == "success"
                                    for item in action_records), json.dumps(action_records))
                self.assertIn(gpu[0], path.read_text())
                self.assertTrue(run.answer)
            finally:
                await service.close()
