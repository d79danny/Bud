import csv
import hashlib
import io
import mimetypes
import os
import shutil
import stat
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from pypdf import PdfReader
from send2trash import send2trash


READ_LIMIT = 4 * 1024 * 1024
ARCHIVE_LIMIT = 512 * 1024 * 1024


def local_path(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("A valid file path is required")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("Use an absolute path or a path beginning with ~/")
    return Path(os.path.abspath(path))


def properties(path):
    info = path.lstat()
    kind = "symlink" if stat.S_ISLNK(info.st_mode) else "directory" if path.is_dir() else "file"
    return {"name": path.name or "/", "path": str(path), "kind": kind, "bytes": info.st_size,
            "modified": info.st_mtime, "permissions": stat.filemode(info.st_mode),
            "owner_id": info.st_uid, "mime": mimetypes.guess_type(path.name)[0]}


def list_directory(path, offset=0, limit=200):
    path = local_path(path)
    with os.scandir(path) as siblings:
        import itertools

        selected = list(itertools.islice(siblings, offset, offset + limit + 1))
    items = []
    for item in selected[:limit]:
        try:
            items.append(properties(Path(item.path)))
        except FileNotFoundError:
            continue
    items.sort(key=lambda item: (item["kind"] != "directory", item["name"].casefold()))
    parent = None if path == path.parent else str(path.parent)
    return {"directory": str(path), "parent": parent, "items": items,
            "next": offset + limit if len(selected) > limit else None,
            "disk_free_bytes": shutil.disk_usage(path).free}


def read_bytes(path, limit=READ_LIMIT):
    if not path.is_file():
        raise ValueError(f"{path} is not a regular file")
    with path.open("rb") as input_file:
        data = input_file.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path} exceeds the {limit}-byte read limit")
    return data


def read_document(path, name=None):
    suffix = Path(name).suffix.lower() if name else path.suffix.lower()
    data = read_bytes(path, 16 * 1024 * 1024 if suffix in {".pdf", ".docx", ".xlsx"} else READ_LIMIT)
    if suffix == ".pdf":
        doc = PdfReader(io.BytesIO(data))
        return ("\n".join(page.extract_text() or "" for page in doc.pages[:30]))[:32000]
    if suffix in {".docx", ".xlsx"}:
        from xml.etree import ElementTree

        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            members = ["word/document.xml"] if suffix == ".docx" else [
                entry for entry in bundle.namelist()
                if entry == "xl/sharedStrings.xml" or entry.startswith("xl/worksheets/sheet")
            ][:20]
            text = []
            strings = []
            members.sort(key=lambda name: name != "xl/sharedStrings.xml")
            for member in members:
                if bundle.getinfo(member).file_size > READ_LIMIT:
                    raise ValueError(f"{member} exceeds the document read limit")
                tree = ElementTree.fromstring(bundle.read(member))
                tags = {"t"} if suffix == ".docx" else {"t", "v"}
                if member == "xl/sharedStrings.xml":
                    strings = ["".join(node.itertext()) for node in tree]
                    continue
                if suffix == ".xlsx":
                    lines = []
                    for row in tree.findall(".//{*}row"):
                        cells = []
                        for cell in row:
                            value = cell.find("{*}v")
                            if cell.get("t") == "s" and value is not None and value.text:
                                index = int(value.text)
                                cells.append(strings[index] if index < len(strings) else value.text)
                            else:
                                cells.append("".join(node.text or "" for node in cell.iter()
                                                     if node.tag.split("}")[-1] in tags))
                        lines.append("\t".join(cells))
                    text.extend(lines)
                else:
                    text.extend(node.text for node in tree.iter() if node.text and node.tag.split("}")[-1] in tags)
        return "\n".join(text)[:32000]
    if b"\0" in data[:4096]:
        raise ValueError("This file requires a media preview or the original application")
    text = data.decode("utf-8-sig", errors="replace")
    if suffix == ".csv":
        rows = csv.reader(io.StringIO(text))
        import itertools

        text = "\n".join("\t".join(row) for row in itertools.islice(rows, 500))
    return text[:32000]


def search(root, pattern, *, duplicates=False, limit=100):
    root = local_path(root)
    if not root.is_dir():
        raise NotADirectoryError(root)
    results, seen = [], {}
    inspected = 0
    hashed_bytes = 0
    stopped = False
    deadline = time.monotonic() + 5
    for directory, _, names in os.walk(root, followlinks=False):
        for name in names:
            inspected += 1
            if inspected > 12000 or time.monotonic() > deadline:
                stopped = True
                break
            candidate = Path(directory) / name
            if pattern.casefold() not in name.casefold() or candidate.is_symlink():
                continue
            try:
                if duplicates:
                    size = candidate.stat().st_size
                    if size > 8 * 1024 * 1024 or not candidate.is_file():
                        continue
                    hashed_bytes += size
                    if hashed_bytes > ARCHIVE_LIMIT:
                        stopped = True
                        break
                    with candidate.open("rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    seen.setdefault(digest, []).append(str(candidate))
                else:
                    results.append(properties(candidate))
                    if len(results) >= limit:
                        stopped = True
                        break
            except (FileNotFoundError, PermissionError):
                continue
        if stopped:
            break
    if duplicates:
        results = [paths for paths in seen.values() if len(paths) > 1][:limit]
    return {"results": results, "inspected": inspected, "truncated": stopped}


def write_text(path, content, backups=None):
    path = local_path(path)
    data = content.encode()
    if len(data) > READ_LIMIT:
        raise ValueError("Text exceeds the write limit")
    backup = None
    if backups and path.exists():
        if not path.is_file() or path.is_symlink():
            raise ValueError("Only regular files can be edited")
        backups.mkdir(parents=True, mode=0o700, exist_ok=True)
        backup = backups / f"{time.time_ns()}-{path.name}"
        shutil.copy2(path, backup)
        backup.chmod(0o600)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                os.fchmod(output.fileno(), stat.S_IMODE(path.stat().st_mode))
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
    else:
        with path.open("xb") as output:
            output.write(data)
    if read_bytes(path) != data:
        raise OSError(f"Saved data at {path} failed verification")
    return {"file": properties(path), "backup": str(backup) if backup else None, "verified": True}


def archive(source, target):
    total = 0
    count = 0
    if not source.exists():
        raise FileNotFoundError(source)
    if source == target or (source.is_dir() and target.is_relative_to(source)):
        raise ValueError("Archive output must be outside the source directory")
    try:
        stream = target.open("xb")
    except FileExistsError:
        raise FileExistsError(f"{target} already exists") from None
    try:
        with stream, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as bundle:
            items = source.rglob("*") if source.is_dir() else (source,)
            for item in items:
                if item.is_symlink() or not item.is_file():
                    continue
                total += item.stat().st_size
                count += 1
                if total > ARCHIVE_LIMIT or count > 12000:
                    raise ValueError("Archive exceeds the file count or expanded-size limit")
                bundle.write(item, item.relative_to(source.parent).as_posix())
        with zipfile.ZipFile(target) as bundle:
            if bundle.testzip() is not None:
                raise OSError("Archive CRC verification failed")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {"file": properties(target), "members": count, "verified": True}


def extract(source, target):
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    with zipfile.ZipFile(source) as bundle:
        entries = bundle.infolist()
        if len(entries) > 12000 or sum(item.file_size for item in entries) > ARCHIVE_LIMIT:
            raise ValueError("Archive exceeds the file count or expanded-size limit")
        with tempfile.TemporaryDirectory(dir=target.parent) as directory:
            directory = Path(directory)
            for item in entries:
                member = PurePosixPath(item.filename)
                if member.is_absolute() or ".." in member.parts or stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError(f"Unsafe archive member: {item.filename}")
                destination = directory.joinpath(*member.parts)
                if item.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(item) as input_file, destination.open("xb") as output:
                    shutil.copyfileobj(input_file, output)
            target.mkdir()
            try:
                for item in directory.iterdir():
                    item.rename(target / item.name)
            except OSError:
                shutil.rmtree(target)
                raise
    return {"directory": str(target), "members": len(entries), "verified": target.is_dir()}


def modify(action, source, destination=None):
    source, target = local_path(source), local_path(destination) if destination else None
    if action in {"copy", "move", "archive", "extract"} and target is None:
        raise ValueError("This operation requires a destination")
    if action == "mkdir":
        source.mkdir()
        return {"file": properties(source), "verified": source.is_dir()}
    if action == "archive":
        return archive(source, target)
    if action == "extract":
        return extract(source, target)
    if action == "trash":
        send2trash(str(source))
        return {"path": str(source), "verified": not os.path.lexists(source)}
    if action == "delete":
        if source.is_dir() and not source.is_symlink():
            source.rmdir()
        else:
            source.unlink()
        return {"path": str(source), "verified": not os.path.lexists(source)}
    if action in {"copy", "move"}:
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)
        if source == target or target.is_relative_to(source):
            raise ValueError("Destination must be outside the source")
        if action == "move":
            shutil.move(source, target)
        elif source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            with source.open("rb") as input_file, target.open("xb") as output:
                shutil.copyfileobj(input_file, output)
            shutil.copystat(source, target)
        verified = os.path.lexists(target) and (action == "copy" or not os.path.lexists(source))
        return {"file": properties(target), "verified": verified}
    raise ValueError("Unknown file operation")
