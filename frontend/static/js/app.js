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
  setAnalysisRunning(true);
  setStatus('⏳', `Sending request for ${ticker}...`);

  // Add user bubble
  addMessage('user', ticker + (queryInput.value.trim() ? ` — ${queryInput.value.trim()}` : ''));

  // Send to server
  ws.send(JSON.stringify({ ticker, query }));
});

// Enter key support
tickerInput.addEventListener('keydown', e => {
  if (e.key === 'Enter') startBtn.click();
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

  // Always add to steps tab
  addStep(event_type, agent_name, message, timestamp);

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
      setStatus('✅', 'Analysis complete.');
      const narrative = (payload && payload.narrative) || message;
      addAgentMessage(narrative, payload);
      break;
    }

    case 'abstain':
      setAnalysisRunning(false);
      setStatus('⚠️', 'Analysis could not be completed.');
      addAgentMessage(message, payload);
      break;

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

function addStep(event_type, agent_name, message, timestamp) {
  // Remove empty placeholder
  if (stepsEmpty && stepsEmpty.parentNode) {
    stepsEmpty.parentNode.removeChild(stepsEmpty);
  }

  stepCount++;
  stepBadge.textContent = stepCount;

  const icon = EVENT_ICONS[event_type] || 'ℹ️';
  const time = timestamp ? new Date(timestamp).toLocaleTimeString() : '';

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
    </div>
  `;
  stepsContainer.appendChild(item);
  scrollToBottom(stepsContainer);
}

function scrollToBottom(el) {
  el.scrollTop = el.scrollHeight;
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
