#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图形界面无头测试：把「分析 → 统计 → 删除」整条链路跑一遍，不弹窗。
用法： python _test/gui_test.py
"""
import sys, time, shutil, traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 管道下也要能输出 ✓✗
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))   # 仓库根：import gui / pz_chunk_cleaner
sys.path.insert(0, str(HERE))          # tests/：import fixture

import tkinter as tk
import gui
import pz_chunk_cleaner as eng
from fixture import make_save, default_safehouses

bad = 0
def check(label, ok, extra=""):
    global bad
    if not ok: bad += 1
    print(("✓ " if ok else "✗ ") + label + ("" if ok else "  " + str(extra)))

# ---- 造一个样例存档（不依赖任何外部文件）
root = HERE / "_gui_save"
make_save(root, bins=[(1225, 1135), (1226, 1136), (1000, 1000), (1000, 1001), (999, 500)])
m = root / "map"

app = gui.App()
app.withdraw()
app.var_save.set(str(root))
app.var_backup.set("")            # 不备份

def wait(pred, secs=25):
    t0 = time.time()
    while time.time() - t0 < secs:
        app.update()
        app._pump()
        if pred(): return True
        time.sleep(0.05)
    return False

# ---- 分析
app._on_scan()
ok = wait(lambda: app.res is not None or (not app.busy and app.lbl_status.cget("text") == "出错"))
check("分析完成没报错", app.lbl_status.cget("text") != "出错", app.lbl_status.cget("text"))
res = app.res
check("拿到分析结果", res is not None)
if res:
    check("区块总数统计正确", app.lbl_stats["total"].cget("text") == f"{res['total_bins']:,}",
          f"{app.lbl_stats['total'].cget('text')} vs {res['total_bins']}")
    check("待删除统计正确", app.lbl_stats["del"].cget("text") == f"{res['del_count']:,}",
          f"{app.lbl_stats['del'].cget('text')} vs {res['del_count']}")
    check("安全屋数量正确", app.lbl_stats["sh"].cget("text") == f"{len(res['safehouses']):,}")
    check("安全屋列表已填充", len(app.tree.get_children()) == len(res["safehouses"]),
          f"{len(app.tree.get_children())} 行 vs {len(res['safehouses'])}")
    check("体积显示与实际字节数一致", app.lbl_stats["size"].cget("text") == eng.human_size(res["total_bytes"]),
          f"{app.lbl_stats['size'].cget('text')} vs {eng.human_size(res['total_bytes'])}")
    check("「执行清理」按钮已解禁", "disabled" not in app.btn_go.state())
    check("预览索引已写出", (eng.ensure_web_dir() / "save_index.json").is_file())

    # ---- 确认框：输错词应该不通过（不弹窗，直接测判定逻辑）
    dlg_done = {}
    def fake_confirm(res_, backup, word):
        dlg_done["called"] = (res_["del_count"], backup, word)
        return False
    app._confirm_dialog = fake_confirm
    app._on_delete()
    check("取消确认后不会删除", not app.busy and len(list(m.rglob("*.bin"))) == 5,
          f"剩余 {len(list(m.rglob('*.bin')))} 个文件")
    check("确认框收到了正确的删除数量", dlg_done.get("called", (None,))[0] == res["del_count"], dlg_done)

    # ---- 真正执行（跳过确认框 + 跳过进程检测）
    app.var_check.set(False)
    app._confirm_dialog = lambda *a: True
    app._on_delete()
    ok = wait(lambda: app.lbl_status.cget("text") in ("清理完成", "出错"), 40)
    check("删除流程跑完", app.lbl_status.cget("text") == "清理完成", app.lbl_status.cget("text"))
    left = sorted(p.name for p in m.rglob("*.bin"))
    check("只保留了安全屋范围内的区块", left == ["1135.bin", "1136.bin"], f"剩下 {left}")
    check("删除后按钮重新禁用（要重新分析）", "disabled" in app.btn_go.state())

# ---- 内置网页服务：点「打开网页预览」应该起服务并给出带 auto=1 的地址 ----
import json as _json
import urllib.request
import gui as gui_mod

app._on_scan()                                            # 先分析（删完之后 res 已清空）
wait(lambda: app.res is not None and not app.busy)
res2 = app.res
check("重新分析成功（预览的前置条件）", res2 is not None)
# 注意：不调 app._open_web()——那会切到地图页并把 WebView2 真的创建出来（弹窗）。
# 只验证服务这一层，嵌入那层由 --selftest-embed 单独跑。
url = app._ensure_server()
check("预览服务能起来", app.server is not None and app.server.port > 0,
      getattr(app.server, "port", None))
check("预览地址带 auto=1（网页自己拉数据）", url.endswith("/?auto=1"), url)
if app.server:
    with urllib.request.urlopen(app.server.url + "/", timeout=5) as r:
        page = r.read().decode("utf-8", "replace")
    check("服务能取到网页", "区块清理" in page, page[:40])
    with urllib.request.urlopen(app.server.url + "/api/state", timeout=5) as r:
        st = _json.loads(r.read())
    check("状态接口带着当前分析结果",
          bool(st.get("ok")) and st.get("analyzed") and st.get("total") == (res2 or {}).get("total_bins"),
          {k: st.get(k) for k in ("ok", "total", "delete", "analyzed")})

    # 网页把框选的保护区存回 exe
    keep_file = eng.base_dir() / "keep.json"
    keep_file.unlink(missing_ok=True)
    req = urllib.request.Request(app.server.url + "/api/keep",
                                 data=b'{"rects":[[9800,9000,10000,9200]]}',
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        j = _json.loads(r.read())
    check("网页能把保护区存回 exe", j.get("ok") and keep_file.is_file(), j)
    if keep_file.is_file():
        check("存回的 keep.json 格式正确",
              _json.loads(keep_file.read_text(encoding="utf-8"))["rects"] == [[9800, 9000, 10000, 9200]])
        keep_file.unlink()

# ---- 中文名的安全屋（多字节屋主 / 安全屋名 / 成员）
# 屋主字符串同时是记录的定位签名，成员名单也按字符集过滤——这几处以前都只认 ASCII，
# 中文名会被整条丢掉，而安全屋丢了就等于不保护，会被当成普通区块删掉。
import fixture as _fx
cn_root = HERE / "_cn_save"
_fx.make_save(cn_root, bins=[(1225, 1135), (1226, 1136)],
              safehouses=_fx.chinese_safehouses())
cn, _w = eng.parse_safehouses(cn_root / "map_meta.bin")
check("中文屋主的安全屋能被解析出来", len(cn) == 2, f"解析出 {len(cn)} 条")
if cn:
    check("中文屋主完整读出", cn[0]["owner"] == "中文玩家", cn[0].get("owner"))
    check("中文安全屋名完整读出", cn[0]["name"] == "我的基地", cn[0].get("name"))
    check("中文成员名单完整读出", cn[0]["members"] == ["小明", "老王"], cn[0].get("members"))
    check("中文安全屋的城镇照常识别", cn[0]["town"] == "Muldraugh, KY", cn[0].get("town"))

# 端到端：中文安全屋必须真的起到保护作用
cn_res = eng.analyze(cn_root, 0, eng.base_dir() / "keep.json", False)
check("中文安全屋被算进保护区", cn_res is not None and cn_res["kept_count"] >= 2,
      f"保留 {cn_res['kept_count'] if cn_res else '?'} 个区块")
shutil.rmtree(cn_root, ignore_errors=True)

# ---- 屋主列表长度为 0 的安全屋
# PZ 对这种只写一遍屋主名（不重复），靠"屋主出现两次"的签名完全匹配不到。
# 刚建好、还没填成员的安全屋就是这种形态；漏掉它 = 这个安全屋不会被保护。
solo_root = HERE / "_solo_save"
solo_root.mkdir(parents=True, exist_ok=True)
(solo_root / "map_meta.bin").write_bytes(_fx.build_map_meta_solo([
    (9492, 3330, 105, 94, "2333smiles", "我是测试的名字", "Riverside, KY"),
]))
solo, _w = eng.parse_safehouses(solo_root / "map_meta.bin")
check("屋主列表长度为 0 的安全屋也能解析出来", len(solo) == 1, f"解析出 {len(solo)} 条")
if solo:
    check("  屋主正确", solo[0]["owner"] == "2333smiles", solo[0].get("owner"))
    check("  名字正确", solo[0]["name"] == "我是测试的名字", solo[0].get("name"))
    check("  尺寸正确", (solo[0]["w"], solo[0]["h"]) == (105, 94),
          f"{solo[0].get('w')}×{solo[0].get('h')}")
    check("  城镇正确", solo[0]["town"] == "Riverside, KY", solo[0].get("town"))
shutil.rmtree(solo_root, ignore_errors=True)

# ---- map\ 里一个区块都没有（新存档 / 刚重置过）
# 以前这种情况会在解析 map_meta.bin 之前就返回，界面上一个安全屋都看不到。
empty_root = HERE / "_empty_save"
_fx.make_save(empty_root, bins=[])
empty_res = eng.analyze(empty_root, 0, eng.base_dir() / "keep.json", False)
check("map 目录为空时照样解析出安全屋",
      empty_res is not None and len(empty_res["safehouses"]) == 2,
      f"{len(empty_res['safehouses']) if empty_res else '返回 None'} 个")
check("map 目录为空时待删除数为 0", bool(empty_res) and empty_res["del_count"] == 0)
shutil.rmtree(empty_root, ignore_errors=True)

app.destroy()
shutil.rmtree(root, ignore_errors=True)
(eng.ensure_web_dir() / "save_index.json").unlink(missing_ok=True)
cfg = Path(HERE.parent / "config.json")
if cfg.exists(): cfg.unlink()          # 测试写出的配置别留下

print()
print("有 %d 项不符" % bad if bad else "全部通过")
sys.exit(1 if bad else 0)
