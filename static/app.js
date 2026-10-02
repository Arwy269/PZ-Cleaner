'use strict';
/* ============================================================
 * 僵毁区块重置器 - 前端
 * 底图: pzmap.org DZI 等距渲染（x0/y0/sqr/scale 投影）
 * 区块: 存档 map/<X>/<Y>.bin（1 区块 = divisor×divisor 世界方格）
 * ============================================================ */

// 前端异常直接显示在页面上，便于排查
window.addEventListener('error', (e) => {
  const el = document.getElementById('hint');
  if (el) el.textContent = '⚠ JS错误: ' + e.message +
    ' @' + String(e.filename || '').split('/').pop() + ':' + e.lineno;
});
window.addEventListener('unhandledrejection', (e) => {
  const el = document.getElementById('hint');
  if (el) el.textContent = '⚠ 未处理异常: ' +
    ((e.reason && e.reason.message) || e.reason);
});

const $ = (id) => document.getElementById(id);

const S = {
  cfg: null,            // 服务端 config
  mapcfg: null,         // 底图投影参数
  map: null,
  layer: null,
  canvasLayer: null,
  chunkArr: [],         // [[cx,cy,bytes],...]
  chunks: new Map(),    // "cx,cy" -> bytes
  selArr: [],           // [[cx,cy],...]
  selSet: new Set(),
  divisor: 8,
  fmt: 'b42',
  mapDir: null,
  name: '',
  rectMode: false,
  pz: { running: null, procs: [] },
  drag: null,
  busy: false,
  tileStat: { loaded: 0, err: 0 },
};

/* ---------------- 基础工具 ---------------- */

async function api(path, body) {
  const opt = body !== undefined
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) }
    : undefined;
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch (e) { /* ignore */ }
  if (!r.ok || (data && data.error)) {
    throw new Error((data && data.error) || ('HTTP ' + r.status));
  }
  return data;
}

function fmtBytes(b) {
  if (b == null) return '—';
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
  return (b / 1073741824).toFixed(2) + ' GB';
}

let toastTimer = null;
function toast(msg, isErr) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.toggle('err', !!isErr);
  t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), 4500);
}

function log(msg, cls) {
  const box = $('resultBox');
  const line = document.createElement('div');
  if (cls) line.className = cls;
  line.textContent = '[' + new Date().toLocaleTimeString() + '] ' + msg;
  box.prepend(line);
  while (box.children.length > 60) box.removeChild(box.lastChild);
}

/* ---------------- 投影 ----------------
 * rawPx = x0 + (x - y) * sqr/2
 * rawPy = y0 + (x + y) * sqr/4
 * px = rawPx / scale, py = rawPy / scale
 * latLng = (-py / 2^refLevel, px / 2^refLevel)
 */

function worldToPx(x, y) {
  const m = S.mapcfg;
  return [(m.x0 + (x - y) * m.sqr / 2) / m.scale,
          (m.y0 + (x + y) * m.sqr / 4) / m.scale];
}
function pxToWorld(px, py) {
  const m = S.mapcfg;
  const dx = px * m.scale - m.x0;
  const dy = py * m.scale - m.y0;
  const a = dx / m.sqr;        // (x - y) / 2
  const b = (2 * dy) / m.sqr;  // (x + y) / 2
  return [a + b, b - a];
}
function pxToLatLng(px, py) {
  const d = Math.pow(2, S.mapcfg.refLevel);
  return [-py / d, px / d];            // [lat, lng]
}
function latLngToPx(lat, lng) {
  const d = Math.pow(2, S.mapcfg.refLevel);
  return [lng * d, -lat * d];
}
function worldToLatLng(x, y) {
  const p = worldToPx(x, y);
  return pxToLatLng(p[0], p[1]);
}
function latToWorld(lat, lng) {
  const p = latLngToPx(lat, lng);
  return pxToWorld(p[0], p[1]);
}
function containerToWorld(cx, cy) {
  const ll = S.map.containerPointToLatLng(L.point(cx, cy));
  return latToWorld(ll.lat, ll.lng);
}

/* ---------------- 底图图层 ---------------- */

function dziBounds() {
  const m = S.mapcfg;
  const d = Math.pow(2, m.refLevel);
  return L.latLngBounds([-m.h / d, 0], [0, m.w / d]);
}

const DziLayer = L.TileLayer.extend({
  // 覆盖 L.TileLayer 的 (url, options) 签名，只收 options
  initialize(options) {
    L.setOptions(this, options);
  },
  getTileUrl(coords) {
    return '/tiles/' + coords.z + '/' + coords.x + '/' + coords.y;
  },
});

function makeDziLayer() {
  const m = S.mapcfg;
  const layer = new DziLayer({
    tileSize: m.tileSize,
    minZoom: 10,
    maxZoom: m.refLevel,
    maxNativeZoom: m.refLevel,
    bounds: dziBounds(),
    noWrap: true,
    keepBuffer: 3,
    className: 'dzi-tile',
  });
  // Deep Zoom 边缘瓦片是裁切过的，按自然尺寸绘制避免拉伸错位
  layer.on('tileload', (e) => {
    const t = e.tile;
    if (t.naturalWidth && t.naturalHeight) {
      t.style.width = t.naturalWidth + 'px';
      t.style.height = t.naturalHeight + 'px';
    }
    S.tileStat.loaded++;
    reportTiles();
  });
  layer.on('tileerror', () => { S.tileStat.err++; reportTiles(); });
  return layer;
}

function reportTiles() {
  const el = $('tileOut');
  if (!el) return;
  const imgs = document.querySelectorAll('.leaflet-tile-pane img').length;
  el.textContent = '瓦片: ' + S.tileStat.loaded + '/' + imgs +
    (S.tileStat.err ? ' (失败' + S.tileStat.err + ')' : '');
}

/* ---------------- 区块画布 ---------------- */

const ChunkCanvas = L.Layer.extend({
  onAdd(map) {
    this._map = map;
    this._canvas = L.DomUtil.create('canvas', 'chunk-canvas');
    map.getContainer().appendChild(this._canvas);
    this._handler = () => this._schedule();
    map.on('move zoomend resize viewreset', this._handler);
    this._schedule();
  },
  onRemove(map) {
    map.off('move zoomend resize viewreset', this._handler);
    if (this._canvas && this._canvas.parentNode) this._canvas.parentNode.removeChild(this._canvas);
    this._canvas = null;
  },
  _schedule() {
    if (!this._map) return;
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = null; this._draw(); });
  },
  redraw() { this._schedule(); },
  _draw() {
    const map = this._map, c = this._canvas;
    if (!map || !c) return;
    const size = map.getSize();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.round(size.x * dpr), h = Math.round(size.y * dpr);
    if (c.width !== w || c.height !== h) {
      c.width = w; c.height = h;
      c.style.width = size.x + 'px'; c.style.height = size.y + 'px';
    }
    const ctx = c.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size.x, size.y);
    if (!S.mapcfg) return;

    const dd = S.divisor;
    // 一个区块在屏幕上的两个基向量（等距投影为仿射变换，所有区块形状相同）
    const p0 = map.latLngToContainerPoint(worldToLatLng(0, 0));
    const pu = map.latLngToContainerPoint(worldToLatLng(dd, 0));
    const pv = map.latLngToContainerPoint(worldToLatLng(0, dd));
    const U = { x: pu.x - p0.x, y: pu.y - p0.y };
    const V = { x: pv.x - p0.x, y: pv.y - p0.y };

    // 视口世界范围
    const cs = [[0, 0], [size.x, 0], [size.x, size.y], [0, size.y]]
      .map((p) => containerToWorld(p[0], p[1]));
    const xs = cs.map((p) => p[0]), ys = cs.map((p) => p[1]);
    const cx0 = Math.floor(Math.min(...xs) / dd) - 1;
    const cx1 = Math.ceil(Math.max(...xs) / dd) + 1;
    const cy0 = Math.floor(Math.min(...ys) / dd) - 1;
    const cy1 = Math.ceil(Math.max(...ys) / dd) + 1;
    const inView = (cx, cy) => cx >= cx0 && cx <= cx1 && cy >= cy0 && cy <= cy1;

    const MAXDRAW = 22000;
    const cellsInView = (cx1 - cx0 + 1) * (cy1 - cy0 + 1);
    let block = 1;
    if (cellsInView > MAXDRAW) {
      block = Math.max(1, Math.ceil(Math.sqrt(cellsInView / MAXDRAW)));
    }

    const rectPath = (ctx, cx, cy, span) => {
      const ox = p0.x + U.x * cx + V.x * cy;
      const oy = p0.y + U.y * cx + V.y * cy;
      const ax = U.x * span, ay = U.y * span;
      const bx = V.x * span, by = V.y * span;
      ctx.moveTo(ox, oy);
      ctx.lineTo(ox + ax, oy + ay);
      ctx.lineTo(ox + ax + bx, oy + ay + by);
      ctx.lineTo(ox + bx, oy + by);
      ctx.closePath();
    };

    // 已有区块（青）
    ctx.beginPath();
    if (block === 1) {
      for (let cx = cx0; cx <= cx1; cx++) {
        for (let cy = cy0; cy <= cy1; cy++) {
          if (S.chunks.has(cx + ',' + cy)) rectPath(ctx, cx, cy, 1);
        }
      }
    } else {
      const drawn = new Set();
      for (const it of S.chunkArr) {
        const cx = it[0], cy = it[1];
        if (!inView(cx, cy)) continue;
        const bx = Math.floor(cx / block) * block;
        const by = Math.floor(cy / block) * block;
        const key = bx + ',' + by;
        if (drawn.has(key)) continue;
        drawn.add(key);
        rectPath(ctx, bx, by, block);
      }
    }
    ctx.fillStyle = 'rgba(53,196,215,0.20)';
    ctx.strokeStyle = 'rgba(53,196,215,0.55)';
    ctx.lineWidth = 1;
    ctx.fill();
    ctx.stroke();

    // 选中区块（橙）
    if (S.selArr.length) {
      ctx.beginPath();
      if (S.selArr.length <= MAXDRAW || block === 1) {
        for (const it of S.selArr) rectPath(ctx, it[0], it[1], 1);
      } else {
        const drawn = new Set();
        for (const it of S.selArr) {
          const bx = Math.floor(it[0] / block) * block;
          const by = Math.floor(it[1] / block) * block;
          const key = bx + ',' + by;
          if (drawn.has(key)) continue;
          drawn.add(key);
          rectPath(ctx, bx, by, block);
        }
      }
      ctx.fillStyle = 'rgba(255,122,47,0.40)';
      ctx.strokeStyle = 'rgba(255,160,90,0.95)';
      ctx.lineWidth = 1.5;
      ctx.fill();
      ctx.stroke();
    }
  },
});

/* ---------------- 选择逻辑 ---------------- */

function setSelection(cells) {
  S.selSet = new Set();
  S.selArr = [];
  for (const c of cells) {
    const key = c[0] + ',' + c[1];
    if (!S.selSet.has(key)) {
      S.selSet.add(key);
      S.selArr.push([c[0], c[1]]);
    }
  }
  redrawMap();
  updateSelInfo();
  updateResetBtn();
}

function clearSelection(redraw = true) {
  S.selSet = new Set();
  S.selArr = [];
  if (redraw) { redrawMap(); updateSelInfo(); }
  updateResetBtn();
}

function redrawMap() {
  if (S.canvasLayer) S.canvasLayer.redraw();
}

function inQuad(px, py, quad) {
  let sign = 0;
  for (let i = 0; i < 4; i++) {
    const a = quad[i], b = quad[(i + 1) % 4];
    const cr = (b[0] - a[0]) * (py - a[1]) - (b[1] - a[1]) * (px - a[0]);
    if (cr !== 0) {
      const s = cr > 0 ? 1 : -1;
      if (sign === 0) sign = s;
      else if (s !== sign) return false;
    }
  }
  return true;
}

function selectScreenRect(x0, y0, x1, y1, additive) {
  const quad = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    .map((p) => containerToWorld(p[0], p[1]));
  const xs = quad.map((p) => p[0]), ys = quad.map((p) => p[1]);
  const dd = S.divisor;
  const cx0 = Math.floor(Math.min(...xs) / dd);
  const cx1 = Math.floor(Math.max(...xs) / dd);
  const cy0 = Math.floor(Math.min(...ys) / dd);
  const cy1 = Math.floor(Math.max(...ys) / dd);
  if ((cx1 - cx0 + 1) * (cy1 - cy0 + 1) > S.cfg.maxCells) {
    toast('框选范围过大（超过 ' + S.cfg.maxCells + ' 个区块），请放大后再选', true);
    return;
  }
  const cells = additive ? S.selArr.slice() : [];
  const have = new Set(additive ? S.selSet : []);
  for (let cx = cx0; cx <= cx1; cx++) {
    for (let cy = cy0; cy <= cy1; cy++) {
      const wx = (cx + 0.5) * dd, wy = (cy + 0.5) * dd;
      if (!inQuad(wx, wy, quad)) continue;
      const key = cx + ',' + cy;
      if (have.has(key)) continue;
      have.add(key);
      cells.push([cx, cy]);
    }
  }
  setSelection(cells);
}

function selectWorldRect(x0, y0, x1, y1) {
  const dd = S.divisor;
  const cx0 = Math.floor(Math.min(x0, x1) / dd);
  const cx1 = Math.floor((Math.max(x0, x1) - 0.0001) / dd);
  const cy0 = Math.floor(Math.min(y0, y1) / dd);
  const cy1 = Math.floor((Math.max(y0, y1) - 0.0001) / dd);
  if ((cx1 - cx0 + 1) * (cy1 - cy0 + 1) > S.cfg.maxCells) {
    toast('范围过大（超过 ' + S.cfg.maxCells + ' 个区块），请缩小', true);
    return;
  }
  const cells = [];
  for (let cx = cx0; cx <= cx1; cx++) {
    for (let cy = cy0; cy <= cy1; cy++) cells.push([cx, cy]);
  }
  setSelection(cells);
}

/* ---------------- 拖拽框选 ---------------- */

function bindDrag() {
  const container = S.map.getContainer();
  container.addEventListener('mousedown', (e) => {
    if (!S.rectMode || e.button !== 0) return;
    L.DomEvent.stopPropagation(e);
    L.DomEvent.preventDefault(e);
    const rect = container.getBoundingClientRect();
    S.drag = {
      x0: e.clientX - rect.left, y0: e.clientY - rect.top,
      x1: e.clientX - rect.left, y1: e.clientY - rect.top,
      additive: e.ctrlKey || e.metaKey,
    };
    S.map.dragging.disable();
    const rb = $('rubber');
    rb.classList.remove('hidden');
    updateRubber();
    window.addEventListener('mousemove', onDragMove);
    window.addEventListener('mouseup', onDragUp);
  });
}

function updateRubber() {
  const d = S.drag;
  if (!d) return;
  const rb = $('rubber');
  const x = Math.min(d.x0, d.x1), y = Math.min(d.y0, d.y1);
  const w = Math.abs(d.x1 - d.x0), h = Math.abs(d.y1 - d.y0);
  rb.style.left = x + 'px'; rb.style.top = y + 'px';
  rb.style.width = w + 'px'; rb.style.height = h + 'px';
}

function onDragMove(e) {
  if (!S.drag) return;
  const rect = S.map.getContainer().getBoundingClientRect();
  S.drag.x1 = Math.max(0, Math.min(rect.width, e.clientX - rect.left));
  S.drag.y1 = Math.max(0, Math.min(rect.height, e.clientY - rect.top));
  updateRubber();
}

function onDragUp() {
  const d = S.drag;
  window.removeEventListener('mousemove', onDragMove);
  window.removeEventListener('mouseup', onDragUp);
  $('rubber').classList.add('hidden');
  S.drag = null;
  S.map.dragging.enable();
  if (!d) return;
  const w = Math.abs(d.x1 - d.x0), h = Math.abs(d.y1 - d.y0);
  if (w < 5 || h < 5) return; // 视为误触
  selectScreenRect(d.x0, d.y0, d.x1, d.y1, d.additive);
  if (d.additive) toast('已追加选择（共 ' + S.selArr.length + ' 区块）');
}

/* ---------------- UI 更新 ---------------- */

function updateSelInfo() {
  const el = $('selInfo');
  if (!S.selArr.length) {
    el.innerHTML = '<span class="k">尚未选择</span>';
    return;
  }
  let cx0 = Infinity, cy0 = Infinity, cx1 = -Infinity, cy1 = -Infinity;
  let exist = 0, bytes = 0;
  for (const c of S.selArr) {
    if (c[0] < cx0) cx0 = c[0];
    if (c[0] > cx1) cx1 = c[0];
    if (c[1] < cy0) cy0 = c[1];
    if (c[1] > cy1) cy1 = c[1];
    const b = S.chunks.get(c[0] + ',' + c[1]);
    if (b != null) { exist++; bytes += b; }
  }
  const dd = S.divisor;
  const sample = S.fmt === 'b42'
    ? `map/${cx0}/${cy0}.bin`
    : `r${cx0}_${cy0}.bin`;
  el.innerHTML =
    '<span class="k">区块范围</span> X ' + cx0 + '~' + cx1 +
    '（' + (cx1 - cx0 + 1) + ' 列），Y ' + cy0 + '~' + cy1 +
    '（' + (cy1 - cy0 + 1) + ' 行）<br>' +
    '<span class="k">世界方格</span> x ' + (cx0 * dd) + '~' + ((cx1 + 1) * dd) +
    '，y ' + (cy0 * dd) + '~' + ((cy1 + 1) * dd) + '<br>' +
    '<span class="k">选中</span> <span class="big">' + S.selArr.length + '</span> 区块，' +
    '其中已存在文件 <span class="big">' + exist + '</span> 个（' + fmtBytes(bytes) + '）<br>' +
    '<span class="k">示例文件</span> <code>' + sample + '</code>';
}

function updateResetBtn() {
  const needForce = S.pz.running !== false;
  const wordOk = $('confirmIn').value.trim() === S.cfg.confirmWord;
  const forceOk = !needForce || $('forceChk').checked;
  $('btnReset').disabled = !(S.selArr.length && wordOk && forceOk &&
                             S.mapcfg && !S.busy);
}

function updatePz(pz) {
  S.pz = pz || { running: null, procs: [] };
  const b = $('pzBadge');
  b.classList.remove('green', 'red', 'yellow', 'gray');
  if (S.pz.running === true) {
    const p = (S.pz.procs && S.pz.procs[0]) || {};
    b.classList.add('red');
    b.textContent = '⚠ 服务器进程运行中' + (p.name ? '：' + p.name : '');
    $('forceRow').classList.remove('hidden');
  } else if (S.pz.running === false) {
    b.classList.add('green');
    b.textContent = '✔ 服务器进程已停止';
    $('forceRow').classList.add('hidden');
    $('forceChk').checked = false;
  } else {
    b.classList.add('yellow');
    b.textContent = '? 无法检测进程状态';
    $('forceRow').classList.remove('hidden');
  }
  updateResetBtn();
}

function updateMapBadge() {
  const b = $('mapBadge');
  b.classList.remove('green', 'red', 'yellow', 'gray');
  if (S.mapcfg) {
    b.classList.add('green');
    b.textContent = '底图: ' + S.mapcfg.source + '（B42 等距）';
  } else {
    b.classList.add('red');
    b.textContent = '底图不可用';
  }
}

function renderSaves(saves, selected) {
  const sel = $('saveSelect');
  sel.innerHTML = '';
  for (const s of saves) {
    const o = document.createElement('option');
    o.value = s.path;
    o.textContent = s.name;
    sel.appendChild(o);
  }
  if (selected && ![...sel.options].some((o) => o.value === selected)) {
    const o = document.createElement('option');
    o.value = selected;
    o.textContent = selected;
    sel.appendChild(o);
  }
  if (selected) sel.value = selected;
}

function applyChunks(p) {
  if (!p) {
    S.chunkArr = []; S.chunks = new Map();
  } else {
    S.chunkArr = p.chunks || [];
    S.chunks = new Map(S.chunkArr.map((c) => [c[0] + ',' + c[1], c[2]]));
    S.divisor = p.divisor || S.divisor;
    S.fmt = p.fmt || 'b42';
    S.mapDir = p.mapDir;
    S.name = p.name || '';
  }
  $('divOut').textContent = S.divisor + '×' + S.divisor;
  $('saveOut').textContent = '存档: ' + (S.mapDir || '—');
  updateSelInfo();
  redrawMap();
}

function fitInitial() {
  if (!S.map || !S.mapcfg) return;
  const p = S.chunkArr;
  if (!p.length) {
    const c = pxToWorld(S.mapcfg.w / 2, S.mapcfg.h / 2);
    S.map.setView(worldToLatLng(c[0], c[1]), 14);
    return;
  }
  let cx0 = Infinity, cy0 = Infinity, cx1 = -Infinity, cy1 = -Infinity;
  for (const it of p) {
    if (it[0] < cx0) cx0 = it[0];
    if (it[0] > cx1) cx1 = it[0];
    if (it[1] < cy0) cy0 = it[1];
    if (it[1] > cy1) cy1 = it[1];
  }
  const dd = S.divisor;
  const x0 = cx0 * dd, y0 = cy0 * dd;
  const x1 = (cx1 + 1) * dd, y1 = (cy1 + 1) * dd;
  const corners = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    .map((c) => worldToLatLng(c[0], c[1]));
  S.map.fitBounds(L.latLngBounds(corners), { padding: [40, 40], maxZoom: 19 });
}

function zoomToSelection() {
  if (!S.selArr.length) { toast('尚未选择区域', true); return; }
  let cx0 = Infinity, cy0 = Infinity, cx1 = -Infinity, cy1 = -Infinity;
  for (const c of S.selArr) {
    cx0 = Math.min(cx0, c[0]); cx1 = Math.max(cx1, c[0]);
    cy0 = Math.min(cy0, c[1]); cy1 = Math.max(cy1, c[1]);
  }
  const dd = S.divisor;
  const corners = [[cx0 * dd, cy0 * dd], [(cx1 + 1) * dd, cy0 * dd],
                   [(cx1 + 1) * dd, (cy1 + 1) * dd], [cx0 * dd, (cy1 + 1) * dd]]
    .map((c) => worldToLatLng(c[0], c[1]));
  S.map.fitBounds(L.latLngBounds(corners), { padding: [40, 40], maxZoom: 21 });
}

/* ---------------- 交互绑定 ---------------- */

function setRectMode(on) {
  S.rectMode = on;
  $('btnRectMode').classList.toggle('active', on);
  $('modeTip').innerHTML = on
    ? '<b style="color:#ff7a2f">框选模式已开启</b>：在地图上按住拖拽框选；按住 <b>Ctrl</b> 拖拽可追加选择。'
    : '开启框选模式后，在地图上<b>按住拖拽</b>框出要重置的范围（按住 Ctrl 追加选择）。';
}

function bindUI() {
  $('btnRectMode').addEventListener('click', () => setRectMode(!S.rectMode));
  $('btnClear').addEventListener('click', () => clearSelection());
  $('btnZoomSel').addEventListener('click', zoomToSelection);

  $('btnLocate').addEventListener('click', () => {
    const x = parseFloat($('inX').value), y = parseFloat($('inY').value);
    const w = parseFloat($('inW').value), h = parseFloat($('inH').value);
    if (![x, y, w, h].every((v) => isFinite(v)) || w <= 0 || h <= 0) {
      toast('请输入有效的中心坐标与宽高', true);
      return;
    }
    selectWorldRect(x - w / 2, y - h / 2, x + w / 2, y + h / 2);
    zoomToSelection();
    toast('已选择 ' + S.selArr.length + ' 个区块');
  });

  $('confirmIn').addEventListener('input', updateResetBtn);
  $('forceChk').addEventListener('change', updateResetBtn);

  $('btnReset').addEventListener('click', doReset);

  $('saveSelect').addEventListener('change', (e) => selectSave(e.target.value));
  $('btnSelect').addEventListener('click', () => {
    const p = $('savePath').value.trim();
    if (!p) { toast('请输入存档目录或 map 目录路径', true); return; }
    selectSave(p);
  });
  $('btnRescan').addEventListener('click', async () => {
    try {
      const r = await api('/api/rescan', {});
      applyChunks(r.chunks);
      toast('已重新扫描：' + (r.chunks ? r.chunks.count : 0) + ' 个区块文件');
    } catch (e) { toast(e.message, true); }
  });

  $('btnRetryMap').addEventListener('click', () => location.reload());

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { setRectMode(false); clearSelection(); }
  });
}

async function selectSave(path) {
  try {
    const r = await api('/api/select', { path });
    clearSelection(false);
    applyChunks(r.chunks);
    renderSaves($('saveSelect').options.length
      ? [...$('saveSelect').options].map((o) => ({ name: o.textContent, path: o.value }))
      : [], r.chunks ? r.chunks.mapDir : path);
    if (r.chunks && r.chunks.mapDir) {
      const sel = $('saveSelect');
      if (![...sel.options].some((o) => o.value === r.chunks.mapDir)) {
        const o = document.createElement('option');
        o.value = r.chunks.mapDir; o.textContent = r.chunks.name;
        sel.appendChild(o);
      }
      sel.value = r.chunks.mapDir;
    }
    fitInitial();
    toast('已载入存档：' + r.name + '（' + r.count + ' 个区块文件）');
    log('切换存档 ' + r.name + '，fmt=' + r.fmt + '，文件数=' + r.count, 'ok');
  } catch (e) {
    toast(e.message, true);
  }
}

async function doReset() {
  if (S.busy) return;
  const mode = document.querySelector('input[name="mode"]:checked').value;
  S.busy = true;
  updateResetBtn();
  $('btnReset').textContent = '执行中…';
  try {
    const r = await api('/api/reset', {
      cells: S.selArr,
      mode,
      force: $('forceChk').checked,
      confirm: $('confirmIn').value.trim(),
    });
    const msg = '已删除 ' + r.deleted + ' 个区块文件（' + fmtBytes(r.bytes) + '）' +
      (r.missing ? '，跳过不存在 ' + r.missing + ' 个' : '') +
      (r.backupDir ? '；备份: ' + r.backupDir : '（未备份）');
    log(msg, 'ok');
    if (r.errors && r.errors.length) {
      log('部分失败: ' + r.errors.slice(0, 5).join('； '), 'err');
    }
    toast('重置完成：删除 ' + r.deleted + ' 个区块');
    applyChunks(r.chunks);
    clearSelection();
    updatePz(r.pz);
    $('confirmIn').value = '';
    updateResetBtn();
  } catch (e) {
    log('失败: ' + e.message, 'err');
    toast(e.message, true);
    if (/进程|状态未知|强制/.test(e.message)) {
      try { updatePz(await api('/api/pz')); } catch (e2) { /* ignore */ }
    }
  } finally {
    S.busy = false;
    $('btnReset').textContent = '执行重置';
    updateResetBtn();
  }
}

/* ---------------- 地图鼠标读数 ---------------- */

function bindMouseReadout() {
  const container = S.map.getContainer();
  container.addEventListener('mousemove', (e) => {
    if (!S.mapcfg) return;
    const rect = container.getBoundingClientRect();
    const w = containerToWorld(e.clientX - rect.left, e.clientY - rect.top);
    $('coordOut').textContent =
      '世界坐标: ' + w[0].toFixed(1) + ', ' + w[1].toFixed(1);
    const cx = Math.floor(w[0] / S.divisor), cy = Math.floor(w[1] / S.divisor);
    $('chunkOut').textContent = '区块文件: ' +
      (S.fmt === 'b42' ? `map/${cx}/${cy}.bin` : `r${cx}_${cy}.bin`) +
      (S.chunks.has(cx + ',' + cy) ? ' ✔存在' : '');
  });
  S.map.on('zoomend moveend', () => {
    $('zoomOut').textContent = '缩放: ' + S.map.getZoom().toFixed(1);
  });
}

/* ---------------- 启动 ---------------- */

function initMap() {
  const m = S.mapcfg;
  S.map = L.map('map', {
    crs: L.CRS.Simple,
    minZoom: 10,
    maxZoom: m.refLevel,
    maxBounds: dziBounds().pad(0.5),
    maxBoundsViscosity: 0.6,
    zoomControl: true,
    attributionControl: false,
    zoomSnap: 1,
    zoomDelta: 1,
  });
  S.layer = makeDziLayer().addTo(S.map);
  S.canvasLayer = new ChunkCanvas().addTo(S.map);
  bindDrag();
  bindMouseReadout();
}

async function init() {
  bindUI();
  let boot;
  try {
    boot = await api('/api/bootstrap');
  } catch (e) {
    toast('无法连接本地服务: ' + e.message, true);
    return;
  }
  S.cfg = boot.config;
  S.divisor = boot.config.chunkDivisor;
  $('ver').textContent = 'v' + boot.version;
  $('backupPath').textContent = boot.config.backupRoot;
  renderSaves(boot.saves, boot.selected);
  updatePz(boot.pz);
  applyChunks(boot.chunks);

  if (boot.map) {
    S.mapcfg = boot.map;
    updateMapBadge();
    initMap();
    fitInitial();
    setInterval(reportTiles, 2000);
    setTimeout(reportTiles, 800);
  } else {
    updateMapBadge();
    $('ovMsg').textContent = '底图加载失败：' + (boot.mapError || '未知错误') +
      '。请点击重试（需要访问 pzmap.org / pzmap.net）。';
    $('mapOverlay').classList.remove('hidden');
  }

  if (boot.chunks) {
    log('存档 ' + boot.chunks.name + '：' + boot.chunks.count +
        ' 个区块文件，' + fmtBytes(boot.chunks.bytes), 'ok');
  } else {
    log('未找到存档，请在顶部输入 map 目录路径', 'err');
  }

  // 支持 ?sel=中心X,中心Y,宽,高 快速定位选择
  try {
    const q = new URLSearchParams(location.search).get('sel');
    if (q) {
      const v = q.split(',').map(Number);
      if (v.length === 4 && v.every(isFinite)) {
        selectWorldRect(v[0] - v[2] / 2, v[1] - v[3] / 2,
                        v[0] + v[2] / 2, v[1] + v[3] / 2);
        if (S.map) zoomToSelection();
        toast('已按 URL 参数选择 ' + S.selArr.length + ' 个区块');
      }
    }
  } catch (e) { /* ignore */ }

  // 定期刷新进程状态
  setInterval(async () => {
    try { updatePz(await api('/api/pz')); } catch (e) { /* ignore */ }
  }, 15000);
}

document.addEventListener('DOMContentLoaded', init);
