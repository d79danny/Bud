import {
  replace,   api, ask, button, checkbox, confirmAction, element, entry, field, filesFrom, grid, input, muted,
  notice, panel, pre, row, select, stack, submitForm, textArea, when,
} from "./common.js";

export async function settingsPage(state, signal) {
  state.settings = await api("/settings");
  const preferences = state.settings.preferences;
  const modelOutput = stack();
  const deviceOutput = stack();
  const pairingOutput = stack();
  const audits = stack();
  const proposalOutput = stack();

  const number = (label, key, min, max, step = 1) => field(label, input(key, {
    type: "number", min, max, step, value: preferences[key] ?? "", placeholder: preferences[key] == null ? "Ollama default" : "",
  }));

  const fields = [
    panel("AI model", stack(
      element("strong", {}, state.settings.model), muted(state.settings.ollama),
      row(number("Context tokens", "context", 1024, 131072), number("Output tokens", "output_tokens", 64, 32768),
        number("Temperature", "temperature", 0, 2, 0.05),
        number("Keep alive (seconds)", "keep_alive", 0, 86400), number("Request timeout (seconds)", "timeout", 10, 1800)),
      row(number("CPU threads", "threads", 1, 512), number("GPU layers", "gpu_layers", 0, 999),
        field("Tool protocol", select("tool_protocol", ["auto", "native", "json"], preferences.tool_protocol))),
      row(button("Check model", async () => {
        const model = await api("/model");
        replace(modelOutput, pre(model));
        notice(model.installed ? "The exact model is installed" : "The exact model is missing", model.installed);
      }), button("Load model", async () => replace(modelOutput, pre(await api("/model/load", "POST")))),
      button("Unload model", async () => replace(modelOutput, pre(await api("/model/unload", "POST"))))), modelOutput)),
    panel("Memory and agents", stack(number("Retrieved records", "retrieval", 0, 20),
      muted(state.settings.embedding_model || "Embeddings use a separately configured local Ollama model."),
      checkbox("Allow Bud to delegate sequential specialist runs", "allow_agents", preferences.allow_agents),
      row(button("Manage memory", () => state.navigate("memory")), button("View agents", () => state.navigate("agents"))))),
    panel("Voice", stack(number("Speech speed (words per minute)", "speech_speed", 80, 450),
      field("eSpeak voice", input("voice", {value: preferences.voice, maxlength: 35, required: ""})),
      field("Wake phrase", input("wake_phrase", {value: preferences.wake_phrase, maxlength: 60, required: ""})),
      number("Microphone activity threshold", "microphone_level", 0.005, 0.3, 0.005))),
    panel("Applications and devices", stack(
      checkbox("Notify about task results when the Tasks tab is open", "notifications", preferences.notifications),
      checkbox("Unload the idle Ollama model before ComfyUI generation", "share_gpu", preferences.share_gpu),
      muted("ComfyUI: " + state.settings.comfy),
      muted("Cloud or LAN access: " + (state.settings.origin || "Local only")),
      muted("Data storage: " + state.settings.data),
      row(button("Computer controls", () => state.navigate("computer")),
        button("Browser sessions", () => state.navigate("browser")), button("Generation workflows", () => state.navigate("studio"))))),
  ];

  const settings = submitForm(grid(fields), "Save settings", async form => {
    const updated = {};
    for (const key of Object.keys(preferences)) {
      const item = form[key];
      if (!item) throw new Error("Unknown settings field " + key);
      if (item.type === "checkbox") updated[key] = item.checked;
      else if (item.type === "number") updated[key] = item.value === "" ? null : Number(item.value);
      else updated[key] = item.value;
    }
    if (updated.notifications && "Notification" in window && Notification.permission === "default") {
      await Notification.requestPermission();
    }
    state.settings.preferences = await api("/settings", "PUT", updated);
    notice("Settings saved", true);
  });

  async function devices() {
    const result = await api("/devices");
    if (signal.aborted) return;
    replace(deviceOutput, result.devices.map(device => entry(
      row(element("strong", {}, device.label), muted(device.id === result.current ? "Current session" : ""),
        muted(device.revoked ? "Revoked" : "Last seen " + when(device.seen))),
      device.revoked ? null : button("Revoke", async () => {
        if (await confirmAction("Revoke device", device.label)) {
          await api("/devices/" + device.id, "DELETE");
          if (device.id === result.current) window.dispatchEvent(new Event("bud-signout"));
          else await devices();
        }
      }, {class: "danger"}))));
  }

  async function changePassword() {
    const values = await ask("Change password", [
      field("Current password", input("current", {type: "password", required: "", autocomplete: "current-password"})),
      field("New password", input("new", {type: "password", required: "", minlength: 12, autocomplete: "new-password"})),
      field("Verification code (when enabled)", input("otp", {maxlength: 6, inputmode: "numeric"})),
    ]);
    if (!values) return;
    await api("/password", "POST", values);
    notice("Password changed and other sessions revoked", true);
    await devices();
  }

  async function enrollFactor() {
    const setup = await ask("Enable verification codes", field("Password",
      input("password", {type: "password", required: "", autocomplete: "current-password"})), {submit: "Start enrollment"});
    if (!setup) return;
    const {secret, uri} = await api("/second-factor/setup", "POST", setup);
    const code = await ask("Verify authenticator", [
      element("a", {href: uri}, "Open authenticator"), pre(secret),
      field("Code from authenticator", input("code", {required: "", minlength: 6, maxlength: 6, inputmode: "numeric"})),
    ], {submit: "Enable"});
    if (!code) return;
    await api("/second-factor/verify", "POST", {...setup, ...code});
    notice("Verification codes enabled", true);
    await state.navigate("settings");
  }

  async function disableFactor() {
    const values = await ask("Disable verification codes", [
      field("Password", input("password", {type: "password", required: "", autocomplete: "current-password"})),
      field("Current code", input("code", {required: "", minlength: 6, maxlength: 6, inputmode: "numeric"})),
    ], {submit: "Disable"});
    if (values) {
      await api("/second-factor", "DELETE", values);
      await state.navigate("settings");
    }
  }

  async function pair() {
    const result = await api("/pairings", "POST");
    replace(pairingOutput,
      element("img", {src: "data:image/png;base64," + result.png, alt: "Scan to pair with BudAI", width: 176, height: 176}),
      muted("Pairing link expires in " + result.expires_in + " seconds"),
      button("Copy pairing link", () => navigator.clipboard.writeText(result.url)));
  }

  async function log() {
    const records = await api("/audit");
    replace(audits, records.length ? records.map(record =>
      entry(row(muted(when(record.created)), element("strong", {}, record.action), muted(record.detail)))) :
      muted("No account activity"));
  }

  const current = await api("/session");
  const security = panel("Security", stack(row(muted(current.name), button("Change password", changePassword),
    current.second_factor ? button("Disable verification codes", disableFactor) :
      button("Enable verification codes", enrollFactor), button("Login and device history", log)), audits));
  const mobile = panel("Mobile access", stack(
    muted(state.settings.origin || "Set an HTTPS origin to enable device pairing."),
    button("Create pairing QR", pair, {disabled: !state.settings.origin}),
    pairingOutput, deviceOutput));

  async function propose() {
    const data = await ask("Propose improvement", [
      field("Name", input("name", {maxlength: 120, required: ""})),
      field("Proposal type", select("kind", [["workflow", "Reusable workflow"], ["python", "Python source for review"]])),
      field("Workflow JSON or Python source", textArea("proposal", {maxlength: 40000, required: ""})),
    ], {submit: "Record proposal",
      info: 'A workflow contains a steps array. Each step specifies tool, arguments and expected result fields under expect.'});
    if (!data) return;
    const proposal = data.kind === "python" ? {source: data.proposal} : JSON.parse(data.proposal);
    await api("/improvements", "POST", {name: data.name, kind: data.kind, proposal});
    await improvements();
  }

  async function improvements() {
    const list = await api("/improvements");
    replace(proposalOutput, list.length ? list.map(item => entry(
      row(element("strong", {}, item.name), muted("Version " + item.version), muted(item.kind), muted(item.state)),
      element("details", {}, element("summary", {}, "Proposal and test evidence"), pre(item.proposal),
        item.evidence ? pre(item.evidence) : null),
      row(button("Validate", async () => {
        const values = await ask("Validate proposal", field("File paths available during the trial", textArea("paths")),
          {submit: "Run validation"});
        if (!values) return;
        try {
          await api("/improvements/" + item.id + "/validate", "POST", {files: filesFrom(values.paths)});
          notice("Validation recorded", true);
        } finally {
          await improvements();
        }
      }), item.kind === "workflow" && ["validated", "retired"].includes(item.state) ?
        button(item.state === "retired" ? "Restore this version" : "Activate", async () => {
          if (await confirmAction("Enable " + item.name, "Enable this validated read-only workflow for Bud.")) {
            await api("/improvements/" + item.id + "/activate", "POST");
            await improvements();
          }
        }) : null))) : muted("No improvement proposals"));
  }

  replace(state.page, stack(settings, grid(security, mobile),
    panel("Self-improvement", stack(button("Propose improvement", propose), proposalOutput))));
  await Promise.all([devices(), improvements()]);
}
