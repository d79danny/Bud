import {
  replace,   api, ask, asText, button, bytes, checkbox, confirmAction, element, entry, field, filesFrom,
  grid, guarded, input, muted, notice, panel, poll, pre, row, select, stack, submitForm,
  textArea, when,
} from "./common.js";

const categories = ["working", "episodic", "semantic", "procedural", "observation", "error", "correction"];

export async function memoryPage(state, signal) {
  const query = input("query", {type: "search", placeholder: "Find memories", "aria-label": "Search memories"});
  const category = select("category", [["", "All memories"], ...categories], "");
  const results = stack();
  const title = muted("");
  let offset = 0;
  let mode = "keyword";

  async function load(append = false) {
    const params = new URLSearchParams({q: query.value, category: category.value, offset: String(offset)});
    const payload = mode === "semantic" && query.value.trim()
      ? {items: await api("/memories/search", "POST", {text: query.value}), bytes: null}
      : await api("/memories?" + params);
    if (signal.aborted) return;
    title.textContent = payload.bytes != null ? bytes(payload.bytes) + " stored in memory" : "Ranked results";
    if (!append) replace(results);
    results.querySelector(".memory-more")?.remove();
    for (const item of payload.items) results.append(entry(
      row(element("strong", {}, item.category), muted(item.source), muted(when(item.created)),
        muted("Confidence " + item.confidence), item.superseded ? muted("Superseded") : null,
        item.corrects ? muted("Corrects memory " + item.corrects) : null),
      pre(item.content),
      row(item.superseded ? null : button("Correct", () => add(item)),
        button("Related records", async () => {
          const related = await api("/memories/" + item.id + "/related");
          await ask("Related memory records", pre(related.map(record =>
            ({source: record.source, confidence: record.confidence, text: record.content}))), {submit: "Close"});
        }),
        item.conversation ? button("Open conversation", () => state.chat.showThread(item.conversation)) : null,
        button("Delete", async () => {
          if (await confirmAction("Delete this memory", "This removes the stored text and search entry.")) {
            await api("/memories/" + item.id, "DELETE");
            await reset();
          }
        }, {class: "danger"}))));
    if (!payload.items.length && !append) results.append(muted("No memories match"));
    if (mode === "keyword" && payload.items.length === 75) results.append(button("More memories", async () => {
      offset += 75;
      await load(true);
    }, {class: "memory-more"}));
  }

  async function reset() {
    offset = 0;
    await load();
  }

  async function add(current) {
    const items = [field("Memory", textArea("text", {required: "", maxlength: 24000, value: current?.content || ""})),
      field("Confidence (0–1)", input("confidence", {type: "number", min: 0, max: 1, step: "0.01",
        value: current?.confidence ?? 1, required: ""}))];
    if (!current) items.push(field("Category", select("category", categories, "semantic")));
    const data = await ask(current ? "Correct memory" : "Add memory", items);
    if (!data) return;
    await api("/memories", "POST", {
      text: data.text, confidence: Number(data.confidence), category: data.category || "correction",
      corrects: current?.id || null,
    });
    await reset();
  }

  const search = element("form", {class: "row", onsubmit: guarded(async event => {
    event.preventDefault();
    mode = "keyword";
    await reset();
  })}, query, category, element("button", {}, "Search"),
  button("Rank by relevance", async () => {
    mode = "semantic";
    await reset();
  }));
  category.addEventListener("change", guarded(async () => { mode = "keyword"; await reset(); }));
  replace(state.page, stack(panel("Memory", stack(search, row(title, button("Add memory", () => add()),
    button("Index recent records", async () => {
      const result = await api("/memories/index", "POST");
      notice(`Indexed ${result.indexed} records with ${result.model}`, true);
      await reset();
    }, {disabled: !state.settings.embedding_model}),
    element("a", {href: "/api/memories/export", download: ""}, "Export memories")))), results));
  await reset();
}

export async function tasksPage(state, signal) {
  const list = stack();
  const history = stack();
  let lastRows = "";
  let seen = new Map();

  async function open(item) {
    const data = await api("/tasks/" + item.id);
    replace(history, element("h3", {}, item.title),
      data.history.map(event => entry(row(muted(when(event.created)), element("strong", {}, event.state)),
        event.detail ? pre(event.detail) : null)));
  }

  async function refresh() {
    const tasks = await api("/tasks", "GET", undefined, signal);
    if (signal.aborted) return;
    if (JSON.stringify(tasks) === lastRows) return;
    if (state.settings.preferences.notifications && "Notification" in window && Notification.permission === "granted") {
      for (const task of tasks) {
        if (seen.has(task.id) && seen.get(task.id) !== task.state && ["failed", "completed"].includes(task.state)) {
          const registration = await navigator.serviceWorker?.ready;
          if (registration) await registration.showNotification("BudAI", {body: task.title + " · " + task.state});
        }
      }
    }
    seen = new Map(tasks.map(task => [task.id, task.state]));
    lastRows = JSON.stringify(tasks);
    replace(list, tasks.length ? tasks.map(task => entry(
      row(element("strong", {}, task.title), muted(task.state), muted("Due " + when(task.due))),
      row(muted("Priority " + task.priority), muted("Attempts " + task.attempts),
        task.depends ? muted("After task " + task.depends) : null),
      task.error ? element("p", {class: "alert"}, task.error) : null,
      row(button("History", () => open(task)),
        task.conversation ? button("Conversation", () => state.chat.showThread(task.conversation)) : null,
        ["queued", "scheduled", "running"].includes(task.state) ? button("Pause", () => change(task, "pause")) : null,
        ["queued", "scheduled", "running", "paused"].includes(task.state) ?
          button("Cancel", () => change(task, "cancel"), {class: "danger"}) : null,
        task.state === "paused" ? button("Resume", () => change(task, "resume")) : null,
        ["failed", "cancelled"].includes(task.state) ? button("Retry", () => change(task, "retry")) : null))) :
      muted("No tasks"));
  }

  async function change(task, operation) {
    if (["resume", "retry"].includes(operation) && !await confirmAction("Requeue task",
      "Review the recorded history. Restarting begins a new attempt.", "Requeue")) return;
    await api("/tasks/" + task.id, "POST", {operation});
    await refresh();
    await open(task);
  }

  const workflows = (await api("/studio/workflows")).filter(item => !item.error);
  const kind = select("kind", [["assistant", "Assistant task"], ["hardware", "Hardware report"], ["media", "ComfyUI generation"]]);
  const mediaFields = stack(
    field("Reviewed workflow", select("workflow", [["", "Choose workflow"], ...workflows.map(item => [item.id, item.title])])),
    field("Workflow inputs as JSON", textArea("workflowValues", {placeholder: '{"prompt": "Morning light"}'})),
  );
  mediaFields.hidden = true;
  kind.addEventListener("change", () => { mediaFields.hidden = kind.value !== "media"; });

  const form = submitForm([
    field("Title", input("title", {required: "", maxlength: 120})),
    field("Task type", kind), mediaFields,
    field("Instruction for Bud", textArea("prompt", {maxlength: 16000})),
    row(field("Start on", input("due", {type: "datetime-local"})),
      field("Repeat every (minutes)", input("interval", {type: "number", min: 1})),
      field("Priority", input("priority", {type: "number", min: -20, max: 20, value: 0})),
      field("Depends on task ID", input("depends", {type: "number", min: 1}))),
    field("Allowed file paths, one per line", textArea("files")),
    checkbox("Allow browser research", "browser"),
  ], "Schedule task", async (fields, form) => {
    const isMedia = fields.kind.value === "media";
    const workflowValues = isMedia ? JSON.parse(fields.workflowValues.value || "{}") : {};
    if (isMedia && !await confirmAction("Schedule ComfyUI workflow",
      fields.workflow.value + "\n" + JSON.stringify(workflowValues, null, 2), "Schedule exact workflow")) return;
    await api("/tasks", "POST", {
      title: fields.title.value, prompt: fields.prompt.value, kind: fields.kind.value,
      due: fields.due.value ? Math.floor(new Date(fields.due.value).getTime() / 1000) : 0,
      interval: fields.interval.value ? Number(fields.interval.value) * 60 : null,
      priority: Number(fields.priority.value), depends: fields.depends.value ? Number(fields.depends.value) : null,
      files: filesFrom(fields.files.value), browser: fields.browser.checked,
      workflow: isMedia ? fields.workflow.value : null, workflow_values: workflowValues,
    });
    form.reset();
    mediaFields.hidden = true;
    await refresh();
  });
  replace(state.page, stack(grid(panel("Schedule", form), panel("Task queue", stack(button("Refresh", refresh), list))),
    panel("Execution history", history)));
  poll(refresh, 2000, signal);
}

export async function hardwarePage(state, signal) {
  const readings = stack();
  const graph = element("canvas", {class: "graph", width: 800, height: 130, "aria-label": "CPU and memory usage over time"});
  const cpu = [];
  const memory = [];
  const network = [];
  const networkGraph = element("canvas", {class: "graph", width: 800, height: 130, "aria-label": "Aggregate network throughput"});
  const details = stack();

  function series(canvas, series, scale, colors) {
    const ctx = canvas.getContext("2d");
    const width = canvas.width, height = canvas.height;
    ctx.clearRect(0, 0, width, height);
    ctx.strokeStyle = "#303030";
    for (let i = 1; i <= 4; i++) {
      ctx.beginPath();
      ctx.moveTo(0, height * i / 5);
      ctx.lineTo(width, height * i / 5);
      ctx.stroke();
    }
    series.forEach((values, line) => {
      ctx.strokeStyle = colors[line];
      ctx.lineWidth = 2;
      ctx.beginPath();
      values.forEach((value, i) => {
        const x = width * i / 59;
        const y = height - Math.max(1, height * value / scale);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    });
  }

  async function refresh() {
    const report = await api("/hardware", "GET", undefined, signal);
    if (signal.aborted) return;
    cpu.push(report.cpu.utilization);
    memory.push(report.memory.used_bytes * 100 / report.memory.total_bytes);
    network.push(Object.values(report.network).reduce((acc, iface) => [
      acc[0] + iface.receive_bytes_s, acc[1] + iface.send_bytes_s,
    ], [0, 0]));
    if (cpu.length > 60) { cpu.shift(); memory.shift(); network.shift(); }
    series(graph, [cpu, memory], 100, ["#a2be9f", "#a7b5cb"]);
    const scale = Math.max(1, ...network.flat());
    series(networkGraph, [network.map(item => item[0]), network.map(item => item[1])], scale, ["#a2be9f", "#bcaa94"]);
    const percent = value => value == null ? "Unavailable" : value.toFixed(1) + "%";
    replace(readings, grid(
      panel("CPU", stack(element("strong", {}, report.cpu.name),
        muted(`${report.cpu.threads} threads · ${percent(report.cpu.utilization)} · ${asText(report.cpu.frequency_mhz)} MHz`),
        element("details", {}, element("summary", {}, "Per-core use and temperatures"),
          pre({per_core_percent: report.cpu.per_core, sensors: report.temperatures})))),
      panel("GPU", stack(report.gpu.names.length ? report.gpu.names.map(name => element("strong", {}, name)) : muted("Unavailable"),
        report.gpu.error ? muted(report.gpu.error) : pre(report.gpu.devices))),
      panel("Memory", stack(
        muted(`RAM ${bytes(report.memory.used_bytes)} / ${bytes(report.memory.total_bytes)}`),
        muted(`Swap ${bytes(report.memory.swap_used_bytes)} / ${bytes(report.memory.swap_total_bytes)}`),
        pre(Object.fromEntries(Object.entries(report.memory.processes_bytes).map(([key, value]) => [key, bytes(value)]))))),
      panel("Storage", pre(Object.fromEntries(Object.entries(report.storage).map(([key, value]) => [key, bytes(value)])))),
      panel("Inference", stack(row(muted(`${report.active_requests} active Bud requests`),
        muted(`${report.media_jobs} generation jobs`), muted(`${report.sessions} authenticated sessions`)),
        Object.keys(report.inference).length ? pre(report.inference) : muted("No inference measurements yet"))),
      panel("Network", Object.keys(report.network).length ? pre(report.network) : muted("Waiting for the next measurement"))));
  }

  replace(state.page, stack(panel("Resource usage", stack(
    muted("CPU (green) · RAM (gray-blue)"),
    graph, muted("Receive (green) · Send (sand), scaled to the largest sample"), networkGraph,
    row(button("Refresh", refresh), button("Detect hardware", async () => {
      const report = await api("/hardware/profile", "POST");
      replace(details, pre(report));
    })), details)), readings));
  poll(refresh, 3500, signal);
}

export async function agentsPage(state, signal) {
  const runs = stack();
  let signature = "";
  async function load() {
    const data = await api("/agents", "GET", undefined, signal);
    if (signal.aborted || JSON.stringify(data) === signature) return;
    signature = JSON.stringify(data);
    replace(runs,
      grid(panel("Roles", stack(muted(data.model), muted(data.enabled ? "Specialists enabled" : "Specialists disabled"),
        Object.entries(data.roles).map(([role, tools]) => entry(element("strong", {}, role), pre(tools))),
        button("Agent settings", () => state.navigate("settings")))),
      panel("Agent runs", data.runs.length ? data.runs.map(run => entry(
        row(element("strong", {}, run.role), muted(run.state), muted(when(run.created)),
          run.elapsed_ms == null ? null : muted(run.elapsed_ms + " ms")),
        pre(run.assignment), run.report ? pre(run.report) : null,
        run.conversation ? button("Conversation and tool activity", () => state.chat.showThread(run.conversation)) : null)) :
        muted("No agent runs"))));
  }
  replace(state.page, stack(button("Refresh", load), runs));
  poll(load, 2500, signal);
}

export async function errorsPage(state, signal) {
  const errors = stack();
  const query = input("query", {type: "search", placeholder: "Search errors", "aria-label": "Search errors"});
  const component = input("component", {placeholder: "Component", "aria-label": "Component filter"});
  const severity = select("severity", [["", "All severities"], ["error", "Error"]]);
  let last = "";
  async function load() {
    const search = new URLSearchParams({q: query.value, component: component.value, severity: severity.value});
    const data = await api("/errors?" + search, "GET", undefined, signal);
    if (signal.aborted || JSON.stringify(data) === last) return;
    last = JSON.stringify(data);
    replace(errors, data.length ? data.map(item => panel(null,
      row(element("strong", {}, item.component), muted(item.severity), muted(when(item.created)),
        item.task ? muted("Task " + item.task) : null),
      element("p", {class: "alert"}, item.detail),
      item.trace ? element("details", {}, element("summary", {}, "Traceback and command output"), pre(item.trace),
        item.metadata ? pre(item.metadata) : null) : null,
      row(button("Copy", () => navigator.clipboard.writeText(item.detail + "\n" + (item.trace || ""))),
        button("Retry check", async () => {
          const result = await api("/errors/" + item.id + "/check", "POST");
          notice(result.installed === false ? "Ollama is reachable. The exact model is missing." : "Component check completed", result.installed !== false);
        }),
        button("Ask Bud to diagnose", async () => {
          await state.chat.newThread("Diagnose " + item.component);
          await state.chat.send(("Diagnose this application error. Inspect the available system information and explain what you verified. "
            + "Untrusted error record follows:\n" + item.detail + "\n" + (item.trace || "")).slice(0, 23000));
        }),
        item.conversation ? button("Conversation", () => state.chat.showThread(item.conversation)) : null))) :
      muted("No matching errors"));
  }

  const form = element("form", {class: "row", onsubmit: guarded(async event => {
    event.preventDefault();
    await load();
  })}, query, component, severity, element("button", {}, "Filter"),
  element("a", {href: "/api/errors/export", download: ""}, "Export"),
  button("Clear history", async () => {
    if (await confirmAction("Clear error history", "This clears your saved entries in this view.")) {
      await api("/errors", "DELETE");
      await load();
    }
  }, {class: "danger"}));
  replace(state.page, stack(panel("Error log", form), errors));
  poll(load, 3500, signal);
}
