import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field


MODEL = "hf.co/OBLITERATUS/Qwen3.8-27B-OBLITERATED:Q3_K_M"


def loopback_endpoint(value: str) -> str:
    url = urlsplit(value)
    try:
        local = url.hostname == "localhost" or ipaddress.ip_address(url.hostname or "").is_loopback
    except ValueError:
        local = False
    if url.scheme not in {"https", "http"} or not local or url.username or url.password:
        raise ValueError("Service endpoints must use HTTP(S) and a loopback address")
    if url.path not in {"", "/"} or url.query or url.fragment:
        raise ValueError("Service endpoints cannot contain a path, query or fragment")
    return value.rstrip("/")


@dataclass(frozen=True)
class Config:
    data: Path
    ollama: str
    comfy: str
    origin: str | None
    chromium: str | None
    whisper_model: Path | None
    embedding_model: str | None
    sherpa_model: Path | None = None

    @classmethod
    def from_env(cls, directory=None):
        default = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "budai"
        data = Path(directory or os.environ.get("BUDAI_DATA_DIR", default)).expanduser().resolve()
        origin = os.environ.get("BUDAI_ORIGIN")
        if origin:
            url = urlsplit(origin)
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("BUDAI_ORIGIN must be an HTTPS origin")
            if url.path not in {"", "/"} or url.query or url.fragment:
                raise ValueError("BUDAI_ORIGIN cannot contain a path, query or fragment")
            origin = f"https://{url.netloc}"
        whisper = os.environ.get("BUDAI_WHISPER_MODEL")
        sherpa = os.environ.get("BUDAI_SHERPA_MODEL")
        return cls(
            data,
            loopback_endpoint(os.environ.get("BUDAI_OLLAMA_URL", "http://127.0.0.1:11434")),
            loopback_endpoint(os.environ.get("BUDAI_COMFYUI_URL", "http://127.0.0.1:8188")),
            origin,
            os.environ.get("BUDAI_CHROMIUM"),
            Path(whisper).expanduser().resolve() if whisper else None,
            os.environ.get("BUDAI_EMBEDDING_MODEL"),
            Path(sherpa).expanduser().resolve() if sherpa else None,
        )


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context: int = Field(default=4096, ge=1024, le=131072)
    temperature: float = Field(default=0.4, ge=0, le=2)
    output_tokens: int = Field(default=1024, ge=64, le=32768)
    threads: int | None = Field(default=None, ge=1, le=512)
    gpu_layers: int | None = Field(default=None, ge=0, le=999)
    keep_alive: int = Field(default=300, ge=0, le=86400)
    timeout: int = Field(default=180, ge=10, le=1800)
    retrieval: int = Field(default=5, ge=0, le=20)
    tool_protocol: str = Field(default="auto", pattern="^(auto|native|json)$")
    allow_agents: bool = False
    share_gpu: bool = True
    speech_speed: int = Field(default=175, ge=80, le=450)
    voice: str = Field(default="en", pattern=r"^[a-zA-Z0-9+_-]{1,35}$")
    wake_phrase: str = Field(default="Hey Bud", min_length=2, max_length=60)
    microphone_level: float = Field(default=0.025, ge=0.005, le=0.3)
    notifications: bool = False

    def options(self):
        options = {"num_ctx": self.context, "temperature": self.temperature,
                   "num_predict": self.output_tokens}
        if self.threads is not None:
            options["num_thread"] = self.threads
        if self.gpu_layers is not None:
            options["num_gpu"] = self.gpu_layers
        return options
