import asyncio
import os
import pty
import shlex
import signal
import sys
import termios
import time
from dataclasses import dataclass, field
from pathlib import Path

from .errors import redact


LIMIT = 64 * 1024


def stop_group(process, sig=signal.SIGTERM):
    # The process group can still own children after the shell exits.
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return


async def terminate(process):
    stop_group(process)
    try:
        await asyncio.wait_for(process.wait(), 0.8)
    except TimeoutError:
        stop_group(process, signal.SIGKILL)
        await process.wait()
    finally:
        stop_group(process, signal.SIGKILL)


async def collect(pipe):
    content = bytearray()
    truncated = False
    while chunk := await pipe.read(16384):
        remaining = LIMIT - len(content)
        content.extend(chunk[:remaining])
        truncated |= len(chunk) > remaining
    return content.decode(errors="replace"), truncated


async def command(argv, *, cwd=None, timeout=60, env=None, check=False, redact_output=True):
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    stdout, stderr = asyncio.create_task(collect(process.stdout)), asyncio.create_task(collect(process.stderr))
    waiter = asyncio.gather(process.wait(), stdout, stderr)
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.shield(waiter), timeout)
    except TimeoutError:
        timed_out = True
        await terminate(process)
        await asyncio.wait_for(asyncio.shield(waiter), 2)
    except asyncio.CancelledError:
        await terminate(process)
        try:
            await asyncio.wait_for(asyncio.shield(waiter), 2)
        except TimeoutError:
            waiter.cancel()
        raise
    output, errors = stdout.result()[0], stderr.result()[0]
    result = {
        "pid": process.pid, "exit_code": process.returncode, "cwd": str(cwd or Path.cwd()),
        "duration_ms": round((time.monotonic() - started) * 1000), "timed_out": timed_out,
        "stdout": redact(output) if redact_output else output, "stderr": redact(errors),
        "truncated": stdout.result()[1] or stderr.result()[1],
    }
    if check and (timed_out or process.returncode):
        raise CommandError(shlex.join(argv), result)
    return result


class CommandError(RuntimeError):
    def __init__(self, command_line, result):
        self.result = result
        self.command = command_line
        status = "timed out" if result["timed_out"] else f"exited {result['exit_code']}"
        super().__init__(f"{command_line} {status}: {result['stderr'] or result['stdout']}")


@dataclass
class Terminal:
    process: asyncio.subprocess.Process
    master: int
    cwd: str
    created: float = field(default_factory=time.monotonic)
    output: bytearray = field(default_factory=bytearray)
    closed: bool = False
    timed_out: bool = False
    timer: asyncio.Task | None = None

    def read(self, event_loop):
        try:
            chunk = os.read(self.master, 8192)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            self.close_fd(event_loop)
            return
        self.output.extend(chunk)
        del self.output[:-LIMIT]

    def close_fd(self, event_loop):
        if not self.closed:
            self.closed = True
            event_loop.remove_reader(self.master)
            os.close(self.master)

    async def wait(self, timeout):
        try:
            await asyncio.wait_for(self.process.wait(), timeout)
        except TimeoutError:
            self.timed_out = True
            await self.stop()

    async def stop(self):
        if self.timer and self.timer is not asyncio.current_task() and not self.timer.done():
            self.timer.cancel()
        await terminate(self.process)
        self.close_fd(asyncio.get_running_loop())

    def snapshot(self):
        return {"pid": self.process.pid, "cwd": self.cwd, "exit_code": self.process.returncode,
                "duration_ms": round((time.monotonic() - self.created) * 1000), "timed_out": self.timed_out,
                "output": redact(self.output.decode(errors="replace"))}


class Terminals:
    def __init__(self):
        self.sessions = {}
        self.lock = asyncio.Lock()

    async def start(self, account, text, cwd, timeout=60):
        async with self.lock:
            if account in self.sessions and self.sessions[account].process.returncode is None:
                raise ValueError("A terminal session is already active")
            if account in self.sessions:
                await self.sessions[account].stop()
            master, slave = pty.openpty()
            attributes = termios.tcgetattr(slave)
            attributes[3] &= ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, attributes)
            os.set_blocking(master, False)
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, str(Path(__file__).with_name("pty_session.py")), text,
                    cwd=cwd, stdin=slave, stdout=slave, stderr=slave,
                    start_new_session=True,
                )
            except BaseException:
                os.close(master)
                raise
            finally:
                os.close(slave)
            session = Terminal(process, master, str(cwd))
            self.sessions[account] = session
            event_loop = asyncio.get_running_loop()
            event_loop.add_reader(master, session.read, event_loop)
            session.timer = asyncio.create_task(session.wait(timeout))
            return session.snapshot()

    def status(self, account):
        terminal = self.sessions.get(account)
        return terminal.snapshot() if terminal else None

    def write(self, account, text):
        terminal = self.sessions.get(account)
        if not terminal or terminal.closed or terminal.process.returncode is not None:
            raise ValueError("No active terminal")
        return {"accepted_bytes": os.write(terminal.master, text.encode())}

    async def stop(self, account):
        async with self.lock:
            if account in self.sessions:
                await self.sessions[account].stop()

    async def close(self):
        await asyncio.gather(*(self.stop(account) for account in self.sessions))
