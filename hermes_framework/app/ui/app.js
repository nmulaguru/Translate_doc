/* Hermes UI — minimal SSE consumer. */
const els = {
  containerId: document.getElementById("container-id"),
  userMsg: document.getElementById("user-msg"),
  send: document.getElementById("send"),
  thinking: document.getElementById("thinking-stream"),
  planTree: document.getElementById("plan-tree"),
  clarifications: document.getElementById("clarifications"),
  questions: document.getElementById("questions"),
  events: document.getElementById("event-list"),
  finalSection: document.getElementById("final"),
  finalAnswer: document.getElementById("final-answer"),
  artifacts: document.getElementById("artifacts"),
  sessionId: document.getElementById("session-id"),
};

let currentSession = null;
let currentSource = null;
const taskRows = new Map();

document.querySelectorAll(".suggestions button").forEach((btn) =>
  btn.addEventListener("click", () => {
    els.userMsg.value = btn.dataset.q;
  })
);

els.send.addEventListener("click", () => sendMessage());

async function sendMessage() {
  const container_id = els.containerId.value.trim();
  const message = els.userMsg.value.trim();
  if (!message) return;
  resetUi();

  const created = await fetch("/v1/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ container_id }),
  }).then((r) => r.json());

  currentSession = created.session_id;
  els.sessionId.textContent = currentSession;

  openStream(currentSession);

  await fetch(`/v1/sessions/${currentSession}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, container_id }),
  });
}

function resetUi() {
  els.thinking.textContent = "";
  els.planTree.classList.add("empty");
  els.planTree.textContent = "No plan yet.";
  els.clarifications.classList.add("hidden");
  els.questions.innerHTML = "";
  els.events.innerHTML = "";
  els.finalSection.classList.add("hidden");
  els.finalAnswer.textContent = "";
  els.artifacts.innerHTML = "";
  taskRows.clear();
  if (currentSource) {
    currentSource.close();
    currentSource = null;
  }
}

function openStream(sessionId) {
  const src = new EventSource(`/v1/sessions/${sessionId}/events`);
  currentSource = src;
  const handlers = [
    "session.started",
    "container.resolved",
    "planner.thinking",
    "planner.text",
    "plan_mode.question",
    "plan_mode.answered",
    "plan.created",
    "plan.repaired",
    "plan.replanning",
    "task.started",
    "task.tool_call",
    "task.tool_result",
    "task.code_generated",
    "task.code_executing",
    "task.code_progress",
    "task.code_stdout",
    "task.mcp_call",
    "task.mcp_result",
    "task.filter_summary",
    "task.execution_plan",
    "task.code_stderr",
    "task.completed",
    "task.retrying",
    "task.failed",
    "task.skipped",
    "subagent.spawned",
    "session.completed",
    "session.error",
  ];
  handlers.forEach((t) => src.addEventListener(t, (ev) => handleEvent(t, JSON.parse(ev.data))));
}

function handleEvent(type, frame) {
  appendEventLog(type, frame.payload);
  const p = frame.payload || {};
  switch (type) {
    case "container.resolved":
      els.thinking.textContent +=
        `[container resolved → ${p.container_id}` +
        (p.available && p.available.length > 1
          ? ` (chose first of ${p.available.length}: ${p.available.join(", ")})`
          : "") +
        `]\n\n`;
      break;
    case "planner.thinking":
      els.thinking.textContent += p.delta || "";
      els.thinking.scrollTop = els.thinking.scrollHeight;
      break;
    case "planner.text":
      els.thinking.textContent += p.delta || "";
      break;
    case "plan_mode.question":
      renderQuestion(p);
      break;
    case "plan_mode.answered":
      markQuestionAnswered(p.question_id, p.answer);
      break;
    case "plan.created":
      renderPlan(p);
      break;
    case "plan.repaired":
      flashRepair(p);
      break;
    case "task.started":
      updateTaskStatus(p.task_id, "RUNNING");
      break;
    case "task.code_progress":
      updateTaskProgress(p.task_id, p.current, p.total, p.msg);
      break;
    case "task.completed":
      updateTaskStatus(p.task_id, "SUCCEEDED");
      attachArtifact(p);
      break;
    case "task.failed":
      updateTaskStatus(p.task_id, "FAILED", p.error);
      break;
    case "task.skipped":
      updateTaskStatus(p.task_id, "SKIPPED", p.reason);
      break;
    case "session.completed":
      showFinal(p);
      break;
    case "session.error":
      showError(p);
      break;
  }
}

function appendEventLog(type, payload) {
  const li = document.createElement("li");
  const t = document.createElement("span");
  t.className = "type";
  t.textContent = type;
  const p = document.createElement("span");
  p.className = "payload";
  p.textContent = payload ? JSON.stringify(payload) : "";
  li.appendChild(t);
  li.appendChild(p);
  els.events.appendChild(li);
  els.events.scrollTop = els.events.scrollHeight;
}

function renderQuestion(p) {
  els.clarifications.classList.remove("hidden");
  // Idempotency: if we already rendered this question (e.g. server retried
  // the interrogator after a transient failure), don't duplicate it.
  if (document.querySelector(`.question[data-qid="${p.question_id}"]`)) return;

  const wrap = document.createElement("div");
  wrap.className = "question";
  wrap.dataset.qid = p.question_id;
  const text = document.createElement("p");
  text.textContent = p.text;
  wrap.appendChild(text);

  const disableThisQuestion = () => {
    wrap.dataset.answered = "1";
    wrap.style.opacity = "0.5";
    wrap.querySelectorAll("button, input").forEach((el) => (el.disabled = true));
  };

  if (p.options && p.options.length > 0) {
    const ops = document.createElement("div");
    ops.className = "options";
    p.options.forEach((opt) => {
      const b = document.createElement("button");
      b.textContent = opt;
      b.addEventListener("click", () => {
        if (wrap.dataset.answered) return;
        disableThisQuestion();
        submitAnswer(p.question_id, opt);
      });
      ops.appendChild(b);
    });
    wrap.appendChild(ops);
  }
  const input = document.createElement("input");
  input.placeholder = "Or type a custom answer and press Enter";
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && input.value.trim() && !wrap.dataset.answered) {
      const v = input.value.trim();
      disableThisQuestion();
      submitAnswer(p.question_id, v);
    }
  });
  wrap.appendChild(input);
  els.questions.appendChild(wrap);
}

function markQuestionAnswered(qid, answer) {
  const node = document.querySelector(`.question[data-qid="${qid}"]`);
  if (node) {
    node.style.opacity = "0.5";
    node.querySelectorAll("button, input").forEach((el) => (el.disabled = true));
    const a = document.createElement("div");
    a.style.color = "var(--ok)";
    a.style.fontSize = "12px";
    a.textContent = "Answered: " + answer;
    node.appendChild(a);
  }
}

async function submitAnswer(question_id, answer) {
  await fetch(`/v1/sessions/${currentSession}/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question_id, answer }),
  });
}

function renderPlan(p) {
  els.planTree.classList.remove("empty");
  els.planTree.innerHTML = "";
  (p.tasks || []).forEach((t) => {
    const row = document.createElement("div");
    row.className = "task";
    row.dataset.id = t.id;
    row.innerHTML = `
      <header>
        <span>${t.id}</span>
        <span class="kind">${t.kind}</span>
        <span class="status PENDING">PENDING</span>
      </header>
      <div class="title">${t.title || ""}</div>
      ${t.depends_on && t.depends_on.length ? `<div class="deps">depends on: ${t.depends_on.join(", ")}</div>` : ""}
      <div class="progress"></div>
    `;
    els.planTree.appendChild(row);
    taskRows.set(t.id, row);
  });
}

function flashRepair(p) {
  const row = taskRows.get(p.task_id);
  if (!row) return;
  const note = document.createElement("div");
  note.style.color = "var(--warn)";
  note.style.fontSize = "11px";
  note.textContent = `⚠ auto-repaired: ${p.reason}`;
  row.appendChild(note);
}

function updateTaskStatus(taskId, status, detail) {
  const row = taskRows.get(taskId);
  if (!row) return;
  const badge = row.querySelector(".status");
  if (badge) {
    badge.className = "status " + status;
    badge.textContent = status;
  }
  if (detail) {
    const d = document.createElement("div");
    d.style.color = "var(--muted)";
    d.style.fontSize = "11px";
    d.textContent = detail;
    row.appendChild(d);
  }
}

function updateTaskProgress(taskId, current, total, msg) {
  const row = taskRows.get(taskId);
  if (!row) return;
  const cell = row.querySelector(".progress");
  if (!cell) return;
  cell.innerHTML = `<progress value="${current}" max="${total}"></progress> ${current}/${total} ${msg || ""}`;
}

function attachArtifact(p) {
  if (!p.artifact_ref) return;
  const a = document.createElement("div");
  a.innerHTML = `<small>\u{1F4CE} artifact (task ${p.task_id}): <code>${p.artifact_ref}</code></small>`;
  els.artifacts.appendChild(a);
}

function showFinal(p) {
  els.finalSection.classList.remove("hidden");
  els.finalAnswer.textContent = p.final_answer || "(no answer)";
  if (p.artifacts && p.artifacts.length) {
    p.artifacts.forEach((a) => {
      const d = document.createElement("div");
      d.innerHTML = `<small>\u{1F4CE} ${a.task_id}: <code>${a.ref}</code></small>`;
      els.artifacts.appendChild(d);
    });
  }
}

function showError(p) {
  els.finalSection.classList.remove("hidden");
  els.finalAnswer.style.color = "var(--err)";
  els.finalAnswer.textContent = "Error: " + (p.error || "unknown");
}
