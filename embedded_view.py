#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把网页嵌进程序自己的窗口（Windows / WebView2）
=============================================
用 pywebview 自带的 WebView2 .NET 程序集，把浏览器控件挂成 Tk 控件的子窗口，
这样地图预览就在主窗口里，不再弹浏览器。

踩过的坑，都写在这儿免得再踩：
  1. **必须在 STA 线程上创建**，且该线程要有消息循环 → 用 .NET 的 Thread 起，
     在它上面跑 WinForms.Application.Run。
  2. **必须显式释放**：不释放的话 Chromium 子进程会变成孤儿（每个 100+ MB，
     攒几个能把机器拖卡）。dispose() 会 Dispose 控件、关 Form，让消息循环退出。
  3. 用户数据目录不能和别人共用，被占用会报 0x800700AA（资源在使用中）。
  4. 子窗口不会被 Tk 自动隐藏：切走标签页时要自己 set_visible(False)。

对外的接口很小：构造 → set_bounds / set_visible / navigate → dispose。
任何一步失败都抛 EmbedError，调用方据此退回"用外部浏览器打开"。
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from pathlib import Path
from typing import Optional

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010


class EmbedError(RuntimeError):
    """嵌入失败（缺依赖、初始化超时、WebView2 起不来等）。"""


class EmbeddedBrowser:
    """把一个 WebView2 控件嵌进指定的 Tk 控件。"""

    def __init__(self, parent_hwnd: int, data_dir: Path, url: str = "about:blank",
                 timeout: float = 30.0):
        self.parent_hwnd = int(parent_hwnd)
        self.data_dir = Path(data_dir)
        self._url = url
        self._form = None
        self._wv = None
        self._thread = None
        self._py_tid: Optional[int] = None
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._disposed = False
        self._visible = True
        self._bounds = (0, 0, 100, 100)
        self._start(timeout)

    # ------------------------------------------------------------------ 启动
    def _start(self, timeout: float) -> None:
        try:
            import clr  # pythonnet
            clr.AddReference("System.Windows.Forms")
            from webview.util import interop_dll_path

            clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.Core.dll"))
            clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.WinForms.dll"))
            # WebView2Loader.dll 是原生库，得让它在 PATH 里找得到
            for plat in ("win-arm64", "win-x64", "win-x86"):
                os.environ["Path"] = os.environ.get("Path", "") + ";" + interop_dll_path(plat)
            from System.Threading import ApartmentState, Thread, ThreadStart
        except Exception as e:  # noqa: BLE001
            raise EmbedError(f"加载 WebView2 组件失败：{e}") from e

        self.data_dir.mkdir(parents=True, exist_ok=True)
        t = Thread(ThreadStart(self._worker))
        t.SetApartmentState(ApartmentState.STA)      # WebView2 要求 STA
        t.IsBackground = True
        t.Start()
        self._thread = t

        if not self._ready.wait(timeout):
            self.dispose()
            raise EmbedError("WebView2 启动超时（30 秒没就绪）")
        if self._error:
            self.dispose()
            raise EmbedError(self._error)

    def _worker(self) -> None:
        """跑在 STA 线程上：建 Form → 挂到 Tk → 跑消息循环。"""
        self._py_tid = threading.get_ident()
        try:
            import System.Windows.Forms as WinForms
            from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties, WebView2
            from System import Uri

            form = WinForms.Form()
            form.FormBorderStyle = getattr(WinForms.FormBorderStyle, "None")   # 无边框
            form.ShowInTaskbar = False
            form.TopLevel = False
            wv = WebView2()
            wv.Dock = WinForms.DockStyle.Fill
            props = CoreWebView2CreationProperties()
            props.UserDataFolder = str(self.data_dir)   # 独立目录，别和别人共用
            wv.CreationProperties = props
            form.Controls.Add(wv)
            form.Show()
            # 关键一步：把窗口挂到 Tk 控件底下，变成子窗口
            ctypes.windll.user32.SetParent(int(form.Handle.ToInt64()), self.parent_hwnd)
            self._form, self._wv = form, wv
            self._apply_bounds()
            wv.CoreWebView2InitializationCompleted += self._on_init
            wv.Source = Uri(self._url)
            WinForms.Application.Run(form)      # 消息循环（关闭 Form 后返回）
        except Exception as e:  # noqa: BLE001
            self._error = f"{type(e).__name__}: {e}"
            self._ready.set()

    def _on_init(self, sender, args) -> None:
        if args.IsSuccess:
            self._apply_bounds()
            self._apply_visible()
        else:
            self._error = f"WebView2 初始化失败：{args.InitializationException}"
            try:
                self._form.Close()
            except Exception:
                pass
        self._ready.set()

    # ------------------------------------------------------------------ 线程内操作
    def _invoke(self, fn) -> None:
        """把操作丢到 STA 线程上执行（Tk 线程不能直接碰 WinForms 控件）。"""
        if self._form is None:
            return
        if threading.get_ident() == self._py_tid:
            fn()
            return
        try:
            from System import Action

            self._form.Invoke(Action(fn))
        except Exception:
            pass

    def _apply_bounds(self) -> None:
        if self._form is None:
            return
        x, y, w, h = self._bounds
        try:
            ctypes.windll.user32.SetWindowPos(
                int(self._form.Handle.ToInt64()), 0,
                int(x), int(y), max(1, int(w)), max(1, int(h)),
                SWP_NOZORDER | SWP_NOACTIVATE)
        except Exception:
            pass

    def _apply_visible(self) -> None:
        if self._form is None:
            return
        try:
            self._form.Visible = bool(self._visible)
        except Exception:
            pass

    # ------------------------------------------------------------------ 对外接口
    @property
    def alive(self) -> bool:
        return self._form is not None and not self._disposed and self._error is None

    def set_bounds(self, x: int, y: int, w: int, h: int) -> None:
        """相对父控件的坐标（子窗口坐标是相对父窗口客户区的）。"""
        self._bounds = (x, y, w, h)
        self._invoke(self._apply_bounds)

    def set_visible(self, visible: bool) -> None:
        self._visible = bool(visible)
        self._invoke(self._apply_visible)

    def navigate(self, url: str) -> None:
        self._url = url

        def go():
            try:
                from System import Uri

                self._wv.Source = Uri(url)
            except Exception:
                pass

        self._invoke(go)

    def reload(self) -> None:
        def do():
            try:
                self._wv.CoreWebView2.Reload()
            except Exception:
                pass

        self._invoke(do)

    def dispose(self) -> None:
        """释放：Dispose 控件 + 关 Form（消息循环随之退出），避免留下 Chromium 孤儿进程。"""
        if self._disposed:
            return
        self._disposed = True

        def teardown():
            try:
                if self._wv is not None:
                    self._wv.Dispose()
            except Exception:
                pass
            try:
                if self._form is not None:
                    self._form.Close()
            except Exception:
                pass

        self._invoke(teardown)
        # 等一下 STH 线程把消息循环收尾
        t = self._thread
        if t is not None:
            try:
                if not t.Join(3000):
                    # 兜底：实在退不掉就让它随进程走，别卡住界面
                    pass
            except Exception:
                pass
        self._thread = None
        self._form = None
        self._wv = None
