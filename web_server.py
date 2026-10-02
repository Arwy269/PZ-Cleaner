#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内置网页服务
============
把 web\\ 目录和当前的分析结果通过本机 HTTP 提供给浏览器，这样网页预览就不用
再去管 web 文件夹在哪、也不用手动选 save_index.json：

    GET  /                 网页预览（index.html）
    GET  /save_index.json  当前分析结果（每次分析后刷新）
    GET  /api/state        当前状态（存档路径、计数、是否有结果）
    POST /api/keep         接收网页框选的额外保护区，写成本目录的 keep.json
    GET  /tiles/...        瓦片、底图等静态文件

只监听 127.0.0.1，端口自动挑空闲的。纯标准库，无第三方依赖。
"""

from __future__ import annotations

import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional


class _Handler(SimpleHTTPRequestHandler):
    server_version = "PZChunkCleaner"

    # 默认会往 stderr 刷访问日志，窗口程序不需要
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass

    def end_headers(self) -> None:
        # 结果文件每次都变，别让浏览器缓存住
        p = self.path.split("?")[0]
        if p.endswith(".json") or p.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        p = self.path.split("?")[0]
        srv: "WebServer" = self.server.app  # type: ignore[attr-defined]
        if p == "/api/state":
            self._json(srv.state())
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        p = self.path.split("?")[0]
        srv: "WebServer" = self.server.app  # type: ignore[attr-defined]
        if p != "/api/keep":
            self._json({"ok": False, "error": "未知接口"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw.decode("utf-8"))
            rects = data.get("rects") if isinstance(data, dict) else data
            if not isinstance(rects, list):
                raise ValueError("缺少 rects 数组")
            out, n = srv.save_keep(rects)
            self._json({"ok": True, "path": str(out), "count": n})
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": str(e)}, 400)


class WebServer:
    """只监听本机的预览服务。端口传 0 让系统自动挑。"""

    def __init__(self, web_dir: Path, keep_path: Path,
                 on_keep: Optional[Callable[[Path, int], None]] = None,
                 port: int = 0):
        self.web_dir = Path(web_dir)
        self.keep_path = Path(keep_path)
        self.on_keep = on_keep
        self._state: Dict[str, Any] = {"ready": False, "save": "", "total": 0,
                                       "keep": 0, "delete": 0, "safehouses": 0}
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port = port

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> str:
        """启动服务，返回可访问的根地址（如 http://127.0.0.1:54321）。"""
        if self._httpd is not None:
            return self.url
        handler = lambda *a, **kw: _Handler(*a, directory=str(self.web_dir), **kw)  # noqa: E731
        httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        httpd.app = self            # type: ignore[attr-defined]
        httpd.daemon_threads = True
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(target=httpd.serve_forever, name="web-preview", daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._httpd is None:
            return
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass
        self._httpd = None
        self._thread = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ---------------------------------------------------------------- 数据
    def set_state(self, **kw: Any) -> None:
        """分析完成后刷新状态（/api/state 用）。"""
        self._state.update(kw)
        self._state["ready"] = True

    def state(self) -> Dict[str, Any]:
        return {"ok": True, **self._state, "url": self.url}

    def save_keep(self, rects: list) -> "tuple[Path, int]":
        """把网页框选的保护区写成本目录的 keep.json（清理器下次分析会自动读）。"""
        clean = []
        for r in rects:
            if isinstance(r, (list, tuple)) and len(r) >= 4:
                try:
                    clean.append([int(float(v)) for v in r[:4]])
                except (TypeError, ValueError):
                    continue
        payload = {
            "version": 1,
            "说明": "额外保护区，世界方格坐标 [x1,y1,x2,y2]，由网页预览保存",
            "rects": clean,
        }
        self.keep_path.parent.mkdir(parents=True, exist_ok=True)
        self.keep_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.on_keep:
            self.on_keep(self.keep_path, len(clean))
        return self.keep_path, len(clean)
