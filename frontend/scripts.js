/**
 * Agent Shell — Frontend Scripts
 *
 * WebSocket 消息协议（与 backend/server/websocket_manager.py 约定）：
 *
 * 发送：
 *   { type: "run",  task: "<任务>", kwargs: {} }
 *   { type: "ping" }
 *
 * 接收：
 *   { type: "log",    content: "<日志文本>", metadata: {...} }
 *   { type: "stream", content: "<文本增量>" }
 *   { type: "result", content: "<最终结果>" }
 *   { type: "status", content: "running|done|error", metadata: {...} }
 *   { type: "error",  content: "<错误信息>" }
 *   { type: "pong" }
 *
 * 多轮历史：同一 WebSocket 连接 = 后端一个 session（id(websocket)）。每轮在日志区/结果区
 * 各追加一个轮次块，历史在 session 内累积；仅在新连接（含自动重连，= 新 session）或手动清空时清。
 */

const WS_URL = `ws://${location.host}/ws`;
const API_INFO = "/api/info";

let ws = null;
let pingTimer = null;

// 当前轮次的目标容器（每次 runTask 新建）
let currentLogGroup = null;
let currentResultBody = null;

// ── DOM refs ──────────────────────────────────────────────────────
const logList     = document.getElementById("log-list");
const resultEl    = document.getElementById("result-content");
const taskInput   = document.getElementById("task-input");
const runBtn      = document.getElementById("run-btn");
const statusDot   = document.getElementById("status-dot");
const agentBadge  = document.getElementById("agent-badge");

// ── 初始化 ────────────────────────────────────────────────────────
(async function init() {
  await loadAgentInfo();
  connectWS();
  taskInput.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) runTask();
  });
})();

async function loadAgentInfo() {
  try {
    const res = await fetch(API_INFO);
    const data = await res.json();
    agentBadge.textContent = data.name || data.agent_class;
    agentBadge.title = data.description || "";
  } catch {
    agentBadge.textContent = "unknown agent";
  }
}

// ── WebSocket ─────────────────────────────────────────────────────
function connectWS() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    // 新连接 = 后端新 session（id(websocket)），历史从这里重新开始
    clearAll();
    appendLog("🔗 新会话已连接", false);
    startPing();
  };

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    handleMessage(msg);
  };

  ws.onclose = () => {
    appendLog("⚠️ WebSocket 断开，3 秒后重连…", true);
    stopPing();
    setStatus("idle");
    setTimeout(connectWS, 3000);
  };

  ws.onerror = () => {
    appendLog("❌ WebSocket 连接错误", true);
  };
}

function handleMessage(msg) {
  switch (msg.type) {
    case "log":
      appendLog(msg.content, false);
      break;

    case "stream":
      appendStreamChunk(msg.content);
      break;

    case "result":
      showResult(msg.content);
      break;

    case "status":
      setStatus(msg.content);
      if (msg.content === "done" || msg.content === "error") {
        setRunning(false);
      }
      break;

    case "error":
      appendLog(msg.content, true);
      setStatus("error");
      setRunning(false);
      break;

    case "pong":
      // 心跳响应，静默处理
      break;

    default:
      appendLog(`[${msg.type}] ${msg.content}`, false);
  }
}

// ── 运行任务 ──────────────────────────────────────────────────────
function runTask() {
  const task = taskInput.value.trim();
  if (!task) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    appendLog("⚠️ WebSocket 未连接，请稍候", true);
    return;
  }

  // 不清空历史：为本轮新建日志分组 + 结果块（同一 session 内累积）
  currentLogGroup = startLogGroup(task);
  currentResultBody = startResultTurn(task);

  // 发送后清空输入框，防止重复提交
  taskInput.value = "";

  setRunning(true);
  setStatus("running");

  ws.send(JSON.stringify({ type: "run", task, kwargs: {} }));
}

function clearAll() {
  logList.innerHTML = "";
  resultEl.classList.add("empty");
  resultEl.textContent = "结果将在这里显示…";
  currentLogGroup = null;
  currentResultBody = null;
  taskInput.value = "";
  setStatus("idle");
}

// ── 轮次块构造 ────────────────────────────────────────────────────
function startLogGroup(task) {
  const group = document.createElement("div");
  group.className = "log-group";
  const head = document.createElement("div");
  head.className = "log-group-head";
  head.textContent = "▶ " + task;
  group.appendChild(head);
  logList.appendChild(group);
  logList.scrollTop = logList.scrollHeight;
  return group;
}

function startResultTurn(task) {
  if (resultEl.classList.contains("empty")) {
    resultEl.classList.remove("empty");
    resultEl.innerHTML = "";
  }
  const block = document.createElement("div");
  block.className = "turn-block";

  const q = document.createElement("div");
  q.className = "turn-q";
  q.textContent = "▶ " + task;

  const a = document.createElement("div");
  a.className = "turn-a pending";
  a.textContent = "运行中…";

  block.appendChild(q);
  block.appendChild(a);
  resultEl.appendChild(block);
  resultEl.scrollTop = resultEl.scrollHeight;
  return a;
}

// ── UI helpers ────────────────────────────────────────────────────
function appendLog(text, isError = false) {
  const item = document.createElement("div");
  item.className = "log-item" + (isError ? " error" : "");
  item.textContent = text;
  // 有当前轮次则归到该轮，否则（连接消息等）直接挂在日志区
  (currentLogGroup || logList).appendChild(item);
  logList.scrollTop = logList.scrollHeight;
}

function appendStreamChunk(chunk) {
  const a = currentResultBody;
  if (!a) return;
  if (a.classList.contains("pending")) {
    a.classList.remove("pending");
    a.textContent = "";
  }
  a.textContent += chunk;
  resultEl.scrollTop = resultEl.scrollHeight;
}

function showResult(text) {
  const a = currentResultBody;
  if (!a) return;
  a.classList.remove("pending");
  a.textContent = text;
  resultEl.scrollTop = resultEl.scrollHeight;
}

function setStatus(status) {
  statusDot.className = "";
  if (["running", "done", "error"].includes(status)) {
    statusDot.classList.add(status);
  }
}

function setRunning(isRunning) {
  runBtn.disabled = isRunning;
  taskInput.disabled = isRunning;
}

// ── 心跳 ─────────────────────────────────────────────────────────
function startPing() {
  pingTimer = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "ping" }));
    }
  }, 20000);
}

function stopPing() {
  clearInterval(pingTimer);
}
