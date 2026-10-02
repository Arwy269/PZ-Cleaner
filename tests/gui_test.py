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

app.destroy()
shutil.rmtree(root, ignore_errors=True)
(eng.ensure_web_dir() / "save_index.json").unlink(missing_ok=True)
cfg = Path(HERE.parent / "config.json")
if cfg.exists(): cfg.unlink()          # 测试写出的配置别留下

print()
print("有 %d 项不符" % bad if bad else "全部通过")
sys.exit(1 if bad else 0)
