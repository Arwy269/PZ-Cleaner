// 网页核心逻辑回归测试：坐标读数 / 网格粒度 / 渲染分层
// 用最小 DOM 桩直接跑 index.html 里的脚本，不需要浏览器： node _test/coord_test.js
const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync(__dirname + "/../web/index.html", "utf8");
const code = html.match(/<script>([\s\S]*?)<\/script>/)[1];

let baseCalls = 0, topCalls = 0;      // 两个画布的绘制调用计数
const nodes = {};
function mkNode(id) {
  const listeners = {};
  const counter = id === "baseCanvas" ? () => baseCalls++ : (id === "overlayCanvas" ? () => topCalls++ : null);
  const ctxStub = new Proxy({}, { get: (t, k) => (typeof k === "string" ? () => { if (counter) counter(); } : undefined) });
  return nodes[id] = {
    id, textContent: "", innerHTML: "", value: "", checked: true, style: {},
    listeners,
    classList: { add(){}, remove(){}, toggle(c, v){ if(c === 'collapsed') nodes.__collapsed = !!v; }, contains: () => !!nodes.__collapsed },
    addEventListener(t, fn){ (listeners[t] = listeners[t] || []).push(fn); },
    click(){}, getBoundingClientRect: () => ({width:0,height:0,left:0,top:0}),
    clientWidth: 1600, clientHeight: 900,
    getContext: () => ctxStub,
  };
}
["baseCanvas","overlayCanvas","btnLoad","btnDemo","loadInfo","sumProduced","sumRetained","sumPending","sumSafehouse","sumSaveRoot","sumNotice",
 "gridStep","chkMap","chkPop","chkSH","anchorNE","anchorSW","btnFitAll","btnKeepExport","btnKeepImport",
 "btnKeepReset","keepInfo","hudScale","hudFocus","hudCursor","hudCursorBin","hudGrain","hudHelp","tooltip","drop"]
 .forEach(mkNode);
nodes["gridStep"].value = "300";

global.document = { getElementById: (id) => nodes[id] || mkNode(id), createElement: () => mkNode("tmp"), addEventListener(){} };
global.window = { addEventListener(){}, devicePixelRatio: 1, innerWidth: 1600, innerHeight: 900 };
global.location = { protocol: "file:", href: "file:///index.html" };
const imgs = [];
global.Image = function(){ const o = {src:"", naturalWidth:0, onload:null, onerror:null}; imgs.push(o); return o; };
global.alert = () => {};
global.URL.createObjectURL = () => "blob:x";

vm.runInThisContext(code, { filename: "index.html<script>" });

const fire = (id, type, ev) => (nodes[id].listeners[type] || []).forEach(fn => fn(ev));
const move = (x, y) => fire("baseCanvas", "mousemove", {clientX:x, clientY:y, button:0, shiftKey:false, ctrlKey:false, metaKey:false, preventDefault(){}});
const xy = () => nodes["hudCursor"].textContent.split(",").map(Number);
const center = () => nodes["hudFocus"].textContent.split(",").map(Number);

let bad = 0;
const check = (label, ok, extra="") => {
  if(!ok) bad++;
  console.log(`${ok ? "✓" : "✗"} ${label}${ok ? "" : "  " + extra}`);
};

// ---- 结构检查：脚本引用的 id / 标签名 / 画布规则是否和 HTML 对得上 ----
// 曾经把 <canvas> 误改成 <baseCv>：脚本照跑、测试照过，页面却整个白掉。
// 这条只查结构，正好补上"测试从不看标签"的盲区。
{
  const body = html.split("</style>")[1].split("<script>")[0];
  const css = html.split("<style>")[1].split("</style>")[0];

  const idsInHtml = new Set([...body.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]));
  const idsInJs = [...new Set([...code.matchAll(/byId\("([^"]+)"\)/g)].map(m => m[1]))];
  const missing = idsInJs.filter(id => !idsInHtml.has(id));
  check("脚本引用的元素 id 在 HTML 里都存在", missing.length === 0, "缺 " + missing.join(", "));

  const tags = [...new Set([...body.matchAll(/<\/?([A-Za-z][\w-]*)/g)].map(m => m[1]))];
  const weird = tags.filter(t => /[A-Z]/.test(t));
  check("HTML 标签名全是小写（没被改名误伤）", weird.length === 0, "可疑标签 " + weird.join(", "));

  const open = (body.match(/<canvas\b/g) || []).length;
  const close = (body.match(/<\/canvas>/g) || []).length;
  check("两个画布元素开着也闭着", open === 2 && close === 2, `开 ${open} / 闭 ${close}`);

  check("CSS 里有 canvas 的尺寸规则", /(^|[\s,}])canvas\s*\{/.test(css), "缺 canvas 规则，画布会没尺寸");
}

ingest({
  version:1, cell_tiles:8, pad:0, save_root:"测试",
  cells_by_x:{"1500":[[1250,1260]], "1501":[[1250,1260]]},
  safehouses:[{x:12000,y:10000,w:20,h:20,owner:"A",town:"X, KY",name:"",members:[]}],
  protected_cells:{"1500":[[1250,1255]]}, extra_rects:[], summary:{total:22,keep:0,delete:22}
});
check("载入后画布已重绘", nodes["hudFocus"].textContent !== "0,0");

// ---- 坐标读数 ----
move(800, 450);
let [mx, my] = xy();
let [cx, cy] = center();
check("中心点坐标 = 相机中心", Math.abs(mx-cx) <= 1 && Math.abs(my-cy) <= 1, `hudCursor=${mx},${my} center=${cx},${cy}`);
check("区块号 = 世界坐标 ÷ 8", nodes["hudCursorBin"].textContent === `${Math.floor(mx/8)}/${Math.floor(my/8)}`,
      `区块=${nodes["hudCursorBin"].textContent}`);

const zoom = parseFloat(nodes["hudScale"].textContent);
move(960, 450);
check(`右移 160px 对应约 ${Math.round(160/zoom)} 格`, Math.abs((xy()[0]-mx) - 160/zoom) <= 2, `实测 ${xy()[0]-mx}`);

fire("baseCanvas", "mouseleave", {});
check("移出画布后显示 –", nodes["hudCursor"].textContent === "–" && nodes["hudCursorBin"].textContent === "–");

move(800, 450);
fire("baseCanvas", "wheel", {clientX:800, clientY:450, deltaY:-600, preventDefault(){}});
check("缩放后读数跟随新相机中心", Math.abs(xy()[0] - center()[0]) <= 1);

// ---- 选区拖动 ----
fire("baseCanvas", "mousedown", {clientX:800, clientY:450, button:0, shiftKey:true, ctrlKey:false, metaKey:false, preventDefault(){}});
fire("baseCanvas", "mousemove", {clientX:900, clientY:500, button:0, shiftKey:true, ctrlKey:false, metaKey:false, preventDefault(){}});
check("拖动时显示选区范围与尺寸", /→.*×.*格/.test(nodes["hudCursor"].textContent), nodes["hudCursor"].textContent);
fire("baseCanvas", "mouseup", {});
check("松手后恢复为光标坐标", /^\d+,\d+$/.test(nodes["hudCursor"].textContent), nodes["hudCursor"].textContent);
check("松手后保留了一块保护区", nodes["keepInfo"].textContent.includes("1 块"), nodes["keepInfo"].textContent);

// ---- 网格粒度：1 格网格不能把叠加层也变成 1 格 ----
check("默认 300 格网格 → 叠加层 300 格", nodes["hudGrain"].textContent === "300 格", nodes["hudGrain"].textContent);
nodes["gridStep"].value = "8"; nodes["gridStep"].onchange();
check("选 8 格 → 叠加层降到区块级", nodes["hudGrain"].textContent === "区块级", nodes["hudGrain"].textContent);
nodes["gridStep"].value = "1"; nodes["gridStep"].onchange();
check("选 1 格 → 叠加层仍是区块级（不会炸）", nodes["hudGrain"].textContent === "区块级", nodes["hudGrain"].textContent);
nodes["gridStep"].value = "50"; nodes["gridStep"].onchange();
check("选 50 格 → 叠加层 50 格", nodes["hudGrain"].textContent === "50 格", nodes["hudGrain"].textContent);

// ---- 渲染分层：鼠标移动只能重画上层 ----
nodes["gridStep"].value = "300"; nodes["gridStep"].onchange();
baseCalls = 0; topCalls = 0;
for(let i=0;i<40;i++) move(200+i*5, 300);
check("40 次鼠标移动没有重画底层画布", baseCalls === 0, `底层被调用 ${baseCalls} 次`);
check("40 次鼠标移动重画了上层画布", topCalls > 0, `上层 ${topCalls} 次`);

baseCalls = 0;
fire("baseCanvas", "wheel", {clientX:800, clientY:450, deltaY:-200, preventDefault(){}});
check("缩放会重画底层", baseCalls > 0, `底层 ${baseCalls} 次`);

// ---- 收起/展开左侧面板（窗口小时把地方留给地图）----
nodes.__collapsed = false;
nodes["btnToggleSide"].onclick();
check("点按钮能收起面板", nodes.__collapsed === true, `collapsed=${nodes.__collapsed}`);
check("按钮文字变成「展开面板」", nodes["btnToggleSide"].textContent === "展开面板",
      nodes["btnToggleSide"].textContent);
nodes["btnToggleSide"].onclick();
check("再点一次能展开", nodes.__collapsed === false, `collapsed=${nodes.__collapsed}`);
check("按钮文字变回「收起面板」", nodes["btnToggleSide"].textContent === "收起面板",
      nodes["btnToggleSide"].textContent);

// ---- 第 3 阶段：被 exe 内置服务打开时（http），自动拉取结果、按钮改成"保存到清理器" ----
{
  const N3 = {};
  const mk3 = (id) => {
    const L = {};
    return N3[id] = { id, textContent:"", innerHTML:"", value:"", checked:true, style:{}, listeners:L,
      classList:{add(){},remove(){}}, addEventListener(t,f){ (L[t]=L[t]||[]).push(f); },
      click(){}, getBoundingClientRect:()=>({width:0,height:0,left:0,top:0}),
      clientWidth:1600, clientHeight:900,
      getContext:()=>new Proxy({},{get:()=>()=>{}}) };
  };
  ["baseCanvas","overlayCanvas","gridStep","chkMap","chkPop","chkSH","anchorNE","anchorSW","tooltip","drop","keepInfo",
   "app","btnToggleSide","sumProduced","sumRetained","sumPending","sumSafehouse","sumSaveRoot","sumNotice","hudScale","hudFocus","hudCursor",
   "hudCursorBin","hudGrain","hudTiles","hudHelp","loadInfo","btnLoad","btnKeepExport","mastheadHint"].forEach(mk3);
  N3["gridStep"].value = "300";
  const INDEX = { version:1, cell_tiles:8, pad:0, save_root:"served-save",
                  cells_by_x:{"1500":[[1250,1260]]}, safehouses:[], protected_cells:{},
                  extra_rects:[], summary:{total:11, keep:0, delete:11} };
  const sandbox = {
    document: { getElementById: id => N3[id] || mk3(id), createElement: () => mk3("t"), addEventListener(){} },
    window: { addEventListener(){}, devicePixelRatio:1, innerWidth:1600, innerHeight:900 },
    location: { protocol:"http:", href:"http://127.0.0.1:1234/?auto=1" },
    Image: function(){ return {src:"", naturalWidth:0, onload:null, onerror:null}; },
    alert(){}, URL:{ createObjectURL: () => "x" }, console, requestAnimationFrame: f => f(),
    fetch: (url) => Promise.resolve({
      ok: true, status: 200,
      json: () => Promise.resolve(url.startsWith("api/state")
        ? {ok:true, analyzed:true, total:11, delete:11}
        : INDEX),
    }),
    Math, Number, Object, Array, String, JSON, Set, Map, Int32Array, Promise,
  };
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, {filename:"page3"});

  setTimeout(() => {
    check("内置服务模式下自动载入了分析结果", N3["sumProduced"].textContent === "11", `sumProduced=${N3["sumProduced"].textContent}`);
    check("按钮改成「保存到清理器」", N3["btnKeepExport"].textContent === "保存到清理器", N3["btnKeepExport"].textContent);
    check("顶部提示改成已连接", N3["mastheadHint"].innerHTML.includes("内置预览服务"), N3["mastheadHint"].innerHTML.slice(0,40));
    finish();
  }, 60);
}

// ---- 第 2 阶段：瓦片底图（用全新上下文，避免被第一阶段的状态干扰）----
{
  const imgs = [];
  const N = {};
  const drawCalls = [];
  let base2 = 0;
  const mk2 = (id) => {
    const L = {};
    const ctxStub = new Proxy({}, { get: (t,k) => {
      if (k === "drawImage") return (...a) => drawCalls.push(a);
      return () => { if(id==="baseCanvas") base2++; };
    }});
    return N[id] = { id, textContent:"", innerHTML:"", value:"", checked:true, style:{}, listeners:L,
      classList:{add(){},remove(){}}, addEventListener(t,f){ (L[t]=L[t]||[]).push(f); },
      click(){}, getBoundingClientRect:()=>({width:0,height:0,left:0,top:0}),
      clientWidth:1600, clientHeight:900, getContext:()=>ctxStub };
  };
  ["baseCanvas","overlayCanvas","gridStep","chkMap","chkPop","chkSH","anchorNE","anchorSW","tooltip","drop","keepInfo",
   "app","btnToggleSide","sumProduced","sumRetained","sumPending","sumSafehouse","sumSaveRoot","sumNotice","hudScale","hudFocus","hudCursor",
   "hudCursorBin","hudGrain","hudTiles","hudHelp","loadInfo","btnFitAll"].forEach(mk2);
  N["gridStep"].value = "300";
  const sandbox = {
    document: { getElementById: id => N[id] || mk2(id), createElement: () => mk2("t"), addEventListener(){} },
    window: { addEventListener(){}, devicePixelRatio:1, innerWidth:1600, innerHeight:900 },
    location: { protocol:"file:", href:"file:///index.html" },
    Image: function(){ const o = {src:"", naturalWidth:0, onload:null, onerror:null}; imgs.push(o); return o; },
    alert(){}, URL: { createObjectURL: () => "x" }, console, requestAnimationFrame: f => f(),
    Math, Number, Object, Array, String, JSON, Set, Map, Int32Array, Promise,
  };
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, {filename:"page2"});
  sandbox.ingest({version:1, cell_tiles:8, pad:0, save_root:"t",
    cells_by_x:{"1500":[[1250,1260]]}, safehouses:[], protected_cells:{}, extra_rects:[], summary:{}});

  const fire2 = (t, ev) => (N["baseCanvas"].listeners[t]||[]).forEach(f => f(ev));
  const wheel = (d) => fire2("wheel", {clientX:800, clientY:450, deltaY:d, preventDefault(){}});
  const levels = () => imgs.map(o => o.src).filter(u => u.startsWith("tiles/"))
                             .map(u => Number(u.split("/")[1]));
  for(let i=0;i<60;i++) wheel(-200);           // 一路放大到上限
  const lvls = levels();
  const snapL0 = imgs.filter(o => o.src.startsWith("tiles/0/"));   // 全分辨率那一轮请求的块
  check("放大到上限后请求全分辨率瓦片（第 0 级）", lvls.length > 0 && Math.min(...lvls) === 0,
        `级别集合 ${[...new Set(lvls)].sort().join(",")}`);
  check("只请求视野内的瓦片（不是上千块）", lvls.length > 0 && lvls.length <= 60, `实际 ${lvls.length} 块`);
  const urls = imgs.map(o => o.src).filter(u => u.startsWith("tiles/"));
  check("同一块瓦片不重复请求", new Set(urls).size === urls.length, `${urls.length} 次 / ${new Set(urls).size} 个不同`);

  imgs.length = 0;
  for(let i=0;i<90;i++) wheel(200);            // 缩小回全图
  const lvlOut = levels();
  check("缩小后自动换到低级别瓦片", lvlOut.length > 0 && Math.min(...lvlOut) >= 2,
        `级别集合 ${[...new Set(lvlOut)].sort().join(",")}`);

  // 模拟当前级别的一块瓦片加载完成 → 应该被画到底层
  const one = [...imgs].reverse().find(o => o.src.startsWith("tiles/"));
  if(one){
    one.naturalWidth = 512;
    one.naturalHeight = 512;
    one.onload();
    wheel(0);
    check("加载完成的瓦片会被画到底层", /×\d+/.test(N["hudTiles"].textContent), `HUD=${N["hudTiles"].textContent}`);
  } else {
    check("加载完成的瓦片会被画到底层", false, "本轮没有瓦片请求");
  }

  // 边缘的块是不满的（源图切到最后一行/列会剩），必须按实际像素画，不能拉伸
  {
    imgs.length = 0; drawCalls.length = 0;
    for(let i=0;i<80;i++) wheel(-200);              // 回到全分辨率
    const zNow = parseFloat(N["hudScale"].textContent);
    const L = Math.max(0, Math.min(6, Math.round(-Math.log2(Math.max(zNow, 0.002)))));
    // 直接算出"视野左上角那一块"的编号，确保它就是会被画出来的那块
    const step = 512 * Math.pow(2, L);
    const [cxw, cyw] = N["hudFocus"].textContent.split(",").map(Number);
    const tx = Math.floor((cxw - 800/zNow) / step), ty = Math.floor((cyw - 450/zNow) / step);
    const want = `tiles/${L}/${tx}_${ty}.jpg`;
    const t = (L === 0 ? snapL0 : []).find(o => o.src === want);   // 第一批（全分辨率那轮）请求里的
    if(t){
      t.naturalWidth = 341; t.naturalHeight = 25;    // 模拟边缘那块（源图右下角切剩的）
      t.onload();
      drawCalls.length = 0;
      wheel(0);
      const z = parseFloat(N["hudScale"].textContent);
      const call = drawCalls.find(a => a[0] === t);
      if(call){
        const wantW = 341 * z * Math.pow(2, L), wantH = 25 * z * Math.pow(2, L);
        check("不满的瓦片按实际像素画（不被拉伸）",
              Math.abs(call[3] - wantW) < 2 && Math.abs(call[4] - wantH) < 2,
              `画成 ${call[3].toFixed(0)}×${call[4].toFixed(0)} 屏幕px，应为 ${wantW.toFixed(0)}×${wantH.toFixed(0)}（第 ${L} 级、341×25 像素的块）`);
      } else {
        check("不满的瓦片按实际像素画（不被拉伸）", false, `这次重绘没有画该块（L=${L} ${want}）`);
      }
    } else {
      check("不满的瓦片按实际像素画（不被拉伸）", false, `没找到 ${want}`);
    }
  }
}

// 第 3 阶段的检查是异步的（等 fetch 的 Promise），所以统一在 finish() 里收尾
function finish(){
  console.log(bad ? `\n有 ${bad} 项不符` : "\n全部通过");
  process.exit(bad ? 1 : 0);
}
