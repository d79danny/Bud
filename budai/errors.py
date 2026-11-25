import json
import logging
import re
import traceback
from logging.handlers import RotatingFileHandler


SECRET = re.compile(
    r"(?i)(\b(?:password|passwd|token|secret|api[_-]?key|authorization|cookie)\b[\"']?\s*[=:]\s*[\"']?)"
    r"([^\s\"'&,;]+(?:[ \t]+[^\s\"'&,;]+){0,1})"
)
KNOWN_TOKEN = re.compile(r"(?i)\b(?:Bearer\s+[^\s,;]+|(?:hf_|gh[pousr]_|sk-)[A-Za-z0-9_-]{12,})")
PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)")
URL_AUTH = re.compile(r"(https?://)[^\s/@]+:[^\s/@]+@")


def redact(value):
    if isinstance(value, str):
        value = PRIVATE_KEY.sub("[redacted private key]", value)
        value = URL_AUTH.sub(r"\1[redacted]@", value)
        return KNOWN_TOKEN.sub("[redacted]", SECRET.sub(r"\1[redacted]", value))
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class ErrorLog:
    def __init__(self, store):
        self.store = store
        directory = store.directory / "logs"
        directory.mkdir(mode=0o700, exist_ok=True)
        self.path = directory / "budai.log"
        self.logger = logging.Logger(f"budai.{id(self)}")
        handler = RotatingFileHandler(self.path, maxBytes=2_000_000, backupCount=3, encoding="utf-8",
                                      delay=True)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(handler)

    def close(self):
        for handler in self.logger.handlers:
            handler.close()

    def capture(self, account, component, error, *, conversation=None, task=None, metadata=None):
        detail = redact(f"{type(error).__name__}: {error}")[:6000]
        trace = redact("".join(traceback.format_exception(error)))[:18000]
        encoded = json.dumps(redact(metadata), ensure_ascii=False)[:16000] if metadata else None
        self.logger.error("%s %s", component, detail.replace("\n", "\\n"))
        if account is not None:
            self.store.run(
                "INSERT INTO errors(user_id,detail,component,trace,conversation,task,metadata) "
                "VALUES (?,?,?,?,?,?,?)",
                (account, detail, component, trace, conversation, task, encoded),
            )
        return detail
