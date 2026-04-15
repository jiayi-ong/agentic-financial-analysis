/**
 * Agentic Financial Analyst — Frontend Application
 *
 * Manages:
 *  - WebSocket connection lifecycle
 *  - Tab switching (Analysis / Reasoning Steps)
 *  - Real-time event routing to the correct UI handler
 *  - Markdown rendering via marked.js
 *  - Base64 figure rendering (matplotlib charts)
 *  - Auto-scroll to the latest message
 */

'use strict';

// ── DOM References ──────────────────────────────────────────────────────────
const tickerInput       = document.getElementById('tickerInput');
const queryInput        = document.getElementById('queryInput');
const startBtn          = document.getElementById('startBtn');
const tickerError       = document.getElementById('tickerError');
const connectionBadge   = document.getElementById('connectionBadge');
const statusBar         = document.getElementById('statusBar');
const statusText        = document.getElementById('statusText');
const messagesContainer = document.getElementById('messagesContainer');
const typingIndicator   = document.getElementById('typingIndicator');
const stepsContainer    = document.getElementById('stepsContainer');
const stepsEmpty        = document.getElementById('stepsEmpty');
const stepBadge         = document.getElementById('stepBadge');
const tabBtns           = document.querySelectorAll('.tab-btn');
const tabPanels         = document.querySelectorAll('.tab-panel');

// ── State ────────────────────────────────────────────────────────────────────
let ws          = null;
let sessionId   = null;
let stepCount   = 0;
let analysisRunning = false;

// ── Marked.js configuration ──────────────────────────────────────────────────
if (typeof marked !== 'undefined') {
  marked.setOptions({
    breaks: true,
    gfm: true,
  });
}

function renderMarkdown(text) {
  if (typeof marked !== 'undefined') {
    return marked.parse(text);
  }
  // Fallback: escape HTML and preserve line breaks
  return text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
             .replace(/\n/g,'<br>');
}

// ── Tab switching ────────────────────────────────────────────────────────────
tabBtns.forEach(btn => {
  btn.addEventListener('click', () => {
    const target = btn.dataset.tab;
    tabBtns.forEach(b => b.classList.toggle('active', b === btn));
    tabPanels.forEach(p => {
      p.classList.toggle('active',  p.id === `tab-${target}`);
      p.classList.toggle('hidden',  p.id !== `tab-${target}`);
    });
  });
});

// ── WebSocket ────────────────────────────────────────────────────────────────
function generateSessionId() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

function connect() {
  sessionId = generateSessionId();
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url   = `${proto}://${location.host}/ws/${sessionId}`;

  ws = new WebSocket(url);

  ws.onopen = () => {
    setConnectionStatus('connected', 'Connected');
    startBtn.disabled = false;
  };

  ws.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data);
      routeEvent(event);
    } catch (err) {
      console.error('Failed to parse event:', e.data, err);
    }
  };

  ws.onclose = () => {
    setConnectionStatus('disconnected', 'Disconnected — reconnecting…');
    startBtn.disabled = true;
    setAnalysisRunning(false);
    setTimeout(connect, 3000);
  };

  ws.onerror = (err) => {
    console.error('WebSocket error:', err);
    setConnectionStatus('error', 'Connection error');
  };
}

// ── Connection status ────────────────────────────────────────────────────────
function setConnectionStatus(state, label) {
  connectionBadge.className = `connection-badge ${state}`;
  connectionBadge.innerHTML = `<span class="dot"></span> ${label}`;
}

// ── Analysis lifecycle ────────────────────────────────────────────────────────
function setAnalysisRunning(running) {
  analysisRunning = running;
  tickerInput.disabled = running;
  queryInput.disabled  = running;
  startBtn.disabled    = running || !ws || ws.readyState !== WebSocket.OPEN;
  if (running) {
    typingIndicator.classList.remove('hidden');
    statusBar.classList.add('active');
  } else {
    typingIndicator.classList.add('hidden');
    statusBar.classList.remove('active');
  }
}

// ── Start button ──────────────────────────────────────────────────────────────
startBtn.addEventListener('click', () => {
  const ticker = tickerInput.value.trim().toUpperCase();
  const query  = queryInput.value.trim()
    || `Perform a comprehensive financial analysis of ${ticker}.`;

  if (!ticker) {
    showTickerError('Please enter a stock ticker or company name.');
    return;
  }
  hideTickerError();

  if (!ws || ws.readyState !== WebSocket.OPEN) {
    showTickerError('Not connected to server. Please wait and try again.');
    return;
  }

  // Clear previous analysis
  clearMessages();
  clearSteps();
  queryInput.style.height = '';  // reset textarea height
  setAnalysisRunning(true);
  setStatus('⏳', `Sending request for ${ticker}...`);

  // Add user bubble — show the full query text (including the generated default)
  addMessage('user', ticker + ' — ' + query);

  // Send to server
  ws.send(JSON.stringify({ ticker, query }));
});

// Enter on ticker input submits; Shift+Enter on query textarea inserts newline (default),
// plain Enter on query textarea also submits for convenience.
tickerInput.addEventListener('keydown', e => {
  if (e.key === 'Enter') startBtn.click();
});
queryInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    startBtn.click();
  }
});

// Auto-grow textarea as user types
queryInput.addEventListener('input', () => {
  queryInput.style.height = 'auto';
  queryInput.style.height = Math.min(queryInput.scrollHeight, 200) + 'px';
});

// ── Event router ──────────────────────────────────────────────────────────────
const EVENT_ICONS = {
  session_ready:    '🟢',
  validation_error: '❌',
  agent_start:      '🚀',
  agent_done:       '✅',
  tool_call:        '🔧',
  tool_result:      '📦',
  synthesis_start:  '🔗',
  synthesis_done:   '✅',
  critique_start:   '🔎',
  critique_done:    '🏷️',
  rerun_start:      '🔄',
  final_output:     '📊',
  abstain:          '⚠️',
  error:            '🚨',
};

function routeEvent(event) {
  const { event_type, agent_name, message, payload, timestamp } = event;

  // Always add to steps tab — pass full payload for rich rendering
  addStep(event_type, agent_name, message, timestamp, payload);

  switch (event_type) {
    case 'session_ready':
      setStatus('🟢', message);
      break;

    case 'validation_error':
      showTickerError(message);
      setAnalysisRunning(false);
      setStatus('❌', 'Invalid ticker entered.');
      break;

    case 'agent_start':
    case 'synthesis_start':
    case 'critique_start':
    case 'rerun_start':
      setStatus(EVENT_ICONS[event_type] || '⏳', message);
      break;

    case 'agent_done':
    case 'synthesis_done':
    case 'critique_done':
      setStatus(EVENT_ICONS[event_type] || '✅', message);
      break;

    case 'tool_call':
      setStatus('🔧', message);
      break;

    case 'final_output': {
      setAnalysisRunning(false);
      const secs = payload && payload.thinking_time_seconds != null
        ? payload.thinking_time_seconds : null;
      setStatus('✅', secs !== null ? `Analysis complete in ${formatDuration(secs)}.` : 'Analysis complete.');
      const narrative = (payload && payload.narrative) || message;
      addAgentMessage(narrative, payload);
      if (secs !== null) addTimingNote(secs);
      break;
    }

    case 'abstain': {
      setAnalysisRunning(false);
      setStatus('⚠️', 'Analysis could not be completed.');
      addAgentMessage(message, payload);
      const abstainSecs = payload && payload.thinking_time_seconds != null
        ? payload.thinking_time_seconds : null;
      if (abstainSecs !== null) addTimingNote(abstainSecs);
      break;
    }

    case 'error':
      setAnalysisRunning(false);
      setStatus('🚨', message);
      addAgentMessage(`**Error:** ${message}`, null);
      break;

    default:
      setStatus('ℹ️', message);
  }
}

// ── UI helpers ────────────────────────────────────────────────────────────────
function setStatus(icon, text) {
  document.querySelector('.status-icon').textContent = icon;
  statusText.textContent = text;
}

function showTickerError(msg) {
  tickerError.innerHTML = msg;
  tickerError.classList.remove('hidden');
}
function hideTickerError() {
  tickerError.classList.add('hidden');
}

function clearMessages() {
  messagesContainer.innerHTML = '';
}
function clearSteps() {
  stepsContainer.innerHTML = '';
  stepsEmpty.style.display = 'block';
  stepsContainer.appendChild(stepsEmpty);
  stepCount = 0;
  stepBadge.textContent = '0';
}

function addMessage(role, text) {
  const isAgent = role === 'agent';
  const div = document.createElement('div');
  div.className = `message ${isAgent ? 'agent-message' : 'user-message'}`;
  div.innerHTML = `
    <div class="message-avatar">${isAgent ? '🤖' : '👤'}</div>
    <div class="message-bubble">${isAgent ? renderMarkdown(text) : escapeHtml(text)}</div>
  `;
  messagesContainer.appendChild(div);
  scrollToBottom(messagesContainer);
}

function formatDuration(totalSeconds) {
  const mins = Math.floor(totalSeconds / 60);
  const secs = Math.round(totalSeconds % 60);
  return `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
}

function addTimingNote(seconds) {
  const div = document.createElement('div');
  div.className = 'timing-note';
  div.textContent = `\u23F1 Analysis completed in ${formatDuration(seconds)}`;
  messagesContainer.appendChild(div);
  scrollToBottom(messagesContainer);
}

function addAgentMessage(markdown, payload) {
  const div = document.createElement('div');
  div.className = 'message agent-message';

  let figuresHtml = '';
  if (payload && payload.figures && payload.figures.length > 0) {
    figuresHtml = payload.figures.map(b64 =>
      `<img class="chart-img" src="data:image/png;base64,${b64}" alt="Analysis chart" />`
    ).join('');
  }

  div.innerHTML = `
    <div class="message-avatar">🤖</div>
    <div class="message-bubble">
      ${renderMarkdown(markdown)}
      ${figuresHtml}
    </div>
  `;
  messagesContainer.appendChild(div);
  scrollToBottom(messagesContainer);
}

function addStep(event_type, agent_name, message, timestamp, payload) {
  // Remove empty placeholder
  if (stepsEmpty && stepsEmpty.parentNode) {
    stepsEmpty.parentNode.removeChild(stepsEmpty);
  }

  stepCount++;
  stepBadge.textContent = stepCount;

  const icon = EVENT_ICONS[event_type] || 'ℹ️';
  const time = timestamp ? new Date(timestamp).toLocaleTimeString() : '';

  // Build rich detail panel for specific event types
  let detailHtml = '';
  if (event_type === 'agent_done' && payload && payload.claims) {
    detailHtml = buildSpecialistDetail(payload);
  } else if (event_type === 'synthesis_done' && payload) {
    detailHtml = buildSynthesisDetail(payload);
  } else if (event_type === 'critique_done' && payload && payload.critique) {
    detailHtml = buildCritiqueDetail(payload.critique);
  } else if (event_type === 'tool_call' && payload && payload.tool_name) {
    detailHtml = buildToolCallDetail(payload);
  }

  const item = document.createElement('div');
  item.className = `step-item step-${event_type}`;
  item.innerHTML = `
    <div class="step-icon">${icon}</div>
    <div class="step-body">
      <div class="step-meta">
        ${agent_name ? `<span class="step-agent-tag">${escapeHtml(agent_name)}</span>` : ''}
        ${time ? `<span class="step-time">${time}</span>` : ''}
      </div>
      <div class="step-message">${escapeHtml(message)}</div>
      ${detailHtml ? `<details class="step-detail"><summary>View details</summary><div class="step-detail-content">${detailHtml}</div></details>` : ''}
    </div>
  `;
  stepsContainer.appendChild(item);
  scrollToBottom(stepsContainer);
}

// ── Rich detail builders ──────────────────────────────────────────────────────

/** Returns a safe http/https URL or null. */
function safeUrl(url) {
  if (!url || typeof url !== 'string') return null;
  return (url.startsWith('http://') || url.startsWith('https://')) ? url : null;
}

/** Returns a short human-readable label for a URL (hostname only). */
function urlLabel(url) {
  try {
    const hostname = new URL(url).hostname.replace(/^www\./, '');
    return hostname + ' \u2197'; // ↗ external-link symbol
  } catch {
    return url.length > 50 ? url.slice(0, 50) + '\u2026' : url;
  }
}

function buildSpecialistDetail(payload) {
  const pct = payload.confidence !== undefined ? Math.round(payload.confidence * 100) : 0;
  const confColor = pct >= 70 ? 'var(--success)' : pct >= 40 ? 'var(--warning)' : 'var(--danger)';

  let html = '';

  // Confidence
  html += `<div class="step-dl-row">
    <span class="step-dl-label">Confidence</span>
    <div class="step-conf-bar"><div class="step-conf-fill" style="width:${pct}%;background:${confColor}"></div></div>
    <span class="step-conf-pct" style="color:${confColor}">${pct}%</span>
  </div>`;

  // Claims
  if (payload.claims && payload.claims.length > 0) {
    html += `<div class="step-dl-label">Key Claims</div><ul class="step-claims">`;
    payload.claims.forEach(c => { html += `<li>${escapeHtml(c)}</li>`; });
    html += `</ul>`;
  }

  // Evidence
  if (payload.evidence && payload.evidence.length > 0) {
    html += `<div class="step-dl-label">Evidence &amp; Sources</div>`;
    payload.evidence.forEach(ev => {
      const link = safeUrl(ev.source_url);
      const filingId = ev.filing_identifier;
      html += `<div class="step-evidence-item">
        <span class="step-source-badge">${escapeHtml(ev.source_type || '')}</span>
        <span class="step-evidence-text">${escapeHtml((ev.text || '').slice(0, 220))}${(ev.text || '').length > 220 ? '…' : ''}</span>
        ${link ? `<div><a class="step-source-link" href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(urlLabel(link))}</a></div>` : ''}
        ${!link && filingId ? `<div class="step-filing-id">${escapeHtml(filingId)}</div>` : ''}
      </div>`;
    });
  }

  // Failure reason
  if (!payload.success && payload.failure_reason) {
    html += `<div class="step-failure"><span class="step-dl-label">Failure Reason</span> ${escapeHtml(payload.failure_reason)}</div>`;
  }

  // Tool Calls
  if (payload.tool_history && payload.tool_history.length > 0) {
    html += `<div class="step-dl-label">Tool Calls (${payload.tool_history.length})</div>`;
    payload.tool_history.forEach(entry => {
      const toolName = entry.name || 'unknown';
      const isExecPy = toolName === 'execute_python';
      html += `<div class="step-tool-call-item">`;
      html += `<div class="step-tool-call-header">`;
      html += `<span class="step-source-badge">${escapeHtml(toolName)}</span>`;
      if (!isExecPy && entry.args) {
        const argStr = JSON.stringify(entry.args);
        html += `<span class="step-tool-args">${escapeHtml(argStr.length > 120 ? argStr.slice(0, 120) + '\u2026' : argStr)}</span>`;
      }
      html += `</div>`;
      if (isExecPy && entry.args && entry.args.code) {
        html += `<pre class="step-code-block"><code>${escapeHtml(entry.args.code)}</code></pre>`;
        const result = entry.result;
        const stdout = result && (result.stdout || (result.data && result.data.stdout));
        const stderr = result && (result.error || (result.data && result.data.error));
        if (stdout) {
          html += `<pre class="step-code-output">${escapeHtml(stdout.slice(0, 600))}${stdout.length > 600 ? '\n\u2026' : ''}</pre>`;
        }
        if (stderr) {
          html += `<div class="step-tool-error">${escapeHtml(stderr.slice(0, 300))}</div>`;
        }
      }
      html += `</div>`;
    });
  }

  return html;
}

function buildSynthesisDetail(payload) {
  let html = '';

  if (payload.key_hypothesis) {
    html += `<div class="step-dl-label">Key Hypothesis</div>
    <div class="step-hypothesis">${escapeHtml(payload.key_hypothesis)}</div>`;
  }

  if (payload.sources && payload.sources.length > 0) {
    const shown = payload.sources.slice(0, 10);
    const rest  = payload.sources.length - shown.length;
    html += `<div class="step-dl-label">Sources (${payload.sources.length})</div><ul class="step-sources">`;
    shown.forEach(src => {
      const link = safeUrl(src);
      html += `<li>${link ? `<a class="step-source-link" href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(urlLabel(link))}</a>` : escapeHtml(src)}</li>`;
    });
    if (rest > 0) html += `<li class="step-more">…and ${rest} more</li>`;
    html += `</ul>`;
  }

  if (payload.omitted_specialists && payload.omitted_specialists.length > 0) {
    html += `<div class="step-failure"><span class="step-dl-label">Omitted Specialists</span> ${escapeHtml(payload.omitted_specialists.join(', '))}</div>`;
  }

  return html;
}

function buildCritiqueDetail(critique) {
  if (!critique) return '';

  const sevColor = { none: 'var(--success)', low: 'var(--text-secondary)', medium: 'var(--warning)', high: 'var(--danger)' };
  const sev = critique.overall_severity || 'none';

  let html = `<div class="step-dl-row">
    <span class="step-dl-label">Overall Severity</span>
    <span class="step-sev-badge" style="color:${sevColor[sev] || 'inherit'}">${sev.toUpperCase()}</span>
  </div>`;

  if (critique.critique_summary) {
    html += `<div class="step-critique-summary">${escapeHtml(critique.critique_summary)}</div>`;
  }

  if (critique.issues && critique.issues.length > 0) {
    html += `<div class="step-dl-label">Issues (${critique.issues.length})</div>`;
    critique.issues.forEach(issue => {
      const isevColor = sevColor[issue.severity] || 'inherit';
      html += `<div class="step-issue-item">
        <div class="step-issue-header">
          <span class="step-sev-badge" style="color:${isevColor}">${(issue.severity || '').toUpperCase()}</span>
          <span class="step-issue-tag">${escapeHtml(issue.tag || '')}</span>
          <span class="step-issue-agent">${escapeHtml(issue.affected_specialist || '')}</span>
        </div>
        <div class="step-issue-quote">"${escapeHtml((issue.quote || '').slice(0, 180))}${(issue.quote || '').length > 180 ? '…' : ''}"</div>
        ${issue.suggestion ? `<div class="step-issue-suggestion">${escapeHtml(issue.suggestion)}</div>` : ''}
      </div>`;
    });
  }

  return html;
}

function buildToolCallDetail(payload) {
  const toolName = payload.tool_name || '';
  const args = payload.args || {};
  const isExecPy = toolName === 'execute_python';
  const isCrawl  = toolName === 'crawl_news';

  let html = `<div class="step-tool-call-item"><div class="step-tool-call-header"><span class="step-source-badge">${escapeHtml(toolName)}</span></div>`;

  if (isExecPy && args.code) {
    // Show the code snippet
    html += `<pre class="step-code-block"><code>${escapeHtml(args.code)}</code></pre>`;
  } else if (isCrawl) {
    // Show query and sources list
    if (args.query) {
      html += `<div class="step-tool-arg-row"><span class="step-tool-arg-key">query</span><span class="step-tool-arg-val">${escapeHtml(String(args.query))}</span></div>`;
    }
    if (args.sources && Array.isArray(args.sources) && args.sources.length > 0) {
      html += `<div class="step-tool-arg-row"><span class="step-tool-arg-key">sources</span><span class="step-tool-arg-val">${escapeHtml(args.sources.join(', '))}</span></div>`;
    }
    if (args.max_articles != null) {
      html += `<div class="step-tool-arg-row"><span class="step-tool-arg-key">max_articles</span><span class="step-tool-arg-val">${escapeHtml(String(args.max_articles))}</span></div>`;
    }
  } else if (Object.keys(args).length > 0) {
    // Generic: show all args as key: value rows
    for (const [k, v] of Object.entries(args)) {
      const valStr = typeof v === 'string' ? v : JSON.stringify(v);
      html += `<div class="step-tool-arg-row"><span class="step-tool-arg-key">${escapeHtml(k)}</span><span class="step-tool-arg-val">${escapeHtml(valStr.length > 200 ? valStr.slice(0, 200) + '\u2026' : valStr)}</span></div>`;
    }
  }

  html += `</div>`;
  return html;
}

function scrollToBottom(el) {
  // If the element is a constrained scroll container, scroll it directly.
  // Otherwise fall back to scrolling the last child into view (e.g. steps tab).
  if (el.scrollHeight > el.clientHeight && getComputedStyle(el).overflowY !== 'visible') {
    el.scrollTop = el.scrollHeight;
  } else {
    el.lastElementChild?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Init ─────────────────────────────────────────────────────────────────────
connect();
