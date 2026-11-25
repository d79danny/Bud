import {
  replace,   api, ask, button, byId, checkbox, confirmAction, element, entry, field, filesFrom, guarded, input,
  muted, notice, panel, pre, response, row, select, stack, textArea, uploadFiles,
} from "./common.js";

function orderedFolders(folders, parent = null, depth = 0) {
  return folders.filter(item => item.parent === parent).flatMap(item => [
    [String(item.id), "   ".repeat(depth) + item.title],
    ...orderedFolders(folders, item.id, depth + 1),
  ]);
}

export function installChat(state) {
  let archived = false;
  let query = "";
  let more = 0;
  let threadEntries = [];
  let folders = [];
  let chat = null;
  let stream = null;
  let queuedAttachments = [];

  async function newThread(title) {
    if (stream) throw new Error("Wait for the active conversation or stop it");
    let fields;
    if (title) fields = {title, folder: ""};
    else {
      fields = await ask("New conversation", [
        field("Title", input("title", {required: "", maxlength: 120, autofocus: ""})),
        field("Folder", select("folder", [["", "No folder"], ...orderedFolders(folders)])),
      ], {submit: "Create"});
    }
    if (!fields) return null;
    const item = await api("/conversations", "POST", {title: fields.title, folder: fields.folder ? Number(fields.folder) : null});
    archived = false;
    query = "";
    await showThread(item.id);
    return item;
  }

  async function folderDialog(folder) {
    const values = await ask(folder ? "Edit folder" : "New folder", [
      field("Folder name", input("title", {value: folder?.title || "", maxlength: 120, required: ""})),
      field("Parent folder", select("parent", [["", "No parent"], ...orderedFolders(folders).filter(([id]) => Number(id) !== folder?.id)], folder?.parent)),
    ], {submit: folder ? "Save" : "Create"});
    if (values) {
      await api(folder ? "/folders/" + folder.id : "/folders", folder ? "PATCH" : "POST", {
        title: values.title, parent: values.parent ? Number(values.parent) : null,
      });
      await refreshSidebar();
    }
  }

  async function renderThreads(list) {
    const url = `/conversations?q=${encodeURIComponent(query)}&archived=${archived}&offset=${more}`;
    const result = await api(url);
    threadEntries = more ? threadEntries.concat(result) : result;
    const threadLink = item => button((item.pinned ? "• " : "") + item.title, () => showThread(item.id),
      {class: "thread-link", "aria-current": state.thread === item.id, title: item.title});
    function children(parent = null) {
      return [
        ...folders.filter(folder => folder.parent === parent).map(folder => {
          const inside = children(folder.id);
          return element("details", {open: ""},
            element("summary", {}, folder.title),
            row(button("Edit", () => folderDialog(folder)), button("Remove", async () => {
              if (await confirmAction("Remove folder", "Conversations and subfolders will move to its parent.")) {
                await api("/folders/" + folder.id, "DELETE");
                await refreshSidebar();
              }
            })),
            inside);
        }),
        ...threadEntries.filter(item => (item.folder ?? null) === parent).map(threadLink),
      ];
    }
    replace(list, children(),
      result.length === 200 ? button("More conversations", async () => {
        more += 200;
        await renderThreads(list);
      }) : null,
      threadEntries.length ? null : muted("No conversations"));
  }

  async function refreshSidebar() {
    folders = await api("/folders");
    more = 0;
    const list = element("div", {class: "thread-list"});
    const search = input("conversation-search", {type: "search", placeholder: "Search conversations",
      "aria-label": "Search conversations", value: query});
    const form = element("form", {onsubmit: guarded(async event => {
      event.preventDefault();
      query = search.value.trim();
      more = 0;
      await renderThreads(list);
    })}, search);
    replace(byId("thread-controls"),
      row(button("New", () => newThread(), {"aria-label": "New conversation"}), button("Folder", () => folderDialog()),
        button(archived ? "Inbox" : "Archive", async () => {
          archived = !archived;
          await refreshSidebar();
        })),
      form, list,
    );
    await renderThreads(list);
  }

  async function renderMarkdown(node, text) {
    node.textContent = text;
    if (!text || text.length > 24000) return;
    const rendered = await response("/render", "POST", {text});
    node.innerHTML = await rendered.text();
    node.classList.add("markdown");
    for (const link of node.querySelectorAll("a[href]")) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
  }

  function addMessage(log, role, text) {
    const content = element("div", {class: "message-content"}, text);
    const container = element("article", {class: "message " + role},
      row(element("span", {class: "speaker"}, role === "assistant" ? "Bud" : "You")),
      content,
    );
    log.append(container);
    return {container, content};
  }

  function addTool(log, item) {
    const summary = `${item.tool || item.tool_name} · ${item.status || "recorded"}${item.duration_ms != null ? " · " + item.duration_ms + " ms" : ""}`;
    const result = item.result;
    let artifacts = [];
    if (result?.artifact) artifacts.push(result.artifact);
    if (result?.outputs) artifacts = artifacts.concat(result.outputs);
    log.append(element("details", {class: "panel"}, element("summary", {}, summary),
      item.error ? element("p", {class: "alert"}, item.error) : null,
      pre(result ?? item.content ?? null),
      artifacts.map(file => element("a", {href: "/api/artifacts/" + encodeURIComponent(file.id) + "?download=true",
        download: file.name}, file.name))));
  }

  function attachLinks(container, items) {
    if (items?.length) container.append(row(items.map(file =>
      element("a", {href: "/api/artifacts/" + encodeURIComponent(file.id) + "?download=true", download: file.name}, file.name))));
  }

  async function loadHistory(log, identifier, before = null) {
    const data = await api(`/conversations/${identifier}${before ? "?before=" + before : ""}`);
    const current = before ? Array.from(log.childNodes).filter(node => !node.matches?.(".load-earlier")) : [];
    replace(log);
    if (data.messages.length === 150) log.append(button("Load earlier messages", () =>
      loadHistory(log, identifier, data.messages[0].id), {class: "load-earlier"}));
    for (const item of data.messages) {
      if (item.role === "tool") {
        try {
          addTool(log, JSON.parse(item.content));
        } catch (_) {
          addTool(log, item);
        }
      } else if (item.content) {
        const {container, content} = addMessage(log, item.role, item.content);
        if (item.role === "assistant") {
          renderMarkdown(content, item.content).catch(() => {});
          container.firstChild.append(button("Copy", () => navigator.clipboard.writeText(item.content)));
        } else {
          attachLinks(container, item.attachments);
          container.firstChild.append(button("Edit and resend", () => {
            chat.text.value = item.content;
            chat.text.focus();
          }));
        }
      }
    }
    log.append(...current);
    return data;
  }

  async function threadSettings(item, update) {
    const values = await ask("Conversation settings", [
      field("Title", input("title", {value: item.title, required: "", maxlength: 120})),
      field("Folder", select("folder", [["", "No folder"], ...orderedFolders(folders)], item.folder)),
    ]);
    if (values) await update({title: values.title, folder: values.folder ? Number(values.folder) : null});
  }

  async function addAttachments(files, display) {
    const space = 5 - queuedAttachments.length;
    if (files.length > space) throw new Error("A message can include up to five attachments");
    queuedAttachments.push(...await uploadFiles(files));
    showAttachments(display);
  }

  function showAttachments(display) {
    replace(display, queuedAttachments.map(file => button(file.name + " ×", () => {
      queuedAttachments = queuedAttachments.filter(item => file.id !== item.id);
      showAttachments(display);
    }, {title: "Remove " + file.name})));
  }

  async function showThread(identifier) {
    if (stream && identifier !== state.thread) throw new Error("Wait for the active conversation or stop it");
    state.thread = identifier;
    if (state.current !== "chat") {
      await state.navigate("chat");
      return;
    }
    await refreshSidebar();
    await renderPage();
  }

  async function stop() {
    if (!stream) return;
    await api("/chat/stop", "POST");
  }

  async function send(text, source = "chat", speak = false) {
    if (stream) throw new Error("A conversation is active");
    if (!state.thread) await newThread(source === "voice" ? "Voice conversation" : "Conversation");
    if (state.current !== "chat") await state.navigate("chat");
    if (!chat) throw new Error("Open a conversation first");
    const localChat = chat;
    const controller = new AbortController();
    stream = controller;
    state.markActive("Generating");
    chat.send.disabled = true;
    chat.stop.disabled = false;
    const chosen = queuedAttachments;
    let answer = "";
    let rendered = null;
    let completed = false;
    let quiet = false;
    try {
      const result = await response(`/conversations/${state.thread}/chat`, "POST", {
        text, source, attachments: chosen.map(file => file.id),
        grants: {
          files: filesFrom(chat.grants.files.value),
          desktop: chat.grants.desktop.checked, browser: chat.grants.browser.checked,
          terminal: chat.grants.terminal.checked, media: chat.grants.media.checked,
        },
      }, controller.signal);
      const user = addMessage(localChat.log, "user", text);
      attachLinks(user.container, chosen);
      chat.text.value = "";
      queuedAttachments = [];
      showAttachments(chat.attachments);
      const read = result.body.pipeThrough(new TextDecoderStream()).getReader();
      let pending = "";
      while (true) {
        const {value, done} = await read.read();
        if (done) break;
        const lines = (pending + value).split("\n");
        pending = lines.pop();
        for (const line of lines) {
          if (!line) continue;
          const event = JSON.parse(line);
          if (event.event === "token") {
            if (!rendered) rendered = addMessage(localChat.log, "assistant", "");
            answer += event.text;
            rendered.content.textContent = answer;
          } else if (event.event === "tool") {
            if (rendered && answer) await renderMarkdown(rendered.content, answer).catch(() => {});
            addTool(localChat.log, event);
            rendered = null;
            answer = "";
            state.markActive("Generating");
          } else if (event.event === "approval") {
            state.markActive("Awaiting approval");
            const allow = await ask("Approve " + event.tool, pre(event.arguments), {submit: "Approve exact action"});
            if (!controller.signal.aborted) await api("/approval", "POST", {id: event.id, allow: Boolean(allow)});
          } else if (event.event === "agent") {
            state.markActive(event.state === "running" ? "Agent · " + event.role : "Generating");
          } else if (event.event === "error") {
            throw new Error(event.detail);
          } else if (event.event === "done") {
            completed = true;
          } else if (event.event === "stopped") {
            quiet = true;
          }
          localChat.log.scrollTop = localChat.log.scrollHeight;
        }
      }
      if (!completed && !quiet) throw new Error("The response ended before completion");
      if (completed && rendered) {
        await renderMarkdown(rendered.content, answer).catch(() => {});
        rendered.container.firstChild.append(button("Copy", () => navigator.clipboard.writeText(answer)));
      }
      if (completed && speak && answer) await state.playSpeech(answer.slice(0, 6000));
      if (quiet) notice("Generation stopped", true);
      return completed ? answer : "";
    } finally {
      if (!completed && !quiet) controller.abort();
      stream = null;
      localChat.send.disabled = false;
      localChat.stop.disabled = true;
      state.markActive("");
      localChat.log.scrollTop = localChat.log.scrollHeight;
    }
  }

  async function renderPage() {
    if (stream && chat && chat.identifier === state.thread) {
      replace(state.page, chat.root);
      return;
    }
    if (!state.thread) {
      state.heading("Chat");
      replace(state.page, panel("Conversations", button("New conversation", () => newThread(), {class: "primary"})));
      return;
    }
    const log = element("div", {class: "chat-log", role: "log", "aria-label": "Conversation"});
    const data = await loadHistory(log, state.thread);
    let item = data.thread;
    state.heading(item.title);
    queuedAttachments = [];
    const text = textArea("prompt", {placeholder: "Message Bud", "aria-label": "Message to Bud",
      required: "", maxlength: 24000});
    const attachments = row();
    const upload = input("attachments", {type: "file", multiple: "", "aria-label": "Attach files"});
    const sendButton = element("button", {class: "primary"}, "Send");
    const stopButton = button("Stop", stop, {disabled: true});
    const grants = {
      files: textArea("files", {placeholder: "/home/you/Documents\nOne path per line", "aria-label": "Files accessible to Bud"}),
      desktop: input("desktop", {type: "checkbox"}),
      terminal: input("terminal", {type: "checkbox"}),
      browser: input("browser", {type: "checkbox"}),
      media: input("media", {type: "checkbox"}),
    };
    const form = element("form", {class: "chat-compose"}, text, attachments,
      element("details", {}, element("summary", {}, "Tool access for this message"),
        field("Allowed file paths", grants.files),
        row(...["desktop", "terminal", "browser", "media"].map(key => element("label", {class: "inline"}, grants[key], key)))),
      row(upload, element("span", {class: "spacer"}), stopButton, sendButton));
    upload.addEventListener("change", guarded(async () => {
      await addAttachments(upload.files, attachments);
      upload.value = "";
    }));
    form.addEventListener("dragover", event => { event.preventDefault(); form.classList.add("drag"); });
    form.addEventListener("dragleave", () => form.classList.remove("drag"));
    form.addEventListener("drop", guarded(async event => {
      event.preventDefault();
      form.classList.remove("drag");
      await addAttachments(event.dataTransfer.files, attachments);
    }));
    form.addEventListener("submit", guarded(async event => {
      event.preventDefault();
      if (text.value.trim()) await send(text.value.trim());
    }));
    text.addEventListener("keydown", event => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
    const update = async patch => {
      item = await api("/conversations/" + item.id, "PATCH", patch);
      await refreshSidebar();
      await renderPage();
    };
    const actions = row(
      button("Rename or move", () => threadSettings(item, update)),
      button(item.pinned ? "Unpin" : "Pin", () => update({pinned: !item.pinned})),
      button(item.archived ? "Restore" : "Archive", async () => {
        await api("/conversations/" + item.id, "PATCH", {archived: !item.archived});
        state.thread = null;
        await refreshSidebar();
        await renderPage();
      }),
      element("a", {href: "/api/conversations/" + item.id + "/export", download: ""}, "Export"),
      button("Save summary to Memory", async () => {
        await api("/conversations/" + item.id + "/summarize", "POST");
        notice("Ollama summary saved in Memory", true);
      }, {disabled: !data.messages.length}),
      button("Regenerate", async () => {
        const latest = await api("/conversations/" + item.id);
        const lastUser = latest.messages.filter(message => message.role === "user").at(-1);
        if (!lastUser) throw new Error("Send a message first");
        queuedAttachments = lastUser.attachments || [];
        await send(lastUser.content, "retry");
      }, {disabled: !data.messages.some(message => message.role === "user")}),
      button("Delete", async () => {
        const values = await ask("Delete conversation", checkbox("Delete derived memories and observations", "derived", true),
          {submit: "Delete permanently"});
        if (!values) return;
        await api("/conversations/" + item.id + "?derived=" + Boolean(values.derived), "DELETE");
        state.thread = null;
        await refreshSidebar();
        await renderPage();
      }, {class: "danger"}),
    );
    const root = element("section", {class: "chat"}, actions, log, form);
    chat = {root, log, text, send: sendButton, stop: stopButton, grants, attachments, identifier: item.id};
    replace(state.page, root);
    log.scrollTop = log.scrollHeight;
  }

  state.chat = {
    newThread, refreshSidebar, renderPage, showThread, send, stop,
    reset() {
      stream?.abort();
      byId("dialog").close("cancel");
      stream = null;
      chat = null;
      threadEntries = [];
      folders = [];
      archived = false;
      query = "";
      queuedAttachments = [];
    },
  };
}
