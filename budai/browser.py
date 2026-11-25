import asyncio
import ipaddress
import json
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote_plus, urlsplit


class Browser:
    def __init__(self, config, artifacts):
        self.config = config
        self.artifacts = artifacts
        self.playwright = None
        self.profiles = {}
        self.active = {}
        self.lock = asyncio.Lock()
        self.internal_ports = {urlsplit(url).port for url in (config.ollama, config.comfy)}
        if config.origin:
            self.internal_host = urlsplit(config.origin).hostname
        else:
            self.internal_host = None

    def executable(self):
        return self.config.chromium or shutil.which("chromium") or shutil.which("google-chrome")

    def navigate_url(self, value):
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("Use an HTTP(S) URL without embedded credentials")
        port = url.port or (443 if url.scheme == "https" else 80)
        host = url.hostname.rstrip(".").lower()
        try:
            address = ipaddress.ip_address(host)
            if address.is_link_local:
                raise PermissionError("The browser cannot access link-local service addresses")
            local = address.is_loopback
        except ValueError:
            local = host == "localhost" or host.endswith(".localhost")
        if host == self.internal_host or (local and port in self.internal_ports | {8765}):
            raise ValueError("BudAI and model service endpoints are unavailable inside the research browser")
        return value

    async def route(self, route):
        url = route.request.url
        if url.startswith(("blob:", "data:", "about:")):
            await route.continue_()
            return
        try:
            self.navigate_url(url)
        except (ValueError, TypeError, PermissionError):
            await route.abort()
            return
        await route.continue_()

    async def context(self, account, visible=False):
        async with self.lock:
            profile = self.profiles.get(account)
            if profile and profile[0] == visible:
                return profile[1]
            if profile:
                await profile[1].close()
                del self.profiles[account]
            try:
                from playwright.async_api import async_playwright
            except ImportError as error:
                raise RuntimeError("Install requirements-browser.txt to enable browser control") from error
            if self.playwright is None:
                self.playwright = await async_playwright().start()
            if self.config.chromium and not Path(self.config.chromium).is_file():
                raise FileNotFoundError(self.config.chromium)
            if visible and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                raise RuntimeError("Visible browser mode requires an active Ubuntu desktop session")
            directory = self.config.data / "browser" / str(account)
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            desktop_config = directory / "config"
            desktop_config.mkdir(mode=0o700, exist_ok=True)
            context = await self.playwright.chromium.launch_persistent_context(
                directory / "profile", executable_path=self.executable(), headless=not visible,
                env={**os.environ, "XDG_CONFIG_HOME": str(desktop_config)},
                chromium_sandbox=True, accept_downloads=True, service_workers="block",
                viewport={"width": 1280, "height": 800},
            )
            context.set_default_timeout(12000)
            await context.route("**/*", self.route)
            self.profiles[account] = (visible, context)
            self.active[account] = context.pages[0] if context.pages else await context.new_page()
            return context

    async def close_account(self, account):
        async with self.lock:
            profile = self.profiles.pop(account, None)
            self.active.pop(account, None)
            if profile:
                await profile[1].close()

    async def close(self):
        for account in list(self.profiles):
            await self.close_account(account)
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def page(self, account):
        profile = self.profiles.get(account)
        context = profile[1] if profile else await self.context(account)
        active = self.active.get(account)
        if not active or active.is_closed():
            active = context.pages[-1] if context.pages else await context.new_page()
            self.active[account] = active
        return context, active

    async def inspect(self, account, links=True):
        _, page = await self.page(account)
        body = await page.locator("body").inner_text(timeout=5000)
        result = {"url": page.url, "title": await page.title(), "text": body[:20000]}
        if links:
            result["links"] = (await page.locator("a[href]").evaluate_all(
                """links => links.filter(a => /^https?:/.test(a.href)).slice(0, 80)
                  .map(a => ({title:(a.innerText || "").trim().slice(0,160), url:a.href}))"""
            ))[:60]
        return result

    async def status(self, account, screenshot=False):
        profile = self.profiles.get(account)
        if not profile:
            return {"running": False, "tabs": [], "research": self.research_history(account)}
        context, page = await self.page(account)
        result = {"running": True, "visible": profile[0], "url": page.url,
                  "tabs": [{"id": index, "url": tab.url, "selected": tab == page}
                           for index, tab in enumerate(context.pages)],
                  "research": self.research_history(account)}
        if screenshot:
            result["view"] = self.artifacts.save(account, "browser.jpg", await self.screenshot(account), "browser")
        return result

    async def screenshot(self, account):
        _, page = await self.page(account)
        return await page.screenshot(type="jpeg", quality=75, timeout=8000)

    async def action(self, account, operation, *, url=None, tab=None, x=None, y=None, text=None, visible=None,
                     selector=None, artifact=None):
        if operation == "start":
            await self.context(account, bool(visible))
            return await self.status(account)
        if operation == "stop":
            await self.close_account(account)
            return await self.status(account)
        if operation == "inspect":
            return await self.inspect(account)
        context, page = await self.page(account)
        if operation in {"open", "new"}:
            if not url and operation == "open":
                raise ValueError("Enter a URL")
            if url:
                self.navigate_url(url)
            if operation == "new":
                page = await context.new_page()
                self.active[account] = page
            if url:
                response = await page.goto(url, wait_until="domcontentloaded")
                if response and response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status} from {page.url}")
                result = await self.inspect(account)
                if response:
                    result["http_status"] = response.status
                return result
        elif operation in {"back", "forward", "refresh"}:
            action = {"back": page.go_back, "forward": page.go_forward, "refresh": page.reload}
            await action[operation](wait_until="domcontentloaded")
        elif operation in {"select", "close"}:
            pages = context.pages
            if tab is None or not 0 <= tab < len(pages):
                raise ValueError("Select an existing tab")
            if operation == "close":
                await pages[tab].close()
            else:
                self.active[account] = pages[tab]
                await pages[tab].bring_to_front()
        elif operation == "click":
            if selector:
                await page.locator(selector).click()
            elif x is not None and y is not None:
                await page.mouse.click(x, y)
            else:
                raise ValueError("Provide browser coordinates or a selector")
        elif operation == "type":
            if text is None:
                raise ValueError("Text is required")
            if selector:
                await page.locator(selector).fill(text)
            else:
                await page.keyboard.insert_text(text)
        elif operation == "upload":
            if not selector or not artifact:
                raise ValueError("Select a file input and an uploaded attachment")
            item = self.artifacts.entry(account, artifact)
            if item["bytes"] > 16 * 1024 * 1024:
                raise ValueError("The selected upload exceeds 16 MiB")
            await page.locator(selector).set_input_files({
                "name": item["name"], "mimeType": item["mime"],
                "buffer": self.artifacts.path(account, artifact).read_bytes(),
            })
        elif operation == "scroll":
            await page.mouse.wheel(0, y or 600)
        else:
            raise ValueError("Unknown browser operation")
        return await self.status(account)

    async def search(self, account, query):
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        context, _ = await self.page(account)
        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
            if not response or response.status >= 400:
                raise RuntimeError(f"Search returned HTTP {response.status if response else 'unavailable'}")
            links = await page.locator(".result__a").evaluate_all(
                "items => items.slice(0,12).map(a => ({title:a.innerText.trim(),url:a.href}))"
            )
            if not links:
                body = (await page.locator("body").inner_text())[:700]
                raise RuntimeError(f"Search yielded no result links. Page response: {body}")
        finally:
            await page.close()
        from urllib.parse import parse_qs

        for link in links:
            parsed = urlsplit(link["url"])
            if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
                link["url"] = parse_qs(parsed.query).get("uddg", [link["url"]])[0]
        result = {"query": query, "source": url, "results": links}
        self.artifacts.store.run("INSERT INTO research(user_id,query,record) VALUES (?,?,?)",
                                 (account, query, json.dumps(result, ensure_ascii=False)))
        return result

    def research_history(self, account):
        rows = self.artifacts.store.all("SELECT record FROM research WHERE user_id=? ORDER BY id DESC LIMIT 40",
                                       (account,))
        return [json.loads(row["record"]) for row in rows]

    async def download(self, account, selector):
        _, page = await self.page(account)
        async with page.expect_download(timeout=30000) as event:
            await page.locator(selector).click()
        download = await event.value
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "download"
            await download.save_as(target)
            if target.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("Browser download exceeds 64 MiB")
            item = self.artifacts.save(account, download.suggested_filename, target.read_bytes(), "download")
        await download.delete()
        return {"source": download.url, "artifact": item}

    async def fetch(self, url):
        import httpx

        async with httpx.AsyncClient(timeout=18, follow_redirects=False) as client:
            for _ in range(6):
                self.navigate_url(url)
                async with client.stream("GET", url, headers={
                    "Accept": "text/html, text/plain, application/json",
                    "User-Agent": "BudAI/0.1 (local research assistant)",
                }) as response:
                    if response.has_redirect_location:
                        from urllib.parse import urljoin

                        url = urljoin(url, response.headers["location"])
                        continue
                    response.raise_for_status()
                    mime = response.headers.get("content-type", "").split(";")[0].lower()
                    if mime not in {"text/html", "text/plain", "application/json", "application/xhtml+xml"}:
                        raise ValueError(f"Web content type {mime or 'unspecified'} requires a browser or an authorized download")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 2 * 1024 * 1024:
                            raise ValueError("Web document exceeds the 2 MiB read limit")
                    text = data.decode(response.encoding or "utf-8", errors="replace")
                    result = {"url": str(response.url), "status": response.status_code, "mime": mime}
                    if "html" in mime:
                        result.update(extract_html(text, url))
                    else:
                        result["text"] = text[:24000]
                    return result
        raise RuntimeError("Web request exceeded the redirect limit")


def extract_html(source, url):
    from html.parser import HTMLParser
    from urllib.parse import urljoin

    class Article(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.skip = 0
            self.is_title = False
            self.title = ""
            self.text = []
            self.links = []

        def handle_starttag(self, tag, attrs):
            if tag in {"script", "style", "noscript", "template"}:
                self.skip += 1
            if tag == "title":
                self.is_title = True
            if tag == "a" and len(self.links) < 60:
                href = dict(attrs).get("href")
                if href:
                    target = urljoin(url, href)
                    if urlsplit(target).scheme in {"https", "http"}:
                        self.links.append(target)

        def handle_endtag(self, tag):
            if tag in {"script", "style", "noscript", "template"}:
                self.skip = max(0, self.skip - 1)
            if tag == "title":
                self.is_title = False

        def handle_data(self, text):
            if not self.skip and text.strip():
                if self.is_title:
                    self.title = text.strip()[:300]
                else:
                    self.text.append(" ".join(text.split()))

    document = Article()
    document.feed(source)
    return {"title": document.title, "text": "\n".join(document.text)[:24000], "links": document.links}
