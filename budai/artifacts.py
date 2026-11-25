import mimetypes
import os
import re
import uuid
from pathlib import Path


MIB = 1024 * 1024
SAFE_INLINE = {"image/png", "image/jpeg", "image/webp", "video/mp4", "video/webm",
               "audio/wav", "audio/ogg", "audio/mpeg"}


class Artifacts:
    def __init__(self, store):
        self.store = store
        self.directory = store.directory / "artifacts"
        self.directory.mkdir(mode=0o700, exist_ok=True)

    def save(self, account, name, data, kind, *, mime=None, conversation=None, limit=64 * MIB):
        if len(data) > limit or not data:
            raise ValueError(f"File must contain between 1 and {limit} bytes")
        name = Path(name).name.replace("\\", "_")
        name = re.sub(r"[\x00-\x1f\x7f]", "_", name)[:180] or "file"
        identifier = uuid.uuid4().hex
        path = self.directory / identifier
        try:
            with path.open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(data)
            mime = (mime or mimetypes.guess_type(name)[0] or "application/octet-stream").split(";")[0]
            self.store.run(
                "INSERT INTO artifacts(id,user_id,name,mime,kind,bytes,conversation) VALUES (?,?,?,?,?,?,?)",
                (identifier, account, name, mime, kind, len(data), conversation),
            )
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return self.entry(account, identifier)

    def entry(self, account, identifier):
        row = self.store.one(
            "SELECT id,name,mime,kind,bytes,created FROM artifacts WHERE id=? AND user_id=?",
            (identifier, account),
        )
        if not row:
            raise FileNotFoundError("File is unavailable or does not belong to this account")
        return row

    def path(self, account, identifier):
        self.entry(account, identifier)
        return self.directory / identifier

    def remove(self, account, identifier):
        path = self.path(account, identifier)
        path.unlink(missing_ok=True)
        self.store.run("DELETE FROM artifacts WHERE id=? AND user_id=?", (identifier, account))
