// 验证「实时坐标」读数：用最小 DOM 桩跑 index.html 的脚本，模拟鼠标事件后检查 HUD 文本
const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync(__dirname + "/../web/index.html", "utf8");
const code = html.match(/<script>([\s\S]*?)<\/script>/)[1];

const nodes = {};
function mkNode(id) {
  const listeners = {};
  return nodes[id] = {
    id, textContent: "", innerHTML: "", value: "", checked: true, style: {},
    listeners,
    classList: { add(){}, remove(){} },
    addEventListener(t, fn){ (listeners[t] = listeners[t] || []).push(fn); },
    click(){}, getBoundingClientRect: () => ({width:0,height:0,left:0,top:0}),
    clientWidth: 1600, clientHeight: 900,
    getContext: () => new Proxy({}, { get: () => () => {} }),
  };
}
["c","btnLoad","btnDemo","loadInfo","statTotal","statKeep","statDel","statSh","statSave","statWarn",
 "gridSize","chkMap","chkPop","chkSH","lockTR","lockBL","btnResetView","btnSaveKeep","btnLoadKeep",
 "btnClearKeep","keepInfo","hudZoom","hudCenter","hudXY","hudBin","hudFine","hudTip","tooltip","drop"]
 .forEach(mkNode);
nodes["gridSize"].value = "300";

global.document = { getElementById: (id) => nodes[id] || mkNode(id), createElement: () => mkNode("tmp"), addEventListener(){} };
global.window = { addEventListener(){}, devicePixelRatio: 1, innerWidth: 1600, innerHeight: 900 };
global.Image = function(){ return {}; };
global.alert = () => {};
global.URL.createObjectURL = () => "blob:x";

vm.runInThisContext(code, { filename: "index.html<script>" });

const fire = (id, type, ev) => (nodes[id].listeners[type] || []).forEach(fn => fn(ev));
const move = (x, y) => fire("c", "mousemove", {clientX:x, clientY:y, button:0, shiftKey:false, ctrlKey:false, metaKey:false, preventDefault(){}});
const xy = () => nodes["hudXY"].textContent.split(",").map(Number);
const center = () => nodes["hudCenter"].textContent.split(",").map(Number);

let bad = 0;
const check = (label, ok, extra="") => {
  if(!ok) bad++;
  console.log(`${ok ? "✓" : "✗"} ${label}${ok ? "" : "  " + extra}`);
};

ingest({
  version:1, bin_tile_size:8, pad_tiles:0, save_root:"测试",
  bins_by_x:{"1500":[[1250,1260]]},
  safehouses:[{x:12000,y:10000,w:20,h:20,owner:"A",town:"X, KY",name:"",members:[]}],
  protected_bins:{}, extra_rects:[], summary:{total:11,keep:0,delete:11}
});
check("载入后画布已重绘（HUD 已刷新）", nodes["hudCenter"].textContent !== "0,0" && nodes["hudCenter"].textContent !== "");

// 1) 画面正中央 → 世界坐标 = 相机中心
move(800, 450);
let [mx, my] = xy();
let [cx, cy] = center();
check("中心点坐标 = 相机中心 x", Math.abs(mx - cx) <= 1, `hudXY=${mx},${my} hudCenter=${cx},${cy}`);
check("中心点坐标 = 相机中心 y", Math.abs(my - cy) <= 1, `hudXY=${mx},${my} hudCenter=${cx},${cy}`);
check("区块号 = 世界坐标 ÷ 8", nodes["hudBin"].textContent === `${Math.floor(mx/8)}/${Math.floor(my/8)}`,
      `区块=${nodes["hudBin"].textContent} 坐标=${mx},${my}`);

// 两次读数都是向下取整，差值允许 ±1 的取整误差（zoom 显示只保留两位小数，再放宽 1）
const zoom = parseFloat(nodes["hudZoom"].textContent);
move(960, 450);
let [mx2] = xy();
check(`右移 160px (zoom=${zoom}) 对应约 ${Math.round(160/zoom)} 格`, Math.abs((mx2 - mx) - 160/zoom) <= 2, `实测增量 ${mx2-mx}`);

// 3) 移出画布 → 清空
fire("c", "mouseleave", {});
check("鼠标移出画布后显示 –", nodes["hudXY"].textContent === "–" && nodes["hudBin"].textContent === "–");

// 4) 滚轮缩放后不移动鼠标，读数也要跟着刷新
move(800, 450);
fire("c", "wheel", {clientX:800, clientY:450, deltaY:-600, preventDefault(){}});
let [ax] = xy(); let [ncx] = center();
check("缩放后读数跟随新相机中心", Math.abs(ax - ncx) <= 1, `hudXY=${ax} hudCenter=${ncx}`);

// 5) 拖动保护区时显示选区范围
fire("c", "mousedown", {clientX:800, clientY:450, button:0, shiftKey:true, ctrlKey:false, metaKey:false, preventDefault(){}});
fire("c", "mousemove", {clientX:900, clientY:500, button:0, shiftKey:true, ctrlKey:false, metaKey:false, preventDefault(){}});
check("拖动时显示选区范围与尺寸", /→.*×.*格/.test(nodes["hudXY"].textContent), `实际=${nodes["hudXY"].textContent}`);
fire("c", "mouseup", {});
check("松手后恢复为光标坐标", /^\d+,\d+$/.test(nodes["hudXY"].textContent), `实际=${nodes["hudXY"].textContent}`);
check("松手后保留了一块保护区", nodes["keepInfo"].textContent.includes("1 块"), nodes["keepInfo"].textContent);

console.log(bad ? `\n有 ${bad} 项不符` : "\n全部通过");
process.exit(bad ? 1 : 0);
