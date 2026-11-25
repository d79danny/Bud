import {
  replace,   api, button, checkbox, element, entry, field, grid, guarded, input, muted, notice, panel, poll, pre,
  renderArtifact, row, select, stack, textArea, uploadFiles, when,
} from "./common.js";

export async function studioPage(state, signal) {
  const workflows = (await api("/studio/workflows")).filter(item => {
    if (item.error) notice(item.error);
    return !item.error;
  });
  const choice = select("workflow", [["", "Choose a workflow"],
    ...workflows.map(item => [item.id, item.title + " · " + item.kind])]);
  const info = stack();
  const inputs = stack();
  const jobs = stack();
  const gallery = element("div", {class: "gallery"});
  const connectivity = stack();
  const generateButton = element("button", {class: "primary", disabled: true}, "Queue generation");
  let current = null;
  let previousJobs = "";

  const refreshJobs = async () => {
    const rows = await api("/studio/jobs", "GET", undefined, signal);
    const identity = JSON.stringify(rows);
    if (identity === previousJobs || signal.aborted) return;
    previousJobs = identity;
    replace(jobs, rows.length ? rows.map(item => entry(
      row(element("strong", {}, workflows.find(flow => flow.id === item.workflow)?.title || item.workflow),
        muted(item.status), element("time", {}, when(item.created))),
      item.remote_id ? muted("ComfyUI job " + item.remote_id) : null,
      item.progress ? muted(item.progress.phase + (item.progress.queue_position ? " · #" + item.progress.queue_position : "")) : null,
      item.error ? element("p", {class: "alert"}, item.error) : null,
      ["running", "queued"].includes(item.status) ? button("Cancel generation", async () => {
        await api("/studio/jobs/" + item.id, "DELETE");
        await refreshJobs();
      }) : null,
    )) : muted("No generation jobs"));
    replace(gallery, rows.flatMap(item => item.outputs.map(file => panel(null, renderArtifact(file)))));
  };

  choice.addEventListener("change", () => {
    current = workflows.find(item => item.id === choice.value);
    generateButton.disabled = !current;
    replace(inputs);
    replace(info);
    if (!current) return;
    if (current.hardware) info.append(muted(current.hardware));
    for (const [name, definition] of Object.entries(current.inputs)) {
      const attrs = {name};
      const control = definition.type === "image" ? input(name, {type: "file", accept: "image/png,image/jpeg,image/webp"}) :
        definition.type === "video" ? input(name, {type: "file", accept: "video/mp4,video/webm"}) :
        definition.type === "string" ? textArea(name, {placeholder: "Use workflow default if empty"}) :
          definition.type === "boolean" ? select(name, [["", "Workflow default"], ["true", "On"], ["false", "Off"]]) :
            input(name, {...attrs, type: "number", step: definition.type === "integer" ? 1 : "any"});
      inputs.append(field(definition.label, control));
    }
  });

  const form = element("form", {class: "stack", onsubmit: guarded(async event => {
    event.preventDefault();
    if (!current) return;
    const values = {};
    for (const [name, definition] of Object.entries(current.inputs)) {
      const control = inputs.querySelectorAll("[name]");
      const entry = Array.from(control).find(item => item.name === name);
      if (definition.type === "image" || definition.type === "video") {
        if (entry.files.length) {
          const [file] = await uploadFiles(entry.files);
          values[name] = file.id;
        }
      } else if (entry.value !== "") {
        if (definition.type === "number" || definition.type === "integer") values[name] = Number(entry.value);
        else if (definition.type === "boolean") values[name] = entry.value === "true";
        else values[name] = entry.value;
      }
    }
    await api("/studio/jobs", "POST", {workflow: current.id, values});
    await refreshJobs();
    notice("Generation queued", true);
  })}, field("Image or video workflow", choice), info, inputs, row(generateButton,
    button("Verify workflow", async () => {
      if (!current) throw new Error("Choose a workflow first");
      const result = await api("/studio/workflows/" + encodeURIComponent(current.id) + "/validate", "POST");
      notice(`ComfyUI recognized ${result.nodes} workflow nodes`, true);
    })));
  replace(state.page, stack(grid(
    panel("Generate", form, workflows.length ? null :
      muted("Add a reviewed ComfyUI API workflow and its descriptor to " + state.settings.data + "/workflows.")),
    panel("Queue", stack(row(button("Check ComfyUI", async () => {
      const result = await api("/studio/status");
      replace(connectivity, pre(result));
      notice("Connected to ComfyUI", true);
    }), button("Refresh", refreshJobs)), connectivity, jobs))),
  panel("Generated media", gallery)));
  poll(refreshJobs, 2000, signal);
}
