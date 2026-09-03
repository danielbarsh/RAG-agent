/*
  Front end. Deliberately dependency-free: one HTML file, one stylesheet, one
  script, served by the same container as the API. No build step, no bundle, no
  second origin to authenticate, and nothing secret has anywhere to hide.
*/

const $ = (id) => document.getElementById(id);

const state = {
  sessionId: localStorage.getItem("sessionId") || newSessionId(),
  me: null,
  streaming: false,
  jobPoll: null,
  sessionTitles: loadTitles(),
};
localStorage.setItem("sessionId", state.sessionId);

function newSessionId() {
  return "s" + Math.random().toString(36).slice(2, 12);
}

function loadTitles() {
  try { return JSON.parse(localStorage.getItem("sessionTitles") || "{}"); } catch (_) { return {}; }
}

function cacheTitle(sessionId, text) {
  if (!text || state.sessionTitles[sessionId]) return;
  state.sessionTitles[sessionId] = text.trim().slice(0, 60);
  localStorage.setItem("sessionTitles", JSON.stringify(state.sessionTitles));
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).error || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}

/* ------------------------------------------------------------- transcript */

function addTurn(who, cssClass) {
  const turn = document.createElement("div");
  turn.className = `turn ${cssClass}`;
  turn.innerHTML = `<div class="who"></div><div class="body"></div>`;
  turn.querySelector(".who").textContent = who;
  $("transcript").appendChild(turn);
  scrollDown();
  return turn;
}

function scrollDown() {
  const t = $("transcript");
  t.scrollTop = t.scrollHeight;
}

/* An attachment rides inside the user's own turn as a leading marker line —
   "📎 name · size MB" — so the chat history (plain strings, no schema change)
   already carries what it needs to redraw the chip after a reload. */
const ATTACHMENT_LINE = /^📎 (.+) · ([\d.]+ MB)$/;

function attachmentMessage(filename, size) {
  return `📎 ${filename} · ${(size / 1048576).toFixed(1)} MB`;
}

function buildFileChip(name, size) {
  const chip = document.createElement("div");
  chip.className = "file-chip";
  const icon = document.createElement("span");
  icon.className = "file-chip-icon";
  icon.textContent = "📎";
  const nameEl = document.createElement("span");
  nameEl.className = "file-chip-name";
  nameEl.textContent = name;
  const sizeEl = document.createElement("span");
  sizeEl.className = "file-chip-size";
  sizeEl.textContent = size;
  chip.append(icon, nameEl, sizeEl);
  return chip;
}

function renderUserBody(el, content) {
  el.innerHTML = "";
  const lines = String(content ?? "").split("\n");
  const match = lines[0] && lines[0].match(ATTACHMENT_LINE);
  if (!match) {
    el.textContent = content || "";
    return;
  }
  el.appendChild(buildFileChip(match[1], match[2]));
  const rest = lines.slice(1).join("\n").trim();
  if (rest) {
    const caption = document.createElement("div");
    caption.className = "caption";
    caption.textContent = rest;
    el.appendChild(caption);
  }
}

function fileTypeLabel(title) {
  const match = /\.([a-z0-9]{2,4})$/i.exec(String(title || "").trim());
  return match ? match[1].toUpperCase() : "DOC";
}

function renderSources(turn, items) {
  if (!items.length) return;
  const box = document.createElement("div");
  box.className = "sources";
  const cards = items
    .map((s) => `
      <div class="source-card">
        <div class="source-card-head">
          <span class="source-badge">${s.n}</span>
          <span class="source-type">${escapeHtml(fileTypeLabel(s.title))}</span>
        </div>
        <div class="source-title" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</div>
        <div class="source-excerpt">${escapeHtml(s.excerpt)}…</div>
      </div>`)
    .join("");
  box.innerHTML = `<h4>Sources</h4><div class="source-grid">${cards}</div>`;
  turn.appendChild(box);
  scrollDown();
}

function citedNumbers(text) {
  const nums = new Set();
  const re = /\[(\d+)\]/g;
  let m;
  while ((m = re.exec(text))) nums.add(Number(m[1]));
  return nums;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ------------------------------------------------------------- markdown */
/*
  A minimal, dependency-free renderer for the agent's answers. Everything is
  HTML-escaped before any tag is added, so there is no path from model (or
  document) text to a raw tag landing in the DOM — only the fixed set of tags
  this function itself writes. Deliberately narrow: headings, lists, code
  spans/fences, bold, italic, paragraphs. No links, no raw HTML passthrough.
*/

function renderInline(text) {
  let out = escapeHtml(text);
  out = out.replace(/`([^`\n]+)`/g, (_, code) => `<code>${code}</code>`);
  out = out.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
  out = out.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  return out;
}

/* Minimal, dependency-free syntax highlighting: a shared keyword list across
   common languages plus regex-tokenized comments/strings/numbers/calls. Not
   a real grammar, but enough visual structure to make a code sample scannable
   instead of a wall of plain monospace text. */
const CODE_KEYWORDS = new Set([
  "def", "class", "return", "if", "elif", "else", "for", "while", "break", "continue",
  "import", "from", "as", "with", "try", "except", "finally", "raise", "yield", "lambda",
  "and", "or", "not", "in", "is", "pass", "global", "nonlocal", "assert", "del",
  "function", "const", "let", "var", "new", "this", "self", "async", "await", "export",
  "default", "switch", "case", "extends", "implements", "interface", "type", "enum",
  "public", "private", "protected", "static", "void", "null", "undefined", "true", "false",
  "True", "False", "None", "struct", "fn", "impl", "use", "pub", "mut", "match", "package",
  "func", "defer", "go", "chan", "select", "namespace", "using", "include", "echo", "then",
  "fi", "do", "done", "esac", "local", "readonly", "throw", "instanceof", "typeof",
]);

const CODE_TOKEN = /(#.*)|(\/\/.*)|(\/\*[\s\S]*?\*\/)|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)|(\b\d+(?:\.\d+)?\b)|(\b[A-Za-z_][A-Za-z0-9_]*\b)/g;

function highlightCode(code) {
  let out = "";
  let last = 0;
  let m;
  CODE_TOKEN.lastIndex = 0;
  while ((m = CODE_TOKEN.exec(code))) {
    out += escapeHtml(code.slice(last, m.index));
    const text = m[0];
    let cls = null;
    if (m[1] || m[2] || m[3]) cls = "tok-comment";
    else if (m[4]) cls = "tok-string";
    else if (m[5]) cls = "tok-number";
    else if (m[6]) {
      if (CODE_KEYWORDS.has(text)) cls = "tok-keyword";
      else if (/^\s*\(/.test(code.slice(CODE_TOKEN.lastIndex, CODE_TOKEN.lastIndex + 4))) cls = "tok-fn";
    }
    out += cls ? `<span class="${cls}">${escapeHtml(text)}</span>` : escapeHtml(text);
    last = CODE_TOKEN.lastIndex;
  }
  out += escapeHtml(code.slice(last));
  return out;
}

const BLOCK_BREAK = /^\s*[-*]\s+|^\s*\d+[.)]\s+|^#{1,6}\s+|^```/;

function renderMarkdown(raw) {
  const lines = String(raw ?? "").replace(/\r\n/g, "\n").split("\n");
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (/^```/.test(line)) {
      const lang = line.slice(3).trim();
      const code = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) { code.push(lines[i]); i++; }
      i++;
      const langAttr = lang ? ` data-lang="${escapeHtml(lang)}"` : "";
      blocks.push(`<pre${langAttr}><code>${highlightCode(code.join("\n"))}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = heading[1].length;
      blocks.push(`<h${level} dir="auto">${renderInline(heading[2])}</h${level}>`);
      i++;
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(`<li dir="auto">${renderInline(lines[i].replace(/^\s*[-*]\s+/, ""))}</li>`);
        i++;
      }
      // dir="auto" on the <ul> itself (not just each <li>) is what moves the
      // bullet marker to the right side for RTL content - unicode-bidi:plaintext
      // in CSS re-flows the text but can't touch marker placement, which is
      // driven by the resolved `direction`, so the list needs its own HTML dir.
      blocks.push(`<ul dir="auto">${items.join("")}</ul>`);
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        items.push(`<li dir="auto">${renderInline(lines[i].replace(/^\s*\d+[.)]\s+/, ""))}</li>`);
        i++;
      }
      blocks.push(`<ol dir="auto">${items.join("")}</ol>`);
      continue;
    }

    if (!line.trim()) { i++; continue; }

    const para = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !BLOCK_BREAK.test(lines[i])) {
      para.push(lines[i]);
      i++;
    }
    blocks.push(`<p dir="auto">${para.map(renderInline).join("<br>")}</p>`);
  }
  return blocks.join("");
}

const VERB = { add: "Add a file", replace: "Replace a file", delete: "Delete a file" };

function renderProposal(turn, proposal) {
  const card = document.createElement("div");
  card.className = "warrant";
  const overwrite = proposal.overwrites_existing;
  card.innerHTML = `
    <div class="kind">${VERB[proposal.operation] || "Change"} — waiting for you</div>
    <div class="subject">${escapeHtml(proposal.target_name)}</div>
    <p>${proposal.operation === "delete"
        ? "This removes the file from the library and every chunk of it from the index."
        : overwrite
          ? "This overwrites the existing file of that name. The old text stops being searchable."
          : "This adds the uploaded PDF to the library and indexes it."}</p>
    <div class="actions">
      <button class="primary" type="button">${proposal.operation === "delete" ? "Delete" : overwrite ? "Replace" : "Add"} ${escapeHtml(proposal.target_name)}</button>
      <button class="secondary" type="button">Keep as is</button>
    </div>`;

  const [confirm, discard] = card.querySelectorAll("button");

  confirm.addEventListener("click", async () => {
    confirm.disabled = discard.disabled = true;
    try {
      const job = await api("/api/jobs/confirm", {
        method: "POST",
        body: JSON.stringify({ session_id: state.sessionId, proposal_id: proposal.proposal_id }),
      });
      card.querySelector(".actions").remove();
      const note = document.createElement("p");
      note.className = "settled";
      note.textContent = `Queued as ${job.job_id}. Progress is in the Jobs panel and survives a refresh.`;
      card.appendChild(note);
      startJobPolling();
    } catch (error) {
      confirm.disabled = discard.disabled = false;
      const note = document.createElement("p");
      note.className = "settled";
      note.textContent = `Could not queue it: ${error.message}`;
      card.appendChild(note);
    }
  });

  discard.addEventListener("click", () => {
    card.querySelector(".actions").remove();
    const note = document.createElement("p");
    note.className = "settled";
    note.textContent = "Left alone. Nothing was changed.";
    card.appendChild(note);
  });

  turn.appendChild(card);
  scrollDown();
}

/* ------------------------------------------------------------------ chat */

const STALL_HINT_MS = 12000;

const TOOL_LABELS = {
  search_documents: (a) => `Searching for “${a.query || "…"}”`,
  list_files: (a) => a.name_contains ? `Listing files matching “${a.name_contains}”` : "Listing files",
  propose_add: (a) => `Looking at ${a.target_name || "the uploaded file"}`,
  propose_replace: (a) => `Finding ${a.file_reference || "the file"} to replace`,
  propose_delete: (a) => `Finding ${a.file_reference || "the file"} to delete`,
};

function toolLabel(event) {
  const make = TOOL_LABELS[event.name];
  return make ? make(event.args || {}) : `Running ${event.name}`;
}

function addStep(turn, text) {
  const note = document.createElement("div");
  note.className = "tool-note running";
  note.textContent = text;
  turn.appendChild(note);
  scrollDown();
  return note;
}

function settleStep(note, text) {
  if (!note) return;
  note.classList.remove("running");
  note.classList.add("done");
  if (text) note.textContent = text;
}

/* The "Thinking" note is a bridge over a silent gap (before the first token,
   or between a finished tool call and the next one) — it never had anything
   worth keeping once that gap closes, so it's discarded rather than settled.
   A real tool-status note ("Searching for … . Done.") is a record of what the
   agent did and stays in the transcript. */
function clearNote(toolState) {
  if (!toolState.note) return;
  if (toolState.notePlaceholder) {
    toolState.note.remove();
  } else {
    settleStep(toolState.note);
  }
  toolState.note = null;
}

async function send(message) {
  if (state.streaming) return;
  state.streaming = true;
  $("send").disabled = true;
  $("file-input").disabled = true;

  const userTurn = addTurn("You", "user");
  renderUserBody(userTurn.querySelector(".body"), message);
  cacheTitle(state.sessionId, message);

  const agentTurn = addTurn("Librarian", "agent");
  const body = agentTurn.querySelector(".body");

  const toolState = { note: addStep(agentTurn, "Thinking"), notePlaceholder: true };
  let lastActivity = Date.now();
  const stallTimer = setInterval(() => {
    if (Date.now() - lastActivity >= STALL_HINT_MS && !toolState.stallNote) {
      toolState.stallNote = addStep(agentTurn, "Still working — this is taking longer than usual");
    }
  }, 2000);

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId, message }),
    });
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let cut;
      while ((cut = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        if (!frame.startsWith("data: ")) continue;
        lastActivity = Date.now();
        if (toolState.stallNote) {
          toolState.stallNote.remove();
          toolState.stallNote = null;
        }
        handleEvent(JSON.parse(frame.slice(6)), agentTurn, body, toolState);
      }
    }
  } catch (error) {
    clearNote(toolState);
    toolState.raw = (toolState.raw || "") + `\n\n[The connection dropped: ${error.message}]`;
    body.innerHTML = renderMarkdown(toolState.raw);
  } finally {
    clearInterval(stallTimer);
    clearNote(toolState);
    if (toolState.stallNote) toolState.stallNote.remove();
    state.streaming = false;
    $("send").disabled = false;
    $("file-input").disabled = false;
    refreshFiles();
    refreshSessions();
  }
}

function handleEvent(event, turn, body, toolState) {
  if (event.type === "token") {
    clearNote(toolState);
    toolState.raw = (toolState.raw || "") + event.text;
    body.innerHTML = renderMarkdown(toolState.raw);
    scrollDown();
  } else if (event.type === "tool" && event.status === "running") {
    clearNote(toolState);
    toolState.label = toolLabel(event);
    toolState.note = addStep(turn, toolState.label + "…");
    toolState.notePlaceholder = false;
  } else if (event.type === "tool" && event.status === "done") {
    settleStep(toolState.note, toolState.label ? `${toolState.label}. Done.` : undefined);
    // A tool finishing doesn't mean a token is imminent — reasoning models can sit
    // silent for many seconds composing the answer. Keep a visible "running" note
    // so the turn never looks frozen before the stall hint's 12s threshold. It's a
    // bridge, not a record, so it gets discarded rather than settled once it's crossed.
    toolState.note = addStep(turn, "Thinking");
    toolState.notePlaceholder = true;
  } else if (event.type === "citations") {
    turn.dataset.citations = JSON.stringify(event.items);
  } else if (event.type === "proposal") {
    renderProposal(turn, event.proposal);
  } else if (event.type === "done") {
    const citations = turn.dataset.citations ? JSON.parse(turn.dataset.citations) : [];
    const cited = citedNumbers(body.textContent);
    renderSources(turn, citations.filter((c) => cited.has(c.n)));
  } else if (event.type === "error") {
    clearNote(toolState);
    toolState.raw = (toolState.raw || "") + `\n\n[${event.message}]`;
    body.innerHTML = renderMarkdown(toolState.raw);
  }
}

/* --------------------------------------------------------------- uploads */

async function attach(file) {
  $("composer-note").textContent = `Uploading ${file.name}…`;
  try {
    const grant = await api("/api/uploads/sas", {
      method: "POST",
      body: JSON.stringify({
        session_id: state.sessionId,
        filename: file.name,
        size: file.size,
      }),
    });

    // Straight to blob storage. The PDF never passes through the API container,
    // which is what makes a 50 MB upload uneventful.
    const put = await fetch(grant.upload_url, {
      method: "PUT",
      headers: { "x-ms-blob-type": "BlockBlob", "Content-Type": "application/pdf" },
      body: file,
    });
    if (!put.ok) throw new Error(`storage returned ${put.status}`);

    $("composer-note").textContent = "";
    const line = attachmentMessage(file.name, file.size);
    if (state.streaming) {
      // Can't send mid-turn — fold it into the composer so it rides along
      // with whatever the user sends next, instead of getting lost.
      const existing = $("message").value;
      $("message").value = existing ? `${line}\n\n${existing}` : line;
      $("message").dispatchEvent(new Event("input"));
      $("composer-note").textContent = "Attached — it will go with your next message.";
    } else {
      send(line);
    }
  } catch (error) {
    $("composer-note").textContent = `Upload failed: ${error.message}`;
  }
}

/* ------------------------------------------------------------------ rail */

const FILE_ICON = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>`;

async function refreshFiles() {
  try {
    const { files } = await api("/api/files");
    $("file-list").innerHTML = files.length
      ? files.map((f) => `<li><span class="file-icon">${FILE_ICON}</span><span class="file-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span><span class="file-size">${(f.size / 1048576).toFixed(1)} MB</span></li>`).join("")
      : `<li class="muted">The library is empty.</li>`;
  } catch (error) {
    $("file-list").innerHTML = `<li class="muted">Could not list files: ${escapeHtml(error.message)}</li>`;
  }
}

async function refreshStatus() {
  try {
    const [stats, status] = await Promise.all([api("/api/stats"), api("/api/indexer/status")]);
    const last = status.lastResult || {};
    $("index-stats").innerHTML = `
      <div><dt>Files</dt><dd>${stats.files}</dd></div>
      <div><dt>Chunks</dt><dd>${stats.chunks}</dd></div>
      <div><dt>Last run</dt><dd>${escapeHtml(last.status || "none")}</dd></div>
      <div><dt>Indexed</dt><dd>${last.itemsProcessed ?? 0}</dd></div>
      <div><dt>Failed</dt><dd>${last.itemsFailed ?? 0}</dd></div>
      <div><dt>Finished</dt><dd>${last.endTime ? new Date(last.endTime).toLocaleTimeString() : "—"}</dd></div>`;
    const errors = (last.errors || []).slice(0, 3);
    $("index-errors").innerHTML = errors.length
      ? errors.map((e) => `<div>${escapeHtml(e.name || e.key || "item")}: ${escapeHtml(e.message || "")}</div>`).join("")
      : "";
  } catch (error) {
    $("index-stats").innerHTML = `<div class="muted">${escapeHtml(error.message)}</div>`;
  }
}

async function refreshJobs() {
  try {
    const { jobs } = await api(`/api/jobs?session_id=${encodeURIComponent(state.sessionId)}`);
    if (!jobs.length) {
      $("job-list").innerHTML = `<p class="muted">Nothing queued.</p>`;
      return false;
    }
    $("job-list").innerHTML = jobs.map((job) => {
      const active = job.status === "queued" || job.status === "running" || job.status === "retrying";
      return `
      <div class="job-card ${escapeHtml(job.status)}">
        <h3>${active ? '<span class="job-spinner" aria-hidden="true"></span>' : ""}${escapeHtml(job.operation)} ${escapeHtml(job.payload.target_name || "")}</h3>
        <div class="muted">${escapeHtml(job.status)}${job.attempts > 1 ? ` · attempt ${job.attempts}` : ""}</div>
        ${job.steps.length ? `<ol>${job.steps.map((s) => `<li>${escapeHtml(s.message)}</li>`).join("")}</ol>` : ""}
        ${job.result ? `<div class="muted">${escapeHtml(job.result)}</div>` : ""}
      </div>`;
    }).join("");
    return jobs.some((job) => job.status === "queued" || job.status === "running" || job.status === "retrying");
  } catch (error) {
    return false;
  }
}

function startJobPolling() {
  if (state.jobPoll) clearInterval(state.jobPoll);
  const tick = async () => {
    const active = await refreshJobs();
    if (!active) {
      clearInterval(state.jobPoll);
      state.jobPoll = null;
      refreshFiles();
      refreshStatus();
    }
  };
  tick();
  state.jobPoll = setInterval(tick, 3000);
}

/* --------------------------------------------------------------- sessions */

const OPENING_HTML = `<div class="opening">
        <p class="serif">Ask what the library says, or tell me what to change in it.</p>
        <p class="muted">Changes are proposed here and take effect only after you confirm them.</p>
      </div>`;

async function refreshSessions() {
  try {
    const { sessions } = await api("/api/sessions");
    if (!sessions.length) {
      $("session-list").innerHTML = `<li class="muted">No chats yet.</li>`;
      return;
    }
    $("session-list").innerHTML = sessions.map((s) => {
      const title = state.sessionTitles[s.session_id]
        || (s.updated_at ? new Date(s.updated_at).toLocaleString() : s.session_id);
      const active = s.session_id === state.sessionId ? " active" : "";
      return `<li class="session-item${active}" data-session="${escapeHtml(s.session_id)}" title="${escapeHtml(title)}">${escapeHtml(title)}</li>`;
    }).join("");
    $("session-list").querySelectorAll(".session-item").forEach((li) => {
      li.addEventListener("click", () => switchSession(li.dataset.session));
    });
  } catch (error) {
    $("session-list").innerHTML = `<li class="muted">Could not load chats.</li>`;
  }
}

async function switchSession(sessionId) {
  if (state.streaming || sessionId === state.sessionId) return;
  state.sessionId = sessionId;
  localStorage.setItem("sessionId", sessionId);

  $("message").value = "";
  $("composer-note").textContent = "";

  if (state.jobPoll) {
    clearInterval(state.jobPoll);
    state.jobPoll = null;
  }

  await restoreTranscript();
  refreshSessions();
  refreshJobs().then((active) => { if (active) startJobPolling(); });
}

function newChat() {
  if (state.streaming) return;
  switchSession(newSessionId());
}

/* ------------------------------------------------------------------ init */

async function restoreTranscript() {
  $("transcript").innerHTML = `<p class="muted">Loading…</p>`;
  try {
    const { messages } = await api(`/api/session/${encodeURIComponent(state.sessionId)}`);
    $("transcript").innerHTML = "";
    if (!messages.length) {
      $("transcript").innerHTML = OPENING_HTML;
      return;
    }
    for (const message of messages) {
      const isUser = message.role === "user";
      const turn = addTurn(isUser ? "You" : "Librarian", isUser ? "user" : "agent");
      const bodyEl = turn.querySelector(".body");
      if (isUser) {
        renderUserBody(bodyEl, message.content || "");
      } else {
        bodyEl.innerHTML = renderMarkdown(message.content || "");
      }
    }
    const firstUser = messages.find((m) => m.role === "user");
    if (firstUser) cacheTitle(state.sessionId, firstUser.content);
  } catch (_) {
    $("transcript").innerHTML = OPENING_HTML;
  }
}

const PLACEHOLDER_BY_LANG = {
  he: "מה כתוב במדיניות הנסיעות לגבי דמי רכבת?",
};

function applyLocalizedPlaceholder() {
  const lang = (navigator.language || "en").slice(0, 2).toLowerCase();
  const text = PLACEHOLDER_BY_LANG[lang];
  if (text) $("message").placeholder = text;
}

async function init() {
  applyLocalizedPlaceholder();
  try {
    state.me = await api("/api/me");
    $("whoami").textContent = state.me.is_admin
      ? `${state.me.name} — may change files`
      : `${state.me.name} — read only (needs the ${state.me.admin_role} role to change files)`;
  } catch (error) {
    $("whoami").textContent = "Not signed in.";
  }

  await restoreTranscript();
  refreshFiles();
  refreshStatus();
  refreshSessions();
  refreshJobs().then((active) => { if (active) startJobPolling(); });

  $("new-chat").addEventListener("click", newChat);

  $("composer").addEventListener("submit", (event) => {
    event.preventDefault();
    const text = $("message").value.trim();
    if (!text) return;
    $("message").value = "";
    $("message").style.height = "auto";
    send(text);
  });

  $("message").addEventListener("input", (event) => {
    event.target.style.height = "auto";
    event.target.style.height = Math.min(event.target.scrollHeight, 180) + "px";
  });

  $("message").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("composer").requestSubmit();
    }
  });

  $("file-input").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (file) attach(file);
    event.target.value = "";
  });

  $("refresh-files").addEventListener("click", refreshFiles);
  $("refresh-status").addEventListener("click", refreshStatus);

  $("backfill").addEventListener("click", async () => {
    $("backfill").disabled = true;
    $("backfill").textContent = "Backfill started";
    try {
      await api("/api/admin/backfill", { method: "POST" });
      setTimeout(refreshStatus, 4000);
    } catch (error) {
      $("backfill").textContent = `Backfill failed: ${error.message}`;
    } finally {
      setTimeout(() => {
        $("backfill").disabled = false;
        $("backfill").textContent = "Run full backfill";
      }, 6000);
    }
  });

  setInterval(refreshStatus, 30000);
}

init();
