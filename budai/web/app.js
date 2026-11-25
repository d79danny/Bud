import {replace, api, button, byId, element, guarded, notice, poll, setMessage} from "./common.js";
import {installChat} from "./chat.js";
import {computerPage} from "./computer.js";
import {browserPage} from "./browser.js";
import {studioPage} from "./studio.js";
import {installSpeech, voicePage} from "./voice.js";
import {agentsPage, errorsPage, hardwarePage, memoryPage, tasksPage} from "./pages.js";
import {settingsPage} from "./settings.js";

const pages = [
  ["chat", "Chat"], ["computer", "Computer", computerPage], ["memory", "Memory", memoryPage],
  ["browser", "Browser", browserPage], ["studio", "Studio", studioPage], ["voice", "Voice", voicePage],
  ["tasks", "Tasks", tasksPage], ["hardware", "Hardware", hardwarePage], ["agents", "Agents", agentsPage],
  ["errors", "Errors", errorsPage], ["settings", "Settings", settingsPage],
];

const state = {page: byId("page"), thread: null, current: "chat", settings: null};
const application = byId("application");
const signin = byId("signin");
let pageController = null;
let sessionController = null;
let pairToken = new URLSearchParams(location.hash.slice(1)).get("pair");
if (pairToken) history.replaceState(null, "", location.pathname + location.search);
let lastInput = Date.now();
let activeText = "";
let running = false;
let hostOnline = true;
let stopEpoch = null;

state.heading = text => byId("page-title").textContent = text;
state.markActive = (label = activeText) => {
  activeText = label;
  byId("active").hidden = !(activeText || running || state.recording);
  byId("active").textContent = [activeText || (running ? "Bud active" : ""), state.recording].filter(Boolean).join(" · ");
};

installChat(state);
installSpeech(state);

function toggleSidebar(value) {
  const collapsed = typeof value === "boolean" ? value : !application.classList.contains("collapsed");
  application.classList.toggle("collapsed", collapsed);
  byId("toggle-sidebar").setAttribute("aria-expanded", String(!collapsed));
  localStorage.setItem("bud-sidebar-collapsed", String(collapsed));
}

state.navigate = async function navigate(page) {
  let item = pages.find(([id]) => id === page);
  if (!item) item = pages[0];
  const [id, label, view] = item;
  pageController?.abort();
  pageController = new AbortController();
  state.current = id;
  byId("thread-controls").hidden = id !== "chat";
  byId("nav").querySelectorAll("button").forEach(node =>
    node.setAttribute("aria-current", String(node.dataset.tab === id)));
  if (matchMedia("(max-width: 760px)").matches && id !== "chat") toggleSidebar(true);
  state.heading(label);
  notice("");
  if (id === "chat") {
    await state.chat.refreshSidebar();
    await state.chat.renderPage();
  } else {
    replace(state.page, element("p", {class: "muted"}, "Loading " + label));
    await view(state, pageController.signal);
  }
};

replace(byId("nav"), pages.map(([id, label]) => button(label,
  () => state.navigate(id), {"data-tab": id})));
byId("toggle-sidebar").addEventListener("click", () => toggleSidebar());
toggleSidebar(localStorage.getItem("bud-sidebar-collapsed") === "true");
const resize = byId("sidebar-resize");
function sidebarWidth(width) {
  width = Math.max(170, Math.min(480, Math.round(width)));
  application.style.setProperty("--sidebar", width + "px");
  localStorage.setItem("bud-sidebar-width", String(width));
}
sidebarWidth(Number(localStorage.getItem("bud-sidebar-width")) || 244);
resize.addEventListener("pointerdown", event => resize.setPointerCapture(event.pointerId));
resize.addEventListener("pointermove", event => {
  if (resize.hasPointerCapture(event.pointerId)) sidebarWidth(event.clientX);
});
resize.addEventListener("keydown", event => {
  if (["ArrowLeft", "ArrowRight"].includes(event.key)) {
    event.preventDefault();
    sidebarWidth(byId("sidebar").offsetWidth + (event.key === "ArrowLeft" ? -14 : 14));
  }
});
document.addEventListener("keydown", guarded(async event => {
  if (!application.hidden && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "n") {
    event.preventDefault();
    await state.chat.newThread();
  }
}));
for (const type of ["pointerdown", "keydown", "touchstart"]) {
  document.addEventListener(type, () => { lastInput = Date.now(); }, {passive: true});
}

function showSignin(message = "") {
  pageController?.abort();
  sessionController?.abort();
  state.chat.reset();
  state.stopVoice();
  application.hidden = true;
  signin.hidden = false;
  byId("login").hidden = Boolean(pairToken);
  byId("pair").hidden = !pairToken;
  setMessage(byId(pairToken ? "pair-error" : "login-error"), message);
  replace(state.page);
  replace(byId("thread-controls"));
  state.thread = state.directory = state.settings = null;
  state.transcript = "";
  running = false;
  stopEpoch = null;
  state.markActive("");
}

async function status() {
  try {
    await api("/health");
    hostOnline = true;
    if (application.hidden) return;
    const current = await api("/active");
    running = current.running;
    if (stopEpoch !== null && current.stop_epoch !== stopEpoch) {
      window.dispatchEvent(new Event("bud-emergency"));
      state.stopVoice();
    }
    stopEpoch = current.stop_epoch;
  } catch (error) {
    hostOnline = false;
    running = false;
  } finally {
    const indicator = byId("connection");
    indicator.textContent = hostOnline ? "Host connected" : "Host unreachable";
    indicator.dataset.online = hostOnline;
    state.markActive();
  }
}

async function enter() {
  await api("/session");
  state.settings = await api("/settings");
  const threads = await api("/conversations");
  state.thread = threads[0]?.id ?? null;
  lastInput = Date.now();
  signin.hidden = true;
  application.hidden = false;
  await state.navigate("chat");
  sessionController?.abort();
  sessionController = new AbortController();
  poll(async () => {
    if (Date.now() - lastInput > 30 * 60 * 1000) {
      await api("/logout", "POST").catch(() => {});
      showSignin("Session locked after inactivity");
      return;
    }
    await status();
  }, 6000, sessionController.signal);
}

byId("login").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form));
  payload.device = matchMedia("(pointer: coarse)").matches ? "Mobile browser" : "Desktop browser";
  setMessage(byId("login-error"), "");
  try {
    await api("/login", "POST", payload);
    form.reset();
    await enter();
  } catch (error) {
    if (!application.hidden) showSignin(error.message);
    else setMessage(byId("login-error"), error.message);
  }
});

byId("pair").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    await api("/pair", "POST", {token: pairToken, ...Object.fromEntries(new FormData(event.currentTarget))});
    pairToken = null;
    await enter();
  } catch (error) {
    setMessage(byId("pair-error"), error.message);
  }
});
byId("cancel-pair").addEventListener("click", () => {
  pairToken = null;
  showSignin();
});
byId("logout").addEventListener("click", guarded(async () => {
  await api("/logout", "POST");
  showSignin();
}));
byId("stop-all").addEventListener("click", guarded(async () => {
  byId("dialog").close("cancel");
  window.dispatchEvent(new Event("bud-emergency"));
  state.stopVoice();
  await api("/stop", "POST");
  state.markActive("");
  notice("Emergency stop applied", true);
}));
window.addEventListener("bud-signout", () => showSignin("Session ended. Sign in again."));
window.addEventListener("offline", () => {
  if (!application.hidden) notice("Host unreachable. Changes require a connection.");
  else setMessage(byId(pairToken ? "pair-error" : "login-error"), "Host unreachable");
});
window.addEventListener("online", () => { if (!application.hidden) status(); });

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
if (pairToken) showSignin();
else enter().catch(error => showSignin(error.message === "Sign in to BudAI" ? "" : error.message));
