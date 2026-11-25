import asyncio

from fastapi import HTTPException

from .artifacts import Artifacts
from .browser import Browser
from .desktop_control import Desktop
from .errors import ErrorLog
from .hardware import Monitor
from .improvement import Improvements
from .media import Media
from .memory import Memory
from .ollama import Ollama
from .process import CommandError, Terminals
from .runner import Runner
from .store import Store
from .tasks import Scheduler
from .voice import Voice


class Services:
    def __init__(self, config, ollama=None):
        self.config = config
        self.store = Store(config.data)
        self.errors = ErrorLog(self.store)
        self.gpu = asyncio.Lock()
        self.ollama = ollama or Ollama(config.ollama)
        self.artifacts = Artifacts(self.store)
        self.monitor = Monitor(config.data)
        self.desktop = Desktop(self.artifacts)
        self.browser = Browser(config, self.artifacts)
        self.terminals = Terminals()
        self.memory = Memory(self.store, self.ollama, config.embedding_model, self.errors)
        self.voice = Voice(config, self.artifacts)
        self.media = Media(config, self.store, self.artifacts, self.ollama, self.gpu, self.errors)
        self.improvements = Improvements(self.store)
        self.runner = Runner(self)
        self.tasks = Scheduler(self)
        self.running = {}
        self.stop_epoch = {}

    async def call(self, account, component, work):
        task = asyncio.create_task(work)
        self.running.setdefault(account, set()).add(task)
        try:
            return await task
        except asyncio.CancelledError as error:
            raise HTTPException(409, "Operation stopped") from error
        except HTTPException:
            raise
        except Exception as error:
            meta = {"command": error.command, **error.result} if isinstance(error, CommandError) else None
            detail = self.errors.capture(account, component, error, metadata=meta)
            status = 404 if isinstance(error, FileNotFoundError) else 403 if isinstance(error, PermissionError) else 400
            raise HTTPException(status, detail) from error
        finally:
            self.running.get(account, set()).discard(task)

    def signout(self, account):
        asyncio.create_task(self.disconnect(account))

    async def disconnect(self, account):
        run = self.runner.active.get(account)
        if run and not run.task_id:
            await self.runner.stop(account)
        await self.terminals.stop(account)
        await self.desktop.stop(account)

    async def stop(self, account):
        self.stop_epoch[account] = self.stop_epoch.get(account, 0) + 1
        running = tuple(self.running.get(account, ()))
        for task in running:
            task.cancel()
        await asyncio.gather(*running, self.runner.stop(account), self.terminals.stop(account),
                             self.media.stop(account), self.tasks.stop(account), self.desktop.stop(account), return_exceptions=True)
        await self.browser.close_account(account)

    async def close(self):
        await self.tasks.close()
        accounts = set(self.runner.active) | set(self.running)
        if accounts:
            await asyncio.gather(*(self.stop(account) for account in accounts))
        await self.terminals.close()
        await self.desktop.close()
        for account in {owner for owner, _ in self.media.jobs.values()}:
            await self.media.stop(account)
        await self.browser.close()
        self.errors.close()
