import {
  replace,   api, ask, button, checkbox, confirmAction, element, field, guarded, input, muted, panel, poll, pre,
  renderArtifact, response, row, select, stack, submitForm, uploadFiles, updateBlob,
} from "./common.js";

function safeLink(url, name) {
  try {
    const parsed = new URL(url);
    if (["http:", "https:"].includes(parsed.protocol)) {
      return element("a", {href: parsed.href, target: "_blank", rel: "noopener noreferrer"}, name || parsed.href);
    }
  } catch (_) {
    return muted(name || url);
  }
  return muted(name || url);
}

export async function browserPage(state, signal) {
  const address = input("url", {type: "url", required: "", class: "grow", placeholder: "https://example.com", "aria-label": "Browser address"});
  const tabs = row();
  const frame = element("img", {class: "viewer", alt: "Controlled browser viewport"});
  const result = stack();
  const status = muted("");
  const downloads = element("div", {class: "gallery"});
  let current = null;
  let interactive = false;
  let screenshotController = null;

  async function refreshDownloads() {
    const files = await api("/artifacts?kind=download");
    replace(downloads, files.map(renderArtifact));
  }

  function stopFrames() {
    screenshotController?.abort();
    screenshotController = null;
    if (frame.src.startsWith("blob:")) URL.revokeObjectURL(frame.src);
    frame.removeAttribute("src");
  }

  function startFrames() {
    if (screenshotController) return;
    screenshotController = new AbortController();
    const controller = screenshotController;
    poll(async () => {
      const snapshot = await response("/browser/frame", "GET", undefined, controller.signal);
      const data = await snapshot.blob();
      if (!controller.signal.aborted) updateBlob(frame, URL.createObjectURL(data));
    }, 1200, controller.signal);
  }

  async function refresh() {
    current = await api("/browser");
    if (signal.aborted) return;
    status.textContent = current.running ? (current.visible ? "Visible browser" : "Background browser") : "Browser closed";
    if (current.url && address !== document.activeElement) address.value = current.url === "about:blank" ? "" : current.url;
    replace(tabs, current.tabs.map(tab => row(
      button(tab.url === "about:blank" ? "New tab" : new URL(tab.url).hostname || "Tab",
        () => act("select", {tab: tab.id}), {"aria-current": tab.selected, title: tab.url}),
      button("×", () => act("close", {tab: tab.id}), {"aria-label": "Close browser tab"}))));
    if (current.running) startFrames();
    else stopFrames();
    if (!result.childElementCount && current.research?.length) {
      result.append(element("details", {open: ""}, element("summary", {}, "Research history"),
        current.research.map(item => stack(element("h3", {}, item.query), item.results.map(link => safeLink(link.url, link.title))))));
    }
  }

  async function act(operation, data = {}) {
    const output = await api("/browser", "POST", {operation, ...data});
    if (output.text !== undefined) {
      replace(result, safeLink(output.url, output.title),
        pre(output.text), element("details", {}, element("summary", {}, "Source links"),
          stack(output.links?.map(link => safeLink(link.url, link.title)))));
    }
    await refresh();
  }

  const open = element("form", {class: "row", onsubmit: guarded(async event => {
    event.preventDefault();
    await act("open", {url: address.value});
  })}, address, element("button", {}, "Open"));
  const controls = row(
    button("Fetch document", async () => {
      if (!address.value) throw new Error("Enter an HTTP(S) URL");
      const item = await api("/browser/fetch", "POST", {url: address.value});
      replace(result, safeLink(item.url, item.title), pre(item.text),
        stack(item.links?.slice(0, 20).map(link => safeLink(link))));
    }),
    button("Back", () => act("back")), button("Forward", () => act("forward")),
    button("Refresh", () => act("refresh")), button("New tab", () => act("new")),
    button("Inspect page", () => act("inspect")),
    button("Session", async () => {
      const values = await ask("Browser session", field("Mode", select("mode", [
        ["background", "Background"], ["visible", "Visible on Ubuntu"], ["close", "Close session"],
      ], current?.visible ? "visible" : "background")), {submit: "Apply"});
      if (values) await act(values.mode === "close" ? "stop" : "start", {visible: values.mode === "visible"});
    }), status);
  const actions = row(
    checkbox("Click in viewport", "interactive"),
    button("Type or fill", async () => {
      const values = await ask("Fill browser input", [
        field("Selector (optional)", input("selector")),
        field("Text", input("text", {required: ""})),
      ], {submit: "Type"});
      if (values) await act("type", {selector: values.selector || null, text: values.text});
    }),
    button("Attach to page", async () => {
      const attachment = input("file", {type: "file", required: ""});
      const values = await ask("Upload to browser page", [
        field("File input selector", input("selector", {required: ""})), field("File", attachment),
      ], {submit: "Authorize upload"});
      if (!values) return;
      const [item] = await uploadFiles(attachment.files);
      await act("upload", {selector: values.selector, artifact: item.id});
    }),
    button("Download link", async () => {
      const values = await ask("Download from page", field("Link selector", input("selector", {required: ""})),
        {submit: "Download"});
      if (values) {
        await api("/browser/download", "POST", {selector: values.selector});
        await refreshDownloads();
      }
    }),
    button("Scroll up", () => act("scroll", {y: -650})), button("Scroll down", () => act("scroll", {y: 650})),
  );
  actions.querySelector("[name=interactive]").addEventListener("change", event => interactive = event.target.checked);
  frame.addEventListener("click", guarded(async event => {
    if (!interactive || !current?.running || !frame.naturalWidth) return;
    const rect = frame.getBoundingClientRect();
    const x = Math.round((event.clientX - rect.left) * frame.naturalWidth / rect.width);
    const y = Math.round((event.clientY - rect.top) * frame.naturalHeight / rect.height);
    if (await confirmAction("Click browser page", `${current.url} · ${x}, ${y}`, "Click")) await act("click", {x, y});
  }));
  const search = submitForm(field("Web search", input("query", {required: "", maxlength: 500})), "Search", async values => {
    const data = await api("/browser/search", "POST", {query: values.query.value});
    replace(result, element("h3", {}, data.query), safeLink(data.source, "Search source"),
      stack(data.results.map(link => safeLink(link.url, link.title))));
  });
  signal.addEventListener("abort", stopFrames, {once: true});
  window.addEventListener("bud-emergency", stopFrames, {signal});
  replace(state.page, stack(panel("Browser", stack(open, controls, tabs, actions, frame)),
    gridResearch(search, result, downloads)));
  await refresh();
  await refreshDownloads();
}

function gridResearch(search, result, downloads) {
  return element("div", {class: "grid"}, panel("Research", stack(search, result)), panel("Downloads", downloads));
}
