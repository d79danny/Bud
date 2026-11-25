import asyncio
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import hardware
from .process import command


async def portal_screenshot():
    from dbus_next import BusType, Message, MessageType, Variant
    from dbus_next.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    destination = "org.freedesktop.portal.Desktop"
    token = "bud" + secrets.token_hex(12)
    path = "/org/freedesktop/portal/desktop/request/" + bus.unique_name[1:].replace(".", "_") + "/" + token
    result = asyncio.get_running_loop().create_future()

    def on_message(message):
        if (message.message_type == MessageType.SIGNAL and message.path == path
                and message.interface == "org.freedesktop.portal.Request" and message.member == "Response"
                and not result.done()):
            result.set_result(message.body)
        return False

    bus.add_message_handler(on_message)
    try:
        match = f"type='signal',sender='{destination}',path='{path}',interface='org.freedesktop.portal.Request'"
        await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                               interface="org.freedesktop.DBus", member="AddMatch", signature="s", body=[match]))
        reply = await bus.call(Message(
            destination=destination, path="/org/freedesktop/portal/desktop",
            interface="org.freedesktop.portal.Screenshot", member="Screenshot", signature="sa{sv}",
            body=["", {"handle_token": Variant("s", token), "interactive": Variant("b", True)}],
        ))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{reply.error_name}: {reply.body}")
        if reply.body[0] != path:
            raise RuntimeError("The screenshot portal did not accept the requested handle")
        status, values = await asyncio.wait_for(result, 90)
        if status != 0:
            raise PermissionError(f"The screenshot portal returned status {status}")
        uri = urlsplit(values["uri"].value)
        if uri.scheme != "file" or uri.netloc not in {"", "localhost"}:
            raise ValueError("The screenshot portal returned a non-local URI")
        return Path(unquote(uri.path)).read_bytes()
    finally:
        bus.remove_message_handler(on_message)
        bus.disconnect()


class Desktop:
    def __init__(self, artifacts):
        self.artifacts = artifacts
        self.lock = asyncio.Lock()
        self.previous = {}
        self.clipboards = {}

    def status(self):
        session = hardware.session_type()
        x11 = session == "x11"
        return {"session": session, "display": os.environ.get("DISPLAY"),
                "screenshots": (x11 and bool(shutil.which("import"))) or session == "wayland",
                "interactive_capture": session == "wayland",
                "input": x11 and bool(shutil.which("xdotool")),
                "windows": x11 and bool(shutil.which("wmctrl")),
                "ocr": bool(shutil.which("tesseract"))}

    async def monitors(self):
        self.require_x11("monitor enumeration")
        result = await command(["xrandr", "--listmonitors"], check=True, timeout=4)
        outputs = []
        for row in result["stdout"].splitlines()[1:]:
            match = re.search(r"(\d+)/\d+x(\d+)/\d+([+-]\d+)([+-]\d+)\s+(.*)", row)
            if match:
                width, height, x, y, name = match.groups()
                outputs.append({"name": name.strip(), "x": int(x), "y": int(y),
                                "width": int(width), "height": int(height)})
        return outputs

    def require_x11(self, operation):
        session = hardware.session_type()
        if session != "x11":
            raise RuntimeError(f"{operation} requires an X11 display; this session is {session}")

    async def capture(self, account, *, scope="screen", region=None, monitor=None, ocr=False, persist=True):
        async with self.lock:
            session = hardware.session_type()
            with tempfile.TemporaryDirectory() as directory:
                file = Path(directory) / "capture.jpg"
                if session == "wayland":
                    if scope != "screen":
                        raise ValueError("The Wayland portal provides its own interactive capture selection")
                    data = await portal_screenshot()
                    name, mime = "desktop.png", "image/png"
                    file.write_bytes(data)
                else:
                    self.require_x11("Screen capture")
                    window = "root"
                    if scope == "window":
                        result = await command(["xdotool", "getactivewindow"], timeout=5, check=True)
                        window = result["stdout"].strip()
                    if scope == "monitor":
                        selections = [output for output in await self.monitors() if output["name"] == monitor]
                        if not selections:
                            raise ValueError(f"Display {monitor} is unavailable")
                        region = selections[0]
                    argv = ["import", "-window", window]
                    if region:
                        geometry = "{width}x{height}{x:+d}{y:+d}".format(**region)
                        argv.extend(["-crop", geometry])
                    argv.extend(["-quality", "82", str(file)])
                    await command(argv, timeout=12, check=True)
                    data, name, mime = file.read_bytes(), "desktop.jpg", "image/jpeg"
                text = None
                ocr_error = None
                if ocr:
                    try:
                        result = await command(["tesseract", str(file), "stdout"], timeout=25, check=True)
                        text = result["stdout"][:10000]
                    except (OSError, RuntimeError) as error:
                        ocr_error = str(error)
            import hashlib

            digest = hashlib.sha256(data).hexdigest()
            previous = self.previous.get(account)
            self.previous[account] = digest
            observed = {"changed": digest != previous if previous else None, "sha256": digest,
                        "ocr": text, "ocr_error": ocr_error, "bytes": len(data)}
            if persist:
                observed["artifact"] = self.artifacts.save(account, name, data, "screenshot", mime=mime)
            return observed, data, mime

    async def windows(self):
        self.require_x11("Window enumeration")
        result = await command(["wmctrl", "-lGx"], timeout=5, check=True)
        windows = []
        for line in result["stdout"].splitlines():
            fields = line.split(None, 8)
            if len(fields) >= 8:
                identifier, workspace, x, y, width, height, application, host, *title = fields
                windows.append({"id": identifier, "workspace": workspace, "x": x, "y": y, "width": width,
                                "height": height, "application": application, "title": title[0] if title else ""})
        current = await command(["xdotool", "getactivewindow"], timeout=4)
        active = current["stdout"].strip()
        return {"active": hex(int(active)) if active.isdecimal() else None, "windows": windows}

    async def input(self, operation, *, x=None, y=None, to_x=None, to_y=None, value=None, count=1):
        self.require_x11("Mouse and keyboard control")
        argv = ["xdotool"]
        if operation in {"move", "click", "right", "middle", "double", "drag", "scroll_up", "scroll_down"}:
            if x is None or y is None:
                raise ValueError("This mouse operation requires coordinates")
            argv.extend(["mousemove", "--sync", str(x), str(y)])
        match operation:
            case "click" | "right" | "middle" | "double" | "scroll_up" | "scroll_down":
                button = {"click": 1, "right": 3, "middle": 2, "double": 1, "scroll_up": 4, "scroll_down": 5}
                argv.extend(["click", "--repeat", str(2 if operation == "double" else count),
                             str(button[operation])])
            case "drag":
                if to_x is None or to_y is None:
                    raise ValueError("Drag requires destination coordinates")
                argv.extend(["mousedown", "1", "mousemove", str(to_x), str(to_y), "mouseup", "1"])
            case "keys":
                if not value or not re.fullmatch(r"[A-Za-z0-9_+ -]{1,100}", value) or value.startswith("-"):
                    raise ValueError("Use xdotool key names such as ctrl+a")
                argv.extend(["key", "--clearmodifiers", *value.split()])
            case "type":
                if value is None:
                    raise ValueError("Text is required")
                argv.extend(["type", "--clearmodifiers", "--", value])
            case _ if operation != "move":
                raise ValueError("Unknown input operation")
        async with self.lock:
            result = await command(argv, timeout=15, check=True)
        pointer = await command(["xdotool", "getmouselocation", "--shell"], timeout=5, check=True)
        return {"input": result, "pointer": pointer["stdout"], "window_state": await self.windows()}

    async def window(self, operation, identifier=None, geometry=None, workspace=None, application=None):
        self.require_x11("Window control")
        if operation == "open":
            if not application or not re.fullmatch(r"[\w.+-]+(?:\.desktop)?", application):
                raise ValueError("Use the desktop application ID, for example org.gnome.Nautilus")
            await command(["gtk-launch", application], timeout=10, check=True)
        elif operation == "workspace":
            if workspace is None:
                raise ValueError("Select a workspace")
            await command(["wmctrl", "-s", str(workspace)], timeout=5, check=True)
        else:
            if not identifier or not re.fullmatch(r"0x[0-9a-fA-F]+", identifier):
                raise ValueError("Use a window ID from the Windows list")
            operations = {
                "close": ["wmctrl", "-ic", identifier], "switch": ["wmctrl", "-ia", identifier],
                "maximize": ["wmctrl", "-ir", identifier, "-b", "add,maximized_vert,maximized_horz"],
                "minimize": ["xdotool", "windowminimize", identifier],
            }
            if operation == "resize":
                if geometry is None:
                    raise ValueError("Resize requires x, y, width and height")
                value = "0,{x},{y},{width},{height}".format(**geometry)
                await command(["wmctrl", "-ir", identifier, "-e", value], timeout=5, check=True)
            elif operation in operations:
                await command(operations[operation], timeout=5, check=True)
            else:
                raise ValueError("Unknown window operation")
        return await self.windows()

    async def accessibility(self):
        from dbus_next import BusType, Message, MessageType
        from dbus_next.aio import MessageBus

        session = await MessageBus(bus_type=BusType.SESSION).connect()
        try:
            response = await asyncio.wait_for(session.call(Message(
                destination="org.a11y.Bus", path="/org/a11y/bus",
                interface="org.a11y.Bus", member="GetAddress",
            )), 5)
            if response.message_type == MessageType.ERROR:
                raise RuntimeError(f"{response.error_name}: {response.body}")
            bus = await MessageBus(bus_address=response.body[0]).connect()
        finally:
            session.disconnect()

        async def request(service, location, member, interface="org.a11y.atspi.Accessible",
                          signature="", arguments=None):
            reply = await asyncio.wait_for(bus.call(Message(
                destination=service, path=location, interface=interface, member=member,
                signature=signature, body=arguments or [],
            )), 4)
            if reply.message_type == MessageType.ERROR:
                raise RuntimeError(f"{reply.error_name}: {reply.body}")
            return reply.body[0]

        async def inspect(service, location, depth):
            properties = await request(service, location, "GetAll", "org.freedesktop.DBus.Properties",
                                       "s", ["org.a11y.atspi.Accessible"])
            role = await request(service, location, "GetRoleName")
            info = {"name": properties["Name"].value[:200] if "Name" in properties else None, "role": role}
            if depth and role not in {"password text", "password"}:
                children = await request(service, location, "GetChildren")
                info["children"] = [await inspect(address, child, depth - 1) for address, child in children[:4]]
            return info

        try:
            root = "/org/a11y/atspi/accessible/root"
            applications = await request("org.a11y.atspi.Registry", root, "GetChildren")
            return {"applications": [await inspect(service, location, 2) for service, location in applications[:6]]}
        finally:
            bus.disconnect()

    async def clipboard(self):
        session = hardware.session_type()
        if session == "headless":
            raise RuntimeError("Clipboard access requires an active desktop session")
        argv = ["wl-paste", "--no-newline"] if session == "wayland" else ["xclip", "-selection", "clipboard", "-o"]
        result = await command(argv, check=True, timeout=4, redact_output=False)
        return result["stdout"]

    async def set_clipboard(self, account, text):
        from .process import terminate

        session = hardware.session_type()
        if session == "headless":
            raise RuntimeError("Clipboard access requires an active desktop session")
        argv = ["wl-copy", "--foreground"] if session == "wayland" else ["xclip", "-selection", "clipboard", "-i", "-quiet"]
        await self.stop(account)
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        )
        self.clipboards[account] = process
        try:
            process.stdin.write(text.encode())
            await process.stdin.drain()
            process.stdin.close()
            await asyncio.sleep(0.15)
            if process.returncode is not None:
                raise RuntimeError(f"{argv[0]} exited with status {process.returncode}")
            verified = await self.clipboard() == text
            return {"verified": verified, "bytes": len(text.encode())}
        except BaseException:
            self.clipboards.pop(account, None)
            await terminate(process)
            raise

    async def stop(self, account):
        from .process import terminate

        process = self.clipboards.pop(account, None)
        if process and process.returncode is None:
            await terminate(process)

    async def close(self):
        await asyncio.gather(*(self.stop(account) for account in tuple(self.clipboards)))
