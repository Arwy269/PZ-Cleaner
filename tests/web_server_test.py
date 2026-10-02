#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内置网页服务的测试：起服务 → 取页面/结果/状态 → POST 保护区 → 关掉。
用法： python tests/web_server_test.py
"""
import json
import shutil
import sys
import tempfile
import urllib.request
import urllib.error
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import web_server

bad = 0
def check(label, ok, extra=""):
    global bad
    if not ok: bad += 1
    print(("✓ " if ok else "✗ ") + label + ("" if ok else "  " + str(extra)))

tmp = Path(tempfile.mkdtemp(prefix="pzweb_"))
web = tmp / "web"; web.mkdir()
(web / "index.html").write_text("<html>ok</html>", encoding="utf-8")
(web / "save_index.json").write_text(json.dumps({"summary": {"total": 42}}), encoding="utf-8")
keep = tmp / "keep.json"

got = {}
srv = web_server.WebServer(web, keep, on_keep=lambda p, n: got.update(path=p, count=n))
url = srv.start()
check("服务起在 127.0.0.1 上", url.startswith("http://127.0.0.1:"), url)

def get(path):
    with urllib.request.urlopen(url + path, timeout=5) as r:
        return r.status, r.read()

try:
    st, body = get("/")
    check("能取到网页", st == 200 and b"ok" in body, f"{st} {body[:40]}")

    st, body = get("/save_index.json")
    check("能取到分析结果", st == 200 and json.loads(body)["summary"]["total"] == 42, body[:60])

    st, body = get("/api/state")
    state = json.loads(body)
    check("状态接口可用", st == 200 and state["ok"] is True and state["ready"] is False, state)

    srv.set_state(total=42, delete=40, keep=2, safehouses=3, analyzed=True, save="X:/save")
    st, body = get("/api/state")
    state = json.loads(body)
    check("分析后状态能刷新", state["analyzed"] and state["total"] == 42 and state["safehouses"] == 3, state)

    # POST 保护区
    payload = json.dumps({"rects": [[10, 20, 30, 40], [1.9, 2.1, 3.5, 4.4], ["坏数据"], [5, 6, 7, 8]]}).encode()
    req = urllib.request.Request(url + "/api/keep", data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        res = json.loads(r.read())
    check("POST 保护区返回成功", res["ok"] is True and res["count"] == 3, res)
    check("keep.json 已写到指定位置", keep.is_file(), keep)
    saved = json.loads(keep.read_text(encoding="utf-8"))
    check("坐标被规整成整数", saved["rects"] == [[10, 20, 30, 40], [1, 2, 3, 4], [5, 6, 7, 8]], saved["rects"])
    check("回调收到了块数", got.get("count") == 3 and got.get("path") == keep, got)

    # 错误路径
    try:
        req = urllib.request.Request(url + "/api/keep", data=b'{"nope":1}',
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5)
        check("缺 rects 时应报错", False, "居然成功了")
    except urllib.error.HTTPError as e:
        check("缺 rects 时返回 400", e.code == 400, e.code)

    try:
        get("/../pz_chunk_cleaner.py")
        check("不能读到 web 目录之外", False, "读到了")
    except urllib.error.HTTPError as e:
        check("不能读到 web 目录之外", e.code in (403, 404), e.code)
finally:
    srv.stop()

# 关掉后应该连不上了
try:
    get("/")
    check("停止后服务不再响应", False, "还能连上")
except Exception:
    check("停止后服务不再响应", True)

shutil.rmtree(tmp, ignore_errors=True)
print()
print(f"有 {bad} 项不符" if bad else "全部通过")
sys.exit(1 if bad else 0)
