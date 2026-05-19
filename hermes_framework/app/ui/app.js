/* ──────────────────────────────────────────────────────────────────────
 * Hermes UI controller.
 *
 *   • Subscribes to /v1/sessions/{id}/events (SSE) for live state.
 *   • Renders the streaming planner thought stream, plan tree, task progress,
 *     clarifying questions, and the final markdown answer.
 *   • Renders artifacts as clickable absolute URLs.
 *   • If SSE drops, falls back to polling /v1/sessions/{id}/status until
 *     the session reaches a terminal state, then renders the final answer
 *     from the polled snapshot.
 *
 * Markdown rendering is intentionally minimal — supports the subset the
 * synthesizer produces (headings, bold/italic, code, lists, blockquotes,
 * tables, autolinked URLs, horizontal rules). No external runtime deps.
 * ────────────────────────────────────────────────────────────────────── */

'use strict';

// ────────────────────────── DOM refs ──────────────────────────
const $ = (id) => document.getElementById(id);
const els = {
  containerId:   $('container-id'),
  webhookUrl:    $('webhook-url'),
  userMsg:       $('user-msg'),
  send:          $('send'),
  thinking:      $('thinking-stream'),
  planTree:      $('plan-tree'),
  planMeta:      $('plan-meta'),
  clarifications:$('clarifications'),
  questions:     $('questions'),
  finalSection:  $('final'),
  finalAnswer:   $('final-answer'),
  artifacts:     $('artifacts'),
  sessionId:     $('session-id'),
  statusPill:    $('status-pill'),
  progressLabel: $('progress-label'),
  progressFill:  $('progress-fill'),
  eventList:     $('event-list'),
  eventCount:    $('event-count'),
  eventDrawer:   $('event-drawer'),
  themeToggle:   $('theme-toggle'),
};

// ────────────────────────── State ──────────────────────────
const state = {
  sessionId: null,
  source: null,          // EventSource
  pollTimer: null,       // setInterval handle for polling fallback
  taskRows: new Map(),   // task_id → element
  taskState: new Map(),  // task_id → {status, current, total}
  eventCount: 0,
  terminal: false,
  // Highest SSE event id we've processed for the current session.
  // SSE clients can replay on reconnect (browsers do this automatically),
  // which would otherwise cause us to render the same "Answered:" line
  // twice, double-count tasks, etc. Tracking the max id and skipping
  // anything ≤ it makes every event handler effectively idempotent.
  lastEventId: 0,
  // Streaming-phase indicator — toggles the .streaming class on the
  // planner terminal panel for the subtle glow effect.
  isThinking: false,
};

// ────────────────────────── Theme ──────────────────────────
(function initTheme() {
  const saved = localStorage.getItem('hermes-theme');
  if (saved) document.body.dataset.theme = saved;
  els.themeToggle.addEventListener('click', () => {
    const next = document.body.dataset.theme === 'dark' ? 'light' : 'dark';
    document.body.dataset.theme = next;
    localStorage.setItem('hermes-theme', next);
  });
})();

// ────────────────────────── Compose hooks ──────────────────────────
document.querySelectorAll('.chip').forEach((btn) =>
  btn.addEventListener('click', () => {
    els.userMsg.value = btn.dataset.q;
    els.userMsg.focus();
  })
);
els.send.addEventListener('click', sendMessage);
els.userMsg.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') sendMessage();
});

async function sendMessage() {
  const message = els.userMsg.value.trim();
  if (!message) return;
  const containerId = els.containerId.value.trim();
  const webhookUrl  = els.webhookUrl.value.trim();
  resetUi();
  setStatus('planning');

  els.send.disabled = true;
  try {
    const created = await fetch('/v1/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        container_id: containerId || undefined,
        webhook_url:  webhookUrl  || undefined,
      }),
    }).then((r) => r.json());

    state.sessionId = created.session_id;
    els.sessionId.textContent = state.sessionId;
    openStream(state.sessionId);

    await fetch(`/v1/sessions/${state.sessionId}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, container_id: containerId || undefined }),
    });
  } finally {
    els.send.disabled = false;
  }
}

function resetUi() {
  state.taskRows.clear();
  state.taskState.clear();
  state.eventCount = 0;
  state.terminal = false;
  state.lastEventId = 0;
  state.isThinking = false;
  els.thinking.textContent = '';
  els.thinking.classList.remove('streaming');
  els.planTree.className = 'empty';
  els.planTree.textContent = 'No plan yet — send a request to start.';
  els.planMeta.textContent = '';
  els.clarifications.classList.add('hidden');
  els.questions.innerHTML = '';
  els.finalSection.classList.add('hidden');
  els.finalAnswer.innerHTML = '';
  els.artifacts.innerHTML = '';
  els.eventList.innerHTML = '';
  updateEventCount();
  setProgress(0);
  if (state.source) { state.source.close(); state.source = null; }
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

// ────────────────────────── SSE wiring ──────────────────────────
const EVENT_TYPES = [
  'session.started', 'session.resumed',
  'container.resolved',
  'planner.thinking', 'planner.text',
  'plan_mode.question', 'plan_mode.answered',
  'plan.created', 'plan.repaired', 'plan.replanning',
  'task.started', 'task.code_generated', 'task.code_executing',
  'task.code_progress', 'task.code_stdout', 'task.code_stderr',
  'task.mcp_call', 'task.mcp_result',
  'task.filter_summary', 'task.execution_plan',
  'task.checkpoint',
  'task.completed', 'task.retrying', 'task.failed', 'task.skipped',
  'subagent.spawned',
  'session.completed', 'session.error',
];

function openStream(sessionId) {
  const src = new EventSource(`/v1/sessions/${sessionId}/events`);
  state.source = src;
  EVENT_TYPES.forEach((t) =>
    src.addEventListener(t, (ev) => {
      // Dedupe replayed events. The server sets `id:` on every SSE frame
      // (per-session monotonic). On reconnect, the browser may receive
      // events we already processed; skipping by id makes handlers
      // effectively idempotent and prevents duplicate UI artifacts
      // (e.g. two "Answered: X" lines for the same question).
      const id = parseInt(ev.lastEventId || '0', 10);
      if (id && id <= state.lastEventId) return;
      if (id) state.lastEventId = id;
      let frame; try { frame = JSON.parse(ev.data); } catch { return; }
      handleEvent(t, frame);
    })
  );
  src.onerror = () => {
    // SSE dropped (proxy disconnect, network glitch). Fall back to polling
    // every 5s until the session reaches a terminal state.
    if (state.terminal || !state.sessionId) return;
    startPollingFallback(state.sessionId);
  };
}

function handleEvent(type, frame) {
  const p = frame.payload || {};
  appendEventLog(frame.ts || '', type, p);
  switch (type) {
    case 'session.started':
      setStatus('planning');
      break;
    case 'session.resumed':
      appendThinking(`[session resumed — ${p.interrupted_tasks ?? 0} interrupted task(s) restarted]\n\n`);
      break;
    case 'container.resolved':
      appendThinking(`[container resolved → ${p.container_id}` +
        (p.available && p.available.length > 1
          ? ` (chose first of ${p.available.length}: ${p.available.join(', ')})`
          : '') + ']\n\n');
      break;
    case 'planner.thinking':
    case 'planner.text':
      if (!state.isThinking) {
        state.isThinking = true;
        els.thinking.classList.add('streaming');
      }
      appendThinking(p.delta || '');
      break;
    case 'plan_mode.question':
      renderQuestion(p);
      setStatus('awaiting');
      break;
    case 'plan_mode.answered':
      markQuestionAnswered(p.question_id, p.answer);
      break;
    case 'plan.created':
      state.isThinking = false;
      els.thinking.classList.remove('streaming');
      renderPlan(p);
      setStatus('executing');
      break;
    case 'plan.repaired':
      flashTaskFlag(p.task_id, `auto-repaired: ${p.reason || ''}`);
      break;
    case 'plan.replanning':
      appendThinking(`\n[replanning — task ${p.failed_task} failed: ${p.error}]\n`);
      break;
    case 'task.started':
      setTaskStatus(p.task_id, 'RUNNING');
      break;
    case 'task.code_progress':
      setTaskProgress(p.task_id, p.current, p.total, p.msg);
      bumpOverallProgress();
      break;
    case 'task.checkpoint':
      setTaskDetail(p.task_id, `checkpoint saved (${prettyCheckpoint(p.checkpoint)})`);
      break;
    case 'task.completed':
      setTaskStatus(p.task_id, 'SUCCEEDED');
      bumpOverallProgress();
      break;
    case 'task.failed':
      setTaskStatus(p.task_id, 'FAILED', p.error);
      bumpOverallProgress();
      break;
    case 'task.skipped':
      setTaskStatus(p.task_id, 'SKIPPED', p.reason);
      bumpOverallProgress();
      break;
    case 'task.retrying':
      setTaskDetail(p.task_id, `retrying (attempt ${p.attempt}): ${p.error}`);
      break;
    case 'session.completed':
      state.terminal = true;
      showFinal(p);
      setStatus('succeeded');
      setProgress(100);
      break;
    case 'session.error':
      state.terminal = true;
      showError(p);
      setStatus('failed');
      break;
  }
}

function startPollingFallback(sid) {
  if (state.pollTimer) return;
  appendThinking(`\n[event stream dropped — switching to status polling every 5s]\n`);
  state.pollTimer = setInterval(async () => {
    try {
      const snap = await fetch(`/v1/sessions/${sid}/status`).then((r) => r.json());
      setProgress(snap.progress_percentage);
      if (snap.status === 'SUCCEEDED' || snap.status === 'FAILED') {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        state.terminal = true;
        if (snap.status === 'SUCCEEDED') {
          showFinal({
            final_answer: snap.final_answer,
            artifacts: (snap.tasks || [])
              .filter((t) => t.artifact_ref)
              .map((t) => ({ task_id: t.id, ref: t.artifact_ref })),
          });
          setStatus('succeeded');
        } else {
          showError({ error: snap.final_answer || 'session failed' });
          setStatus('failed');
        }
      }
    } catch {/* swallow — try again next tick */}
  }, 5000);
}

// ────────────────────────── Status + progress ──────────────────────────
function setStatus(state_) {
  els.statusPill.dataset.state = state_;
  els.statusPill.textContent = state_;
}
function setProgress(pct) {
  const v = Math.max(0, Math.min(100, Math.round(pct)));
  els.progressLabel.textContent = v + '%';
  els.progressFill.style.width = v + '%';
}
function bumpOverallProgress() {
  const tasks = Array.from(state.taskState.values());
  if (tasks.length === 0) return;
  const done = tasks.filter((t) =>
    ['SUCCEEDED', 'FAILED', 'SKIPPED'].includes(t.status)
  ).length;
  setProgress(100 * done / tasks.length);
}

// ────────────────────────── Plan rendering ──────────────────────────
function renderPlan(p) {
  els.planTree.className = '';
  els.planTree.innerHTML = '';
  els.planMeta.textContent = `${(p.tasks || []).length} tasks`;
  (p.tasks || []).forEach((t) => {
    const card = document.createElement('div');
    card.className = 'task-card';
    card.dataset.id = t.id;
    card.dataset.status = 'PENDING';
    card.innerHTML = `
      <div class="task-head">
        <span class="task-id"></span>
        <span class="task-kind"></span>
        <span class="task-status" data-status="PENDING">PENDING</span>
      </div>
      <div class="task-title"></div>
    `;
    card.querySelector('.task-id').textContent = t.id;
    card.querySelector('.task-kind').textContent = t.kind;
    card.querySelector('.task-title').textContent = t.title || '';
    if (t.depends_on && t.depends_on.length) {
      const deps = document.createElement('div');
      deps.className = 'task-deps';
      deps.textContent = 'depends on: ' + t.depends_on.join(', ');
      card.appendChild(deps);
    }
    const prog = document.createElement('div');
    prog.className = 'task-progress hidden';
    prog.innerHTML = `<progress value="0" max="1"></progress><span class="task-progress-text"></span>`;
    card.appendChild(prog);
    els.planTree.appendChild(card);
    state.taskRows.set(t.id, card);
    state.taskState.set(t.id, { status: 'PENDING', current: 0, total: 0 });
  });
}

function setTaskStatus(id, status, detail) {
  const card = state.taskRows.get(id);
  if (!card) return;
  card.dataset.status = status;
  const badge = card.querySelector('.task-status');
  if (badge) { badge.dataset.status = status; badge.textContent = status; }
  if (detail) appendTaskDetail(card, detail, status === 'FAILED');
  const s = state.taskState.get(id);
  if (s) { s.status = status; }
}

function setTaskProgress(id, current, total, msg) {
  const card = state.taskRows.get(id);
  if (!card) return;
  const prog = card.querySelector('.task-progress');
  if (!prog) return;
  prog.classList.remove('hidden');
  prog.querySelector('progress').value = current;
  prog.querySelector('progress').max   = total || 1;
  prog.querySelector('.task-progress-text').textContent =
    `${current}/${total}${msg ? ' — ' + msg : ''}`;
  const s = state.taskState.get(id);
  if (s) { s.current = current; s.total = total; }
}

function setTaskDetail(id, msg) {
  const card = state.taskRows.get(id);
  if (card) appendTaskDetail(card, msg, false);
}

function appendTaskDetail(card, text, isError) {
  const d = document.createElement('div');
  d.className = 'task-detail' + (isError ? ' error' : '');
  d.textContent = text;
  card.appendChild(d);
}

function flashTaskFlag(id, msg) {
  const card = state.taskRows.get(id);
  if (!card) return;
  const f = document.createElement('div');
  f.className = 'task-flag';
  f.textContent = '⚠ ' + msg;
  card.appendChild(f);
}

function prettyCheckpoint(cp) {
  if (!cp || typeof cp !== 'object') return '';
  const parts = [];
  for (const k of ['page', 'offset', 'processed']) {
    if (k in cp) parts.push(`${k}=${cp[k]}`);
  }
  return parts.join(', ') || cp.msg || '';
}

// ────────────────────────── Clarifications ──────────────────────────
function renderQuestion(p) {
  els.clarifications.classList.remove('hidden');
  // The renderQuestion path can be reached more than once for the same
  // question on SSE replay; skip if already rendered.
  if (document.querySelector(`.question[data-qid="${p.question_id}"]`)) return;

  const wrap = document.createElement('div');
  wrap.className = 'question';
  wrap.dataset.qid = p.question_id;

  const text = document.createElement('p');
  text.textContent = p.text;
  wrap.appendChild(text);

  const input = document.createElement('input');
  input.placeholder = 'Or type a custom answer and press Enter';

  // Centralised submit. The .answered flag is set SYNCHRONOUSLY before any
  // async work — even if the user rapidly clicks a button and presses
  // Enter in the same browser tick, only the first call gets through.
  // Also disables inputs immediately and shows the chosen value, so the
  // UI never looks "answerable" after the first selection.
  const submit = (answer) => {
    if (wrap.dataset.answered) return;
    wrap.dataset.answered = '1';
    wrap.querySelectorAll('button, input').forEach((el) => (el.disabled = true));
    input.value = answer;             // confirm the choice in the field
    input.placeholder = '';
    submitAnswer(p.question_id, answer);
  };

  if (p.options && p.options.length) {
    const ops = document.createElement('div');
    ops.className = 'options';
    p.options.forEach((opt) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = opt;
      b.addEventListener('click', () => submit(opt));
      ops.appendChild(b);
    });
    wrap.appendChild(ops);
  }

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && input.value.trim()) {
      e.preventDefault();             // stop accidental form-submit + double-fire
      submit(input.value.trim());
    }
  });
  wrap.appendChild(input);

  els.questions.appendChild(wrap);
}

function markQuestionAnswered(qid, answer) {
  const node = document.querySelector(`.question[data-qid="${qid}"]`);
  if (!node) return;
  node.querySelectorAll('button, input').forEach((el) => (el.disabled = true));

  // Dedupe: only attach ONE "Answered:" indicator per question, even if the
  // server emits the answered event more than once (e.g. SSE replay on
  // reconnect, or a legitimate retry). Update the existing one in place
  // rather than appending a second.
  let existing = node.querySelector('.answered');
  if (!existing) {
    existing = document.createElement('div');
    existing.className = 'answered';
    node.appendChild(existing);
  }
  existing.textContent = '✓ Answered: ' + answer;
  existing.dataset.answer = answer;
}

async function submitAnswer(question_id, answer) {
  await fetch(`/v1/sessions/${state.sessionId}/answer`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question_id, answer }),
  });
}

// ────────────────────────── Final answer + artifacts ──────────────────────────
function showFinal(p) {
  els.finalSection.classList.remove('hidden');
  els.finalAnswer.innerHTML = renderMarkdown(p.final_answer || '_(no answer)_');
  els.artifacts.innerHTML = '';
  (p.artifacts || []).forEach((a) => {
    const link = document.createElement('a');
    link.className = 'artifact-link';
    link.href = a.url || a.ref || '#';
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.innerHTML = `
      <span class="artifact-id"></span>
      <span class="artifact-url"></span>
    `;
    link.querySelector('.artifact-id').textContent = a.task_id;
    link.querySelector('.artifact-url').textContent = a.url || a.ref;
    els.artifacts.appendChild(link);
  });
  els.finalSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function showError(p) {
  els.finalSection.classList.remove('hidden');
  els.finalAnswer.innerHTML = renderMarkdown('**Error:** ' + (p.error || 'unknown'));
}

// ────────────────────────── Thinking stream ──────────────────────────
function appendThinking(text) {
  if (!text) return;
  els.thinking.textContent += text;
  els.thinking.scrollTop = els.thinking.scrollHeight;
}

// ────────────────────────── Event drawer ──────────────────────────
function appendEventLog(ts, type, payload) {
  const li = document.createElement('li');
  li.innerHTML = `<span class="ev-ts"></span><span class="ev-type"></span><span class="ev-body"></span>`;
  li.querySelector('.ev-ts').textContent = formatTs(ts);
  li.querySelector('.ev-type').textContent = type;
  li.querySelector('.ev-body').textContent = oneLine(payload);
  els.eventList.appendChild(li);
  els.eventList.scrollTop = els.eventList.scrollHeight;
  state.eventCount += 1;
  updateEventCount();
}
function updateEventCount() {
  els.eventCount.textContent = `${state.eventCount} events`;
}
function formatTs(iso) {
  if (!iso) return '';
  const t = iso.length >= 19 ? iso.slice(11, 19) : iso;
  return t;
}
function oneLine(obj) {
  try {
    const s = JSON.stringify(obj);
    return s.length > 160 ? s.slice(0, 157) + '…' : s;
  } catch { return ''; }
}

/* ──────────────────────────────────────────────────────────────────────
 * Minimal markdown renderer.
 *
 * Supports the subset the synthesizer is instructed to emit:
 *   • # / ## / ### headings
 *   • **bold**, *italic*, `inline code`
 *   • Fenced code blocks ```
 *   • - or * unordered lists, 1. ordered lists
 *   • > blockquotes
 *   • --- horizontal rules
 *   • | a | b | tables
 *   • Bare http(s):// URLs → clickable <a>
 *
 * Block parser is line-based; inline rendering happens per-line. Avoids
 * pulling in a third-party markdown library (zero external runtime deps).
 * ────────────────────────────────────────────────────────────────────── */
function renderMarkdown(src) {
  const lines = src.replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    // Fenced code block
    if (/^```/.test(line)) {
      const lang = line.slice(3).trim();
      const code = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) { code.push(lines[i]); i += 1; }
      i += 1;
      out.push(`<pre><code data-lang="${escapeHtml(lang)}">${escapeHtml(code.join('\n'))}</code></pre>`);
      continue;
    }

    // Horizontal rule
    if (/^---+\s*$/.test(line)) { out.push('<hr/>'); i += 1; continue; }

    // Heading
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); i += 1; continue; }

    // Blockquote
    if (/^>\s?/.test(line)) {
      const block = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        block.push(lines[i].replace(/^>\s?/, ''));
        i += 1;
      }
      out.push(`<blockquote>${inline(block.join(' '))}</blockquote>`);
      continue;
    }

    // Table: header line is | x | y |, separator is | --- | --- |
    if (/^\|.*\|$/.test(line) && i + 1 < lines.length && /^\|[\s:\-|]+\|$/.test(lines[i + 1])) {
      const header = splitRow(line);
      const align  = splitRow(lines[i + 1]).map(alignFromSep);
      const rows = [];
      i += 2;
      while (i < lines.length && /^\|.*\|$/.test(lines[i])) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      out.push(renderTable(header, align, rows));
      continue;
    }

    // Unordered list
    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ''));
        i += 1;
      }
      out.push('<ul>' + items.map((it) => `<li>${inline(it)}</li>`).join('') + '</ul>');
      continue;
    }

    // Ordered list
    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ''));
        i += 1;
      }
      out.push('<ol>' + items.map((it) => `<li>${inline(it)}</li>`).join('') + '</ol>');
      continue;
    }

    // Blank line: paragraph break
    if (!line.trim()) { i += 1; continue; }

    // Paragraph (collect until blank or block start)
    const para = [line];
    i += 1;
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#{1,4}\s|```|>\s?|\s*[-*]\s+|\s*\d+\.\s+|\|.*\|$|---+\s*$)/.test(lines[i])
    ) {
      para.push(lines[i]);
      i += 1;
    }
    out.push(`<p>${inline(para.join(' '))}</p>`);
  }
  return out.join('\n');
}

function splitRow(line) {
  // Drop leading/trailing | then split. Handle inline pipes via simple split —
  // tables in the synthesizer prompt are simple.
  const inner = line.trim().replace(/^\||\|$/g, '');
  return inner.split('|').map((c) => c.trim());
}
function alignFromSep(cell) {
  const left = cell.startsWith(':');
  const right = cell.endsWith(':');
  if (left && right) return 'center';
  if (right) return 'right';
  if (left) return 'left';
  return null;
}
function renderTable(header, align, rows) {
  const numericCols = header.map((_, c) => rows.every((r) => isNumericCell(r[c] || '')));
  const styleFor = (c) => {
    if (align[c]) return ` style="text-align:${align[c]}"`;
    if (numericCols[c]) return ' class="num"';
    return '';
  };
  const ths = header.map((h, c) => `<th${styleFor(c)}>${inline(h)}</th>`).join('');
  const trs = rows.map((r) =>
    '<tr>' + r.map((cell, c) => `<td${styleFor(c)}>${inline(cell || '')}</td>`).join('') + '</tr>'
  ).join('');
  return `<table><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`;
}
function isNumericCell(s) {
  return /^-?[\d,]+(\.\d+)?\s*%?$/.test(s.trim());
}

function inline(text) {
  // Escape first, then re-introduce the safe markdown constructs.
  let s = escapeHtml(text);
  // `code`
  s = s.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
  // **bold**
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  // *italic*
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  // [text](url)
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, t, u) =>
    `<a href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`
  );
  // Bare URLs (skip if inside an <a> already by matching only outside tags).
  s = s.replace(/(^|[\s(])((?:https?:\/\/)[^\s<)]+)/g, (_m, pre, u) =>
    `${pre}<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`
  );
  return s;
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
