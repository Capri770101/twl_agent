'use strict';

/*
 * 跳舞兰 · 调用监控面板（纯前端，无框架）
 * - 密钥仅存 sessionStorage，不落盘、不进源码
 * - 所有 /api/metrics 请求带 X-Dashboard-Key；SSE 因无法自定义 header 改走 ?key=
 * - 同源部署（api.tiaowulan.com），API 用绝对路径 /api/metrics/*
 */
const API = '/api/metrics';
const KEY_STORAGE = 'dash_key';

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

// 时间分桶（hour/day）格式化为可读标签
function fmtBucket(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:00`;
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

async function loadSummary() {
  const s = await api('/summary');
  $('kpiTotal').textContent = s.total_calls != null ? s.total_calls : '—';
  $('kpi24h').textContent = s.calls_24h != null ? s.calls_24h : '—';
  const errPct = (s.calls_24h > 0)
    ? ((s.errors_24h || 0) / s.calls_24h * 100).toFixed(1) + '%'
    : '0%';
  $('kpiErr').textContent = errPct;
  $('kpiPlat').textContent = s.active_platforms_24h != null ? s.active_platforms_24h : '—';
  $('kpiLat').textContent = (s.avg_latency_24h != null) ? s.avg_latency_24h + 'ms' : '—';
  if ($('updated')) $('updated').textContent = '更新于 ' + fmtTime(new Date());
}

async function loadCalls() {
  const rows = await api('/calls?hours=24&bucket=hour');
  const labels = rows.map((r) => fmtBucket(r.bucket));
  const total = rows.map((r) => r.total);
  const errs = rows.map((r) => r.errors);
  drawLine('callsChart', labels, [
    { label: '调用量', data: total, color: '#4f8cff' },
    { label: '错误', data: errs, color: '#ff5b5b' },
  ], false);
  drawLine('errorChart', labels, [
    { label: '错误数', data: errs, color: '#ff5b5b' },
  ], true);
}

async function loadPlatforms() {
  const rows = await api('/platforms');
  const labels = rows.map((r) => r.platform_id || '未知');
  const data = rows.map((r) => r.total_calls);
  drawBar('platformChart', labels, data, '#7c5cff');
}

async function loadTools() {
  const rows = await api('/tools');
  const labels = rows.map((r) => r.tool_name);
  const data = rows.map((r) => r.total);
  drawBar('toolChart', labels, data, '#22c08b');
}

function destroy(canvasId) {
  if (charts[canvasId]) { charts[canvasId].destroy(); charts[canvasId] = null; }
}

function drawLine(canvasId, labels, series, noFill) {
  const el = $(canvasId);
  if (!el) return;
  const ctx = el.getContext('2d');
  destroy(canvasId);
  charts[canvasId] = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: series.map((s) => ({
        label: s.label,
        data: s.data,
        borderColor: s.color,
        backgroundColor: noFill ? 'transparent' : s.color + '22',
        fill: !noFill,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 2,
      })),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { display: series.length > 1 } },
      scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
    },
  });
}

function drawBar(canvasId, labels, data, color) {
  const el = $(canvasId);
  if (!el) return;
  const ctx = el.getContext('2d');
  destroy(canvasId);
  charts[canvasId] = new Chart(ctx, {
    type: 'bar',
    data: { labels, datasets: [{ label: '次数', data, backgroundColor: color }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
    },
  });
}

function addFeed(d) {
  const ul = $('liveFeed');
  if (!ul) return;
  const li = document.createElement('li');
  const st = d.status === 'error' ? 'err' : 'ok';
  li.innerHTML =
    `<span class="t">${fmtTime(d.created_at)}</span>` +
    `<span class="badge ${st}">${st === 'err' ? '错误' : '成功'}</span>` +
    `<span class="plat">${escapeHtml(d.platform_id || '—')}</span>` +
    `<span class="uid">${escapeHtml((d.user_id || '').slice(0, 12))}</span>` +
    `<span class="lat">${d.latency_ms != null ? d.latency_ms + 'ms' : '?'} · ${escapeHtml(d.model || '')}</span>`;
  ul.prepend(li);
  while (ul.children.length > 30) ul.removeChild(ul.lastChild);
}

function startStream() {
  if (es) es.close();
  es = new EventSource(API + '/stream?key=' + encodeURIComponent(KEY));
  setConn('on', '实时连接中');
  es.onmessage = (ev) => {
    try {
      const d = JSON.parse(ev.data);
      addFeed(d);
      // 新事件到来时顺手刷新概览，保持 KPI 新鲜
      loadSummary().catch(() => {});
    } catch (_) { /* ignore */ }
  };
  es.onerror = () => setConn('off', '连接中断，重连中…');
}

async function refreshAll() {
  try {
    await Promise.all([loadSummary(), loadCalls(), loadPlatforms(), loadTools()]);
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
  if (KEY) enterApp();
});
