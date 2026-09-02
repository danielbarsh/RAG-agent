/*
  Front end. Deliberately dependency-free: one HTML file, one stylesheet, one
  script, served by the same container as the API. No build step, no bundle, no
  second origin to authenticate, and nothing secret has anywhere to hide.
*/

const $ = (id) => document.getElementById(id);

const state = {
  sessionId: localStorage.getItem("sessionId") || newSessionId(),
  me: null,
  pendingUpload: null,
  streaming: false,
  jobPoll: null,
};
localStorage.setItem("sessionId", state.sessionId);

function newSessionId() {
  return "s" + Math.random().toString(36).slice(2, 12);
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

function renderSources(turn, items) {
  if (!items.length) return;
  const box = document.createElement("div");
  box.className = "sources";
  const list = items
    .map((s) => `<li><strong>[${s.n}] ${escapeHtml(s.title)}</strong><br /><span class="excerpt">${escapeHtml(s.excerpt)}…</span></li>`)
    .join("");
  box.innerHTML = `<h4>Sources</h4><ol>${list}</ol>`;
  turn.appendChild(box);
  scrollDown();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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

async function send(message) {
  if (state.streaming) return;
  state.streaming = true;
  $("send").disabled = true;

  const userTurn = addTurn("You", "user");
  userTurn.querySelector(".body").textContent = message;

  const agentTurn = addTurn("Librarian", "agent");
  const body = agentTurn.querySelector(".body");

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
        handleEvent(JSON.parse(frame.slice(6)), agentTurn, body);
      }
    }
  } catch (error) {
    body.textContent += `\n\n[The connection dropped: ${error.message}]`;
  } finally {
    state.streaming = false;
    $("send").disabled = false;
    refreshFiles();
  }
}

function handleEvent(event, turn, body) {
  if (event.type === "token") {
    body.textContent += event.text;
    scrollDown();
  } else if (event.type === "tool" && event.status === "running") {
    const note = document.createElement("div");
    note.className = "tool-note";
    note.textContent = event.name === "search_documents"
      ? "Searching the library…"
      : `Running ${event.name}…`;
    turn.appendChild(note);
    scrollDown();
  } else if (event.type === "citations") {
    turn.dataset.citations = JSON.stringify(event.items);
  } else if (event.type === "proposal") {
    renderProposal(turn, event.proposal);
  } else if (event.type === "done") {
    const citations = turn.dataset.citations ? JSON.parse(turn.dataset.citations) : [];
    renderSources(turn, citations);
  } else if (event.type === "error") {
    body.textContent += `\n\n[${event.message}]`;
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

    state.pendingUpload = grant;
    const box = $("attachment");
    box.classList.remove("hidden");
    box.innerHTML = `<span>${escapeHtml(file.name)} — ${(file.size / 1048576).toFixed(1)} MB, ready</span>
                     <span class="muted">upload id ${escapeHtml(grant.upload_id)}</span>`;
    $("composer-note").textContent = "Now say what to do with it, for example “add this as travel-policy.pdf”.";
    $("message").focus();
  } catch (error) {
    $("composer-note").textContent = `Upload failed: ${error.message}`;
  }
}

/* ------------------------------------------------------------------ rail */

async function refreshFiles() {
  try {
    const { files } = await api("/api/files");
    $("file-list").innerHTML = files.length
      ? files.map((f) => `<li><span>${escapeHtml(f.name)}</span><span>${(f.size / 1048576).toFixed(1)} MB</span></li>`).join("")
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
    $("job-list").innerHTML = jobs.map((job) => `
      <div class="job-card ${escapeHtml(job.status)}">
        <h3>${escapeHtml(job.operation)} ${escapeHtml(job.payload.target_name || "")}</h3>
        <div class="muted">${escapeHtml(job.status)}${job.attempts > 1 ? ` · attempt ${job.attempts}` : ""}</div>
        ${job.steps.length ? `<ol>${job.steps.map((s) => `<li>${escapeHtml(s.message)}</li>`).join("")}</ol>` : ""}
        ${job.result ? `<div class="muted">${escapeHtml(job.result)}</div>` : ""}
      </div>`).join("");
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

/* ------------------------------------------------------------------ init */

async function restoreTranscript() {
  try {
    const { messages } = await api(`/api/session/${encodeURIComponent(state.sessionId)}`);
    if (!messages.length) return;
    $("transcript").innerHTML = "";
    for (const message of messages) {
      const turn = addTurn(message.role === "user" ? "You" : "Librarian",
                           message.role === "user" ? "user" : "agent");
      turn.querySelector(".body").textContent = message.content || "";
    }
  } catch (_) {}
}

async function init() {
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
  refreshJobs().then((active) => { if (active) startJobPolling(); });

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
