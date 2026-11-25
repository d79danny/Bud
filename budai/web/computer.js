import {
  replace,   api, ask, asText, button, bytes, checkbox, confirmAction, element, entry, field, grid, guarded, input,
  muted, notice, panel, poll, pre, renderArtifact, response, row, saveDownload, select, stack,
  submitForm, textArea, updateBlob, when,
} from "./common.js";

export async function computerPage(state, signal) {
  const desktop = await api("/desktop");
  const address = input("address", {class: "grow", value: state.directory || state.settings.home});
  const listing = element("div", {class: "file-list", "aria-label": "Directory entries"});
  const paths = row();
  const preview = stack();
  const usage = muted("");
  let picked = null;

  async function showFile(item) {
    picked = item;
    const result = await api("/files/preview", "POST", {path: item.path});
    replace(preview, element("h3", {}, item.name),
      row(muted(bytes(item.bytes)), muted(item.permissions), muted(when(item.modified))),
      result.artifact ? renderArtifact(result.artifact) : pre(result.text),
      row(button("Download", async () => {
        await saveDownload(await response("/files/download", "POST", {path: item.path}), item.name);
      }), result.artifact ? null : button("Edit as text", () => writeFile(item.path, result.text, true)),
      button("File operations", () => modify(item.path))));
  }

  async function list(path = address.value, offset = 0) {
    const result = await api("/files/list", "POST", {path, offset});
    if (signal.aborted) return;
    address.value = state.directory = result.directory;
    const sections = result.directory.split("/").filter(Boolean);
    replace(paths, button("/", () => list("/")), sections.map((name, index) =>
      button(name, () => list("/" + sections.slice(0, index + 1).join("/")))));
    usage.textContent = bytes(result.disk_free_bytes) + " free";
    const nodes = result.items.map(item => {
      const name = item.kind === "directory" ? item.name + "/" : item.name;
      return element("div", {class: "row"},
        button(name, () => item.kind === "directory" ? list(item.path) : showFile(item),
          {class: "grow", title: item.path}),
        button("⋯", () => modify(item.path), {"aria-label": "Manage " + item.name}));
    });
    if (!offset) replace(listing, ...nodes);
    else {
      listing.querySelector(".more-files")?.remove();
      listing.append(...nodes);
    }
    if (result.next != null) listing.append(button("Load more", () => list(result.directory, result.next), {class: "more-files"}));
    if (!result.items.length && !offset) listing.append(muted("This directory is empty"));
  }

  async function writeFile(path, content = "", replace = false) {
    const fields = await ask(replace ? "Edit text file" : "New text file", [
      field("Path", input("path", {value: path, required: ""})),
      field("Content", textArea("content", {value: content, maxlength: 128000})),
    ], {submit: replace ? "Save with backup" : "Create file"});
    if (!fields) return;
    await api("/files/write", "POST", {...fields, replace});
    await list();
    notice("File saved and verified", true);
  }

  async function modify(path = picked?.path || address.value) {
    const values = await ask("File operation", [
      field("Source path", input("path", {value: path, required: ""})),
      field("Operation", select("operation", [
        ["mkdir", "Create directory"], ["copy", "Copy"], ["move", "Move or rename"], ["trash", "Move to Trash"],
        ["delete", "Delete permanently"], ["archive", "Create ZIP"], ["extract", "Extract ZIP"],
      ])),
      field("Destination", input("destination", {placeholder: "For copy, move, ZIP or extract"})),
    ], {submit: "Review operation"});
    if (!values) return;
    if (!await confirmAction("Confirm file operation",
      `${values.operation} · ${values.path}${values.destination ? " → " + values.destination : ""}`, "Apply")) return;
    const result = await api("/files/change", "POST", {...values, destination: values.destination || null});
    await list();
    replace(preview, pre(result));
  }

  async function fileSearch() {
    const values = await ask("Search directory", [
      field("Directory", input("path", {value: address.value, required: ""})),
      field("Filename contains", input("query")),
      checkbox("Find duplicates by content", "duplicates"),
    ], {submit: "Search"});
    if (!values) return;
    const data = await api("/files/search", "POST", {...values, duplicates: Boolean(values.duplicates)});
    replace(preview, element("h3", {}, "Search results"), pre(data));
  }

  const navigation = element("form", {class: "row", onsubmit: guarded(async event => {
    event.preventDefault();
    await list();
  })}, address, element("button", {}, "Go"), button("Home", () => list(state.settings.home)));

  const frame = element("img", {class: "viewer", alt: "Current desktop"});
  const observation = element("span", {role: "status"});
  const screenLog = stack();
  let live = null;
  const liveButton = button("Start live view", () => {
    if (live) return stopLive();
    live = new AbortController();
    const current = live;
    observation.textContent = "Screen observation active";
    observation.className = "alert";
    liveButton.textContent = "Stop live view";
    poll(async () => {
      const result = await response("/desktop/frame", "POST", undefined, current.signal);
      updateBlob(frame, URL.createObjectURL(await result.blob()));
    }, 850, current.signal);
  }, {disabled: !desktop.screenshots || desktop.interactive_capture});

  function stopLive() {
    if (live) live.abort();
    live = null;
    observation.textContent = "";
    liveButton.textContent = "Start live view";
    if (frame.src.startsWith("blob:")) URL.revokeObjectURL(frame.src);
    frame.removeAttribute("src");
  }
  signal.addEventListener("abort", stopLive, {once: true});
  const stopEvent = () => stopLive();
  window.addEventListener("bud-emergency", stopEvent, {signal});

  const screenshot = async () => {
    let choices = [["screen", "Entire screen"]];
    if (desktop.session === "x11") choices = choices.concat([
      ["window", "Active window"], ["monitor", "Monitor"], ["region", "Region"],
    ]);
    const fields = await ask("Capture screen", [
      field("Scope", select("scope", choices)),
      field("Monitor name", input("monitor")),
      field("Region x,y,width,height", input("region", {placeholder: "0,0,640,480"})),
      checkbox("Read text with OCR", "ocr", desktop.ocr),
    ], {submit: "Capture"});
    if (!fields) return;
    const values = fields.region.split(",").map(Number);
    const region = fields.scope === "region" ? Object.fromEntries(["x", "y", "width", "height"].map((key, index) =>
      [key, values[index]])) : null;
    const result = await api("/desktop/capture", "POST", {
      scope: fields.scope, ocr: Boolean(fields.ocr), monitor: fields.monitor || null, region,
    });
    replace(screenLog, renderArtifact(result.artifact), result.ocr ? pre(result.ocr) : null,
      result.ocr_error ? element("p", {class: "alert"}, result.ocr_error) : null);
  };

  async function sendInput() {
    const values = await ask("Desktop input", [
      field("Action", select("operation", ["move", "click", "right", "middle", "double", "drag", "scroll_up", "scroll_down", "keys", "type"])),
      field("Coordinates x,y", input("xy")),
      field("Drag destination x,y", input("to")),
      field("Text or keys", input("value")),
    ], {submit: "Send input"});
    if (!values) return;
    const coordinate = value => value ? value.split(",").map(Number) : [null, null];
    const [x, y] = coordinate(values.xy);
    const [to_x, to_y] = coordinate(values.to);
    replace(screenLog, pre(await api("/desktop/input", "POST", {
      operation: values.operation, x, y, to_x, to_y, value: values.value || null,
    })));
  }

  async function manageWindow() {
    const values = await ask("Window control", [
      field("Action", select("operation", ["switch", "open", "close", "maximize", "minimize", "resize", "workspace"])),
      field("Window ID", input("identifier")),
      field("Application ID", input("application")),
      field("Workspace number", input("workspace", {type: "number", min: 0})),
      field("Window x,y,width,height", input("geometry")),
    ], {submit: "Apply"});
    if (!values) return;
    const dimensions = values.geometry.split(",").map(Number);
    const geometry = values.geometry ? Object.fromEntries(["x", "y", "width", "height"].map((key, index) =>
      [key, dimensions[index]])) : null;
    replace(screenLog, pre(await api("/desktop/window", "POST", {
      operation: values.operation, identifier: values.identifier || null, application: values.application || null,
      geometry, workspace: values.workspace ? Number(values.workspace) : null,
    })));
  }

  let terminal = null;
  const terminalLog = pre("");
  const terminalStatus = muted("No terminal session");
  const statusTerminal = async () => {
    const result = await api("/terminal", "GET", undefined, signal);
    terminal = result;
    terminalStatus.textContent = result ? `PID ${result.pid} · ${result.cwd} · ${result.exit_code === null ? "Running" : "Exit " + result.exit_code}` : "No terminal session";
    terminalLog.textContent = result?.output || "";
  };
  const terminalPanel = panel("Terminal", stack(
    submitForm([field("Working directory", input("cwd", {value: state.settings.home})),
      field("Command", textArea("command", {required: "", placeholder: "printf 'BudAI terminal\\n'"}))],
    "Run command", async (values, form) => {
      if (!await confirmAction("Run shell command", values.command.value + "\nDirectory: " + values.cwd.value, "Run")) return;
      await api("/terminal", "POST", {command: values.command.value, cwd: values.cwd.value});
      form.elements.command.value = "";
      await statusTerminal();
    }),
    terminalStatus,
    terminalLog,
    submitForm(field("Process input", input("text", {required: "", autocomplete: "off"})), "Send input",
      async (values, form) => {
        if (!terminal || terminal.exit_code !== null) throw new Error("No active terminal process");
        await api("/terminal/input", "POST", {text: values.text.value + "\n"});
        form.reset();
      }),
    button("Stop terminal", async () => {
      await api("/terminal", "DELETE");
      await statusTerminal();
    }),
  ));
  const result = stack(
    grid(
      panel("Files", stack(navigation, paths,
        row(button("Search", fileSearch), button("New text file", () => writeFile(address.value + "/notes.txt")),
          button("New or manage files", () => modify()), usage),
        listing, preview)),
      panel("Desktop", stack(row(muted(desktop.session), observation),
        row(button("Capture", screenshot, {disabled: !desktop.screenshots}), liveButton,
          button("Windows", async () => replace(screenLog, pre(await api("/desktop/windows"))), {disabled: !desktop.windows}),
          button("Manage windows", manageWindow, {disabled: !desktop.windows}),
          button("Input", sendInput, {disabled: !desktop.input}),
          button("Displays", async () => replace(screenLog, pre(await api("/desktop/monitors"))), {disabled: desktop.session !== "x11"}),
          button("Processes", async () => replace(screenLog, pre(await api("/desktop/processes")))),
          button("Accessibility", async () => replace(screenLog, pre(await api("/desktop/accessibility"))),
            {disabled: desktop.session === "headless"}),
          button("Clipboard", async () => {
            const current = await api("/desktop/clipboard").catch(() => ({text: ""}));
            const values = await ask("Host clipboard", field("Text", textArea("text", {value: current.text, maxlength: 24000})), {submit: "Replace clipboard"});
            if (values?.text) {
              const result = await api("/desktop/clipboard", "POST", values);
              replace(screenLog, pre(result));
            }
          }, {disabled: desktop.session === "headless"})),
        frame, screenLog,
        desktop.session === "headless" ? muted("Desktop capture and input need an active Ubuntu desktop session.") :
          desktop.session === "wayland" ? muted("The Wayland screenshot portal will request consent. Input controls require X11.") : null)),
    ),
    terminalPanel,
  );
  replace(state.page, result);
  await list();
  if (!signal.aborted) poll(statusTerminal, 1500, signal);
}
