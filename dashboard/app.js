'use strict';

/*
 * 花艺智能体 · 调用监控面板（纯前端，无框架）
 * - 密钥仅存 sessionStorage，不落盘、不进前端源码
 * - 所有 /api/metrics 请求带 X-Dashboard-Key；SSE 无法自定义 header，改走 ?key=
 * - 同源部署，API 用绝对路径 /api/metrics/*
 * - 布局要点：长标签（工具名/平台名）一律用横向条形图，避免标签重叠；
 *   容器高度按条目数动态计算，条目多也不挤压。
 */
const API = '/api/metrics';
const KEY_STORAGE = 'dash_key';
const MAX_FEED_ROWS = 30;

let KEY = sessionStorage.getItem(KEY_STORAGE) || '';
let charts = {};
let es = null;
let refreshTimer = null;

const $ = (id) => document.getElementById(id);

function pad(n) { return String(n).padStart(2, '0'); }

function fmtTime(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fmtBucket(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return `${pad(d.getHours())}:00`;
}

function fmtNum(n) {
  if (n == null) return '—';
  return Number(n).toLocaleString('zh-CN');
}

// 耗时：>=1000ms 用秒展示，避免一长串数字影响扫读
function fmtDur(ms) {
  if (ms == null) return '—';
  return ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : ms + 'ms';
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

async function api(path) {
  const res = await fetch(API + path, { headers: { 'X-Dashboard-Key': KEY } });
  if (res.status === 401) { logout('密钥无效或已失效'); throw new Error('unauthorized'); }
  if (res.status === 503) { logout('服务端未启用监控（DASHBOARD_API_KEY 未配置）'); throw new Error('disabled'); }
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res.json();
}

function setConn(state, text) {
  const dot = $('connDot');
  if (dot) dot.className = 'dot ' + (state === 'on' ? 'on' : 'off');
  if ($('connText')) $('connText').textContent = text;
}

/* ---------- 空状态 / 容器高度 ---------- */

function setEmpty(boxId, isEmpty, text) {
  const box = $(boxId);
  if (!box) return;
  let ov = box.querySelector('.empty');
  if (isEmpty) {
    if (!ov) {
      ov = document.createElement('div');
      ov.className = 'empty';
      box.appendChild(ov);
    }
    ov.textContent = text || '暂无数据';
    ov.style.display = 'flex';
  } else if (ov) {
    ov.style.display = 'none';
  }
}

// 横向条形图：每条约占 28px，太矮会挤压标签
function sizeBoxForBars(boxId, count) {
  const box = $(boxId);
  if (!box) return;
  const h = Math.max(180, count * 28 + 32);
  box.style.height = h + 'px';
}

/* ---------- 绘图 ---------- */

function destroy(canvasId) {
  if (charts[canvasId]) { charts[canvasId].destroy(); charts[canvasId] = null; }
}

function drawLine(canvasId, labels, series) {
  const el = $(canvasId);
  if (!el) return;
  destroy(canvasId);
  charts[canvasId] = new Chart(el.getContext('2d'), {
    type: 'line',
    data: {
      labels,
      datasets: series.map((s) => ({
        label: s.label,
        data: s.data,
        borderColor: s.color,
        backgroundColor: s.fill ? s.color + '20' : 'transparent',
        fill: !!s.fill,
        tension: 0.3,
        pointRadius: 0,
        pointHoverRadius: 4,
        borderWidth: 2,
      })),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: true, position: 'top', align: 'end', labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true, font: { size: 11 } } },
      },
      scales: {
        x: { grid: { display: false }, ticks: { font: { size: 11 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 } },
        y: { beginAtZero: true, grid: { color: '#f0f3f8' }, ticks: { precision: 0, font: { size: 11 } } },
      },
    },
  });
}

function drawHBar(canvasId, labels, data, color, tooltipFn) {
  const el = $(canvasId);
  if (!el) return;
  destroy(canvasId);
  charts[canvasId] = new Chart(el.getContext('2d'), {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: '调用次数',
        data,
        backgroundColor: color,
        borderRadius: 4,
        barThickness: 'flex',
        maxBarThickness: 18,
      }],
    },
    options: {
      indexAxis: 'y',          // 横向：长工具名/平台名不会被裁切或重叠
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: tooltipFn ? { callbacks: { label: tooltipFn } } : {},
      },
      scales: {
        x: { beginAtZero: true, grid: { color: '#f0f3f8' }, ticks: { precision: 0, font: { size: 11 } } },
        y: { grid: { display: false }, ticks: { font: { size: 11.5 }, autoSkip: false } },
      },
    },
  });
}

/* ---------- 数据加载 ---------- */

async function loadSummary() {
  const s = await api('/summary');
  $('kpiTotal').textContent = fmtNum(s.total_calls);
  $('kpi24h').textContent = fmtNum(s.calls_24h);
  $('kpiErr').textContent = (s.calls_24h > 0)
    ? ((s.errors_24h || 0) / s.calls_24h * 100).toFixed(1) + '%'
    : '0%';
  $('kpiPlat').textContent = fmtNum(s.active_platforms_24h);
  $('kpiLat').textContent = fmtDur(s.avg_latency_24h);
  if ($('updated')) $('updated').textContent = '更新于 ' + fmtTime(new Date());
}

async function loadCalls() {
  const rows = await api('/calls?hours=24&bucket=hour');
  setEmpty('callsBox', rows.length === 0, '近 24 小时暂无调用');
  const labels = rows.map((r) => fmtBucket(r.bucket));
  drawLine('callsChart', labels, [
    { label: '调用量', data: rows.map((r) => r.total), color: '#4f8cff', fill: true },
    { label: '错误', data: rows.map((r) => r.errors), color: '#ef4b4b', fill: false },
  ]);
}

async function loadPlatforms() {
  const rows = await api('/platforms');
  setEmpty('platformBox', rows.length === 0, '暂无平台调用数据');
  sizeBoxForBars('platformBox', rows.length);
  drawHBar(
    'platformChart',
    rows.map((r) => r.platform_id || '未知'),
    rows.map((r) => r.total_calls),
    '#7c5cff',
    (ctx) => {
      const r = rows[ctx.dataIndex] || {};
      return [
        '累计调用：' + fmtNum(r.total_calls),
        '近 24h：' + fmtNum(r.calls_24h),
        '独立用户：' + fmtNum(r.distinct_users),
        '错误数：' + fmtNum(r.errors),
      ];
    }
  );
}

async function loadTools() {
  const rows = await api('/tools');
  setEmpty('toolBox', rows.length === 0, '暂无工具调用数据');
  sizeBoxForBars('toolBox', rows.length);
  drawHBar(
    'toolChart',
    rows.map((r) => r.tool_name),
    rows.map((r) => r.total),
    '#17b978',
    (ctx) => {
      const r = rows[ctx.dataIndex] || {};
      return [
        '调用：' + fmtNum(r.total),
        '成功：' + fmtNum(r.success) + '　失败：' + fmtNum(r.errors),
        '平均耗时：' + fmtDur(r.avg_latency_ms),
      ];
    }
  );
}

/* ---------- 实时调用流（表格） ---------- */

function addFeedRow(d) {
  const tbody = $('liveFeed');
  if (!tbody) return;
  // 首次有数据时清掉占位行
  const placeholder = tbody.querySelector('.empty-row');
  if (placeholder) placeholder.remove();

  const tr = document.createElement('tr');
  const ok = d.status !== 'error';
  tr.innerHTML =
    `<td class="num dim">${fmtTime(d.created_at)}</td>` +
    `<td>${escapeHtml(d.platform_id || '—')}</td>` +
    `<td class="mono dim" title="${escapeHtml(d.user_id || '')}">${escapeHtml((d.user_id || '—').slice(0, 18))}</td>` +
    `<td><span class="badge ${ok ? 'ok' : 'err'}">${ok ? '成功' : '错误'}</span></td>` +
    `<td class="num">${fmtDur(d.latency_ms)}</td>` +
    `<td class="num dim">${d.tool_calls != null ? d.tool_calls : '—'}</td>` +
    `<td class="dim">${escapeHtml(d.model || '—')}</td>`;
  tbody.prepend(tr);
  while (tbody.children.length > MAX_FEED_ROWS) tbody.removeChild(tbody.lastChild);
}

function renderFeedEmpty() {
  const tbody = $('liveFeed');
  if (!tbody || tbody.children.length) return;
  tbody.innerHTML = '<tr class="empty-row"><td colspan="7">暂无调用记录，有新调用会自动出现</td></tr>';
}

/* ---------- 实时流与刷新 ---------- */

function startStream() {
  if (es) es.close();
  es = new EventSource(API + '/stream?key=' + encodeURIComponent(KEY));
  setConn('on', '实时连接中');
  es.onmessage = (ev) => {
    try {
      const d = JSON.parse(ev.data);
      addFeedRow(d);
      loadSummary().catch(() => {});   // 有新调用就顺手刷新概览
    } catch (_) { /* 忽略解析异常 */ }
  };
  es.onerror = () => setConn('off', '连接中断，重连中…');
}

async function refreshAll() {
  try {
    await Promise.all([loadSummary(), loadCalls(), loadPlatforms(), loadTools()]);
    renderFeedEmpty();
    setConn('on', '已连接');
  } catch (e) {
    if (e.message !== 'unauthorized' && e.message !== 'disabled') {
      setConn('off', '刷新失败');
      console.warn(e);
    }
  }
}

function enterApp() {
  $('login').classList.add('hidden');
  $('app').classList.remove('hidden');
  refreshAll();
  startStream();
  refreshTimer = setInterval(refreshAll, 20000);
}

function logout(msg) {
  if (es) { es.close(); es = null; }
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  sessionStorage.removeItem(KEY_STORAGE);
  KEY = '';
  $('app').classList.add('hidden');
  $('login').classList.remove('hidden');
  if (msg) $('loginErr').textContent = msg;
}

async function doLogin() {
  const k = $('keyInput').value.trim();
  if (!k) { $('loginErr').textContent = '请输入密钥'; return; }
  KEY = k;
  $('loginErr').textContent = '';
  try {
    const res = await fetch(API + '/summary', { headers: { 'X-Dashboard-Key': KEY } });
    if (res.status === 401) { $('loginErr').textContent = '密钥无效'; KEY = ''; return; }
    if (res.status === 503) { $('loginErr').textContent = '服务端未启用监控（DASHBOARD_API_KEY 未配置）'; KEY = ''; return; }
    if (!res.ok) { $('loginErr').textContent = '服务异常：HTTP ' + res.status; KEY = ''; return; }
    sessionStorage.setItem(KEY_STORAGE, KEY);
    enterApp();
  } catch (e) {
    $('loginErr').textContent = '无法连接服务：' + e.message;
    KEY = '';
  }
}

window.addEventListener('DOMContentLoaded', () => {
  $('loginBtn').addEventListener('click', doLogin);
  $('keyInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });
  $('logoutBtn').addEventListener('click', () => logout('已退出'));
  renderFeedEmpty();
  if (KEY) enterApp();
});
