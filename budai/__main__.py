import argparse
import asyncio
import getpass
import json
import re
import sys
from pathlib import Path

import uvicorn

from .auth import hasher
from .backup import copy_data, exclusive
from .config import Config
from .hardware import write_profile
from .ollama import Ollama
from .server import create_app
from .store import Store


def main():
    parser = argparse.ArgumentParser(prog="budai")
    commands = parser.add_subparsers(dest="command", required=True)
    add_user = commands.add_parser("add-user", help="Create a local user")
    add_user.add_argument("username")
    for mode in ("serve", "desktop"):
        launch = commands.add_parser(mode, help="Start the API" if mode == "serve" else "Open the desktop window")
        launch.add_argument("--port", type=int, default=8765)
    commands.add_parser("init", help="Initialize private application data and the database")
    commands.add_parser("hardware", help="Detect hardware and write the hardware profile")
    commands.add_parser("model", help="Check whether the exact model is installed")
    backup = commands.add_parser("backup", help="Copy and verify the current data directory")
    backup.add_argument("destination", type=Path)
    restore = commands.add_parser("restore", help="Restore a backup into a new directory")
    restore.add_argument("backup", type=Path)
    restore.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        config = Config.from_env()
        if args.command == "serve":
            if not 1 <= args.port <= 65535:
                parser.error("Use a port between 1 and 65535")
            with exclusive(config.data):
                uvicorn.run(create_app(config=config), host="127.0.0.1", port=args.port, workers=1,
                            forwarded_allow_ips="127.0.0.1,::1", access_log=False)
            return 0
        if args.command == "desktop":
            if not 1 <= args.port <= 65535:
                parser.error("Use a port between 1 and 65535")
            try:
                from .desktop_app import run
            except ImportError as error:
                raise RuntimeError("Install requirements-desktop.txt to open the native application") from error
            return run(config, args.port)
        if args.command == "backup":
            print(copy_data(config.data, args.destination))
            return 0
        if args.command == "restore":
            print(copy_data(args.backup, args.destination, restore=True))
            return 0
        if args.command == "model":
            model = asyncio.run(Ollama(config.ollama).status())
            print(json.dumps(model, indent=2))
            return 0 if model["installed"] else 2
        store = Store(config.data)
        if args.command == "init":
            print(f"BudAI data initialized at {config.data}")
        elif args.command == "hardware":
            print(json.dumps(write_profile(config.data), indent=2))
        elif args.command == "add-user":
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.username):
                parser.error("Use 1 to 64 letters, digits, underscores, dots or hyphens")
            if store.one("SELECT id FROM users WHERE name=?", (args.username,)):
                parser.error("That username already exists")
            password = getpass.getpass("Password: ")
            if not 12 <= len(password) <= 256:
                parser.error("Use a password with 12 to 256 characters")
            if password != getpass.getpass("Repeat password: "):
                parser.error("Passwords differ")
            store.run("INSERT INTO users(name,password) VALUES (?,?)", (args.username, hasher.hash(password)))
            print(f"Created the local BudAI account {args.username}")
        return 0
    except Exception as error:
        from .errors import redact

        print(redact(f"{type(error).__name__}: {error}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
