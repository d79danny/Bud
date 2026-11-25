export const byId = id => document.getElementById(id);

export function replace(node, ...children) {
  node.replaceChildren(...children.flat(Infinity).filter(value => value !== null && value !== undefined));
}

export function element(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else if (["value", "checked", "disabled", "hidden", "selected"].includes(key)) node[key] = value;
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  node.append(...children.flat(Infinity).filter(value => value !== null && value !== undefined));
  return node;
}

export function setMessage(node, text, ok = false) {
  node.textContent = text;
  node.dataset.ok = ok;
  node.hidden = !text;
}

export function notice(text, ok = false) {
  setMessage(byId("notice"), text, ok);
}

export function guarded(operation) {
  return async event => {
    try {
      await operation(event);
    } catch (error) {
      if (error.name !== "AbortError") notice(error.message);
    }
  };
}

export async function response(path, method = "GET", body, signal) {
  const headers = {"X-BudAI": "1"};
  const binary = body instanceof Blob || body instanceof ArrayBuffer;
  if (body !== undefined && !binary) headers["Content-Type"] = "application/json";
  const result = await fetch("/api" + path, {
    method, headers, body: binary ? body : JSON.stringify(body), signal, cache: "no-store",
  });
  if (!result.ok) {
    const error = await result.json().catch(() => ({}));
    if (result.status === 401 && !["/login", "/pair", "/session"].includes(path)) {
      window.dispatchEvent(new Event("bud-signout"));
    }
    throw new Error(typeof error.detail === "string" ? error.detail : `HTTP ${result.status}`);
  }
  return result;
}

export async function api(path, method, body, signal) {
  const result = await response(path, method, body, signal);
  return result.status === 204 ? undefined : result.json();
}

export const button = (label, action, attributes = {}) =>
  element("button", {type: "button", ...attributes, onclick: guarded(action)}, label);

export function field(label, input) {
  return element("label", {}, label, input);
}

export function input(name, attributes = {}) {
  return element("input", {name, ...attributes});
}

export function textArea(name, attributes = {}) {
  return element("textarea", {name, ...attributes});
}

export function select(name, options, value) {
  const node = element("select", {name}, options.map(option => {
    const [key, label] = Array.isArray(option) ? option : [option, option];
    return element("option", {value: key}, label);
  }));
  if (value !== undefined && value !== null) node.value = String(value);
  return node;
}

export function checkbox(label, name, checked = false) {
  return element("label", {class: "inline"}, input(name, {type: "checkbox", checked}), label);
}

export const row = (...children) => element("div", {class: "row"}, ...children);
export const panel = (title, ...children) => element("section", {class: "panel"}, title && element("h2", {}, title), ...children);
export const stack = (...children) => element("div", {class: "stack"}, ...children);
export const grid = (...children) => element("div", {class: "grid"}, ...children);
export const muted = (text) => element("span", {class: "muted"}, text);
export const pre = (value) => element("pre", {class: "output"}, typeof value === "string" ? value : JSON.stringify(value, null, 2));
export const entry = (...children) => element("div", {class: "entry"}, ...children);
export const asText = value => value === null || value === undefined ? "Unavailable" : String(value);
export const bytes = value => value == null ? "Unavailable" : new Intl.NumberFormat(undefined, {maximumFractionDigits: 2}).format(value / 1048576) + " MiB";
export const when = value => !value ? "" : new Date(typeof value === "number" ? value * 1000 : value.replace(" ", "T") + "Z").toLocaleString();
export const filesFrom = value => value.split("\n").map(path => path.trim()).filter(Boolean);
export const itemUrl = (id, download = false) => "/api/artifacts/" + encodeURIComponent(id) + (download ? "?download=true" : "");

export function submitForm(children, label, action) {
  const form = element("form", {class: "stack"}, children, element("button", {class: "primary"}, label));
  form.addEventListener("submit", guarded(async event => {
    event.preventDefault();
    await action(form.elements, form);
  }));
  return form;
}

export async function ask(title, fields, {submit = "Save", info = ""} = {}) {
  const dialog = byId("dialog"), form = byId("dialog-form");
  if (dialog.open) dialog.close("cancel");
  replace(form, element("h2", {}, title), info ? element("p", {class: "muted"}, info) : null,
    fields, row(element("button", {type: "submit", value: "ok", class: "primary"}, submit),
      element("button", {type: "button", onclick: () => dialog.close("cancel")}, "Cancel")));
  dialog.returnValue = "cancel";
  return new Promise(resolve => {
    dialog.addEventListener("close", () => {
      if (dialog.returnValue === "ok") resolve(Object.fromEntries(new FormData(form)));
      else resolve(null);
    }, {once: true});
    dialog.showModal();
  });
}

export const confirmAction = async (title, info, submit = title) => Boolean(await ask(title, [], {info, submit}));

export function renderArtifact(item) {
  const url = itemUrl(item.id);
  let display = null;
  if (/^image\/(png|jpeg|webp)$/.test(item.mime)) display = element("img", {src: url, alt: item.name, loading: "lazy"});
  else if (/^video\/(mp4|webm)$/.test(item.mime)) display = element("video", {src: url, controls: "", preload: "metadata"});
  else if (/^audio\/(wav|ogg|mpeg)$/.test(item.mime)) display = element("audio", {src: url, controls: "", preload: "none"});
  return stack(display, element("a", {href: itemUrl(item.id, true), download: item.name}, item.name));
}

export async function saveDownload(result, name) {
  const url = URL.createObjectURL(await result.blob());
  const link = element("a", {href: url, download: name});
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export async function uploadFiles(list) {
  const files = Array.from(list);
  const entries = [];
  for (const file of files) entries.push(await api("/uploads?name=" + encodeURIComponent(file.name), "POST", file));
  return entries;
}

export function updateBlob(node, url, cleanup) {
  if (node.src.startsWith("blob:")) URL.revokeObjectURL(node.src);
  node.src = url;
  if (cleanup && url.startsWith("blob:")) cleanup(() => URL.revokeObjectURL(url));
}

export function poll(operation, interval, signal, immediate = true) {
  let timer;
  const tick = async () => {
    if (signal.aborted) return;
    try {
      await operation();
    } catch (error) {
      if (!signal.aborted && error.name !== "AbortError") notice(error.message);
    } finally {
      if (!signal.aborted) timer = setTimeout(tick, interval);
    }
  };
  signal.addEventListener("abort", () => clearTimeout(timer), {once: true});
  if (immediate) tick();
  else timer = setTimeout(tick, interval);
}
