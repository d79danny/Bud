import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

from .process import command


class Voice:
    def __init__(self, config, artifacts):
        self.config = config
        self.artifacts = artifacts

    def status(self):
        model = self.config.whisper_model
        engine = shutil.which("whisper-cli") if model else None
        present = bool(model and model.is_file())
        if self.config.sherpa_model:
            model = self.config.sherpa_model
            engine = "sherpa-onnx" if importlib.util.find_spec("sherpa_onnx") else None
            present = all((model / name).is_file() for name in (
                "tiny.en-encoder.int8.onnx", "tiny.en-decoder.int8.onnx", "tiny.en-tokens.txt",
            ))
        return {"ffmpeg": shutil.which("ffmpeg"), "recognizer": engine,
                "model": str(model) if model else None, "model_exists": present,
                "speaker": shutil.which("espeak-ng") or shutil.which("espeak")}

    async def transcribe(self, data, language="auto"):
        profile = self.status()
        if not profile["model_exists"]:
            raise FileNotFoundError("Configure a local model with BUDAI_SHERPA_MODEL or BUDAI_WHISPER_MODEL")
        if not profile["recognizer"]:
            raise FileNotFoundError("The speech recognizer for the configured model is unavailable")
        model = profile["model"]
        if not data or len(data) > 16 * 1024 * 1024:
            raise ValueError("Audio must be between 1 byte and 16 MiB")
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            recorded, wav, output = directory / "recorded", directory / "voice.wav", directory / "speech"
            recorded.write_bytes(data)
            await command(["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
                           "-i", str(recorded), "-t", "90", "-vn", "-ac", "1", "-ar", "16000", str(wav)],
                          check=True, timeout=25)
            if profile["recognizer"] == "sherpa-onnx":
                worker = Path(__file__).with_name("speech_worker.py")
                result = await command([sys.executable, str(worker), model, str(wav), language],
                                       check=True, timeout=240)
                return json.loads(result["stdout"])
            await command(["whisper-cli", "-m", model, "-f", str(wav), "-l", language,
                           "-oj", "-of", str(output), "-nt"], check=True, timeout=240)
            result = json.loads(output.with_suffix(".json").read_text())
            text = " ".join(item["text"].strip() for item in result["transcription"]).strip()
            return {"text": text, "language": result.get("result", {}).get("language"),
                    "segments": len(result["transcription"])}

    async def speak(self, account, text, speed, voice):
        executable = shutil.which("espeak-ng") or shutil.which("espeak")
        if not executable:
            raise FileNotFoundError("Install espeak-ng for local speech output")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bud.wav"
            source = Path(directory) / "text.txt"
            source.write_text(text)
            await command([executable, "-s", str(speed), "-v", voice, "-f", str(source), "-w", str(output)],
                          check=True, timeout=60)
            return self.artifacts.save(account, "bud.wav", output.read_bytes(), "speech", mime="audio/wav")

    async def voices(self):
        executable = shutil.which("espeak-ng") or shutil.which("espeak")
        if not executable:
            raise FileNotFoundError("Install espeak-ng for local speech output")
        result = await command([executable, "--voices"], check=True)
        return result["stdout"]
