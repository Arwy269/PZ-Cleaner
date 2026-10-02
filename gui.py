#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
僵毁区块清理器 — 图形界面
=========================
Project Zomboid B42 存档清理：删除安全屋以外的所有区块文件，
服务器重启后这些区域按地图包重新生成（地形/建筑/战利品/丧尸），
玩家的安全屋、基地、箱子原样保留。

界面只是外壳，"扫描 / 解析安全屋 / 计算保护范围 / 删除" 全部复用
pz_chunk_cleaner.py 里那套逻辑（命令行模式也还在，见 README）。

依赖：Python 自带 tkinter，无第三方库。
"""

from __future__ import annotations

import queue
import sys
import time
import threading
import traceback
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pz_chunk_cleaner as eng


def _dpi_aware() -> None:
    """高分屏下不发虚。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("僵毁区块清理器  ·  Project Zomboid B42")
        self.geometry("920x720")
        self.minsize(780, 580)

        self.cfg: Dict[str, Any] = eng.load_config()
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.res: Optional[Dict[str, Any]] = None      # analyze() 的结果
        self.busy = False
        self.last_plan_key: Optional[tuple] = None

        self._init_style()
        self._build()
        self._load_defaults()

        eng.set_log_sink(lambda m: self.q.put(("log", m)))
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._pump)

    # ------------------------------------------------------------------ 样式
    # 注意：Windows 原生主题（vista）不理会 ttk 控件的自定义背景色，
    # 硬套深色会变成"白底白字"。这里就用系统默认的浅色外观，
    # 只有 tk 原生控件（Label/Text/Frame）才设颜色，它们一定是准的。
    BG_CARD = "#f1f5f9"          # 统计卡片底色
    FG_TITLE = "#334155"         # 小标题
    FG_DIM = "#4b5563"           # 说明文字
    FG_LOG = "#1e293b"           # 日志文字

    def _init_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        ui = ("Microsoft YaHei UI", 9)
        self.option_add("*Font", ui)
        self.BG_WIN = self.cget("bg")      # 系统窗口底色，tk 控件显式套用，免得和 ttk 有细微色差
        style.configure(".", font=ui)
        style.configure("TButton", padding=(10, 5))
        style.configure("Treeview", rowheight=23, font=ui)
        style.configure("Treeview.Heading", font=ui)

    # ------------------------------------------------------------------ 布局
    def _build(self) -> None:
        pad = {"padx": 10, "pady": 4}
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)

        # —— 存档路径
        f1 = ttk.Frame(root); f1.grid(row=0, column=0, sticky="ew")
        f1.columnconfigure(1, weight=1)
        ttk.Label(f1, text="存档路径").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.var_save = tk.StringVar()
        self.ent_save = ttk.Entry(f1, textvariable=self.var_save)
        self.ent_save.grid(row=0, column=1, sticky="ew")
        ttk.Button(f1, text="浏览…", command=self._pick_save).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(f1, text="自动查找", command=self._auto_save).grid(row=0, column=3, padx=(6, 0))
        tk.Label(f1, text="（存档根目录，也就是含 map\\ 的那一层）",
                 fg=self.FG_DIM).grid(row=1, column=1, sticky="w", pady=(2, 0))

        # —— 选项
        f2 = ttk.Frame(root); f2.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        f2.columnconfigure(6, weight=1)
        ttk.Label(f2, text="安全屋外扩").grid(row=0, column=0, sticky="w")
        self.var_pad = tk.IntVar(value=int(self.cfg.get("pad_tiles") or 0))
        sp = ttk.Spinbox(f2, from_=0, to=eng.MAX_SAFEHOUSE_PAD, width=6, textvariable=self.var_pad,
                         command=self._invalidate)
        sp.grid(row=0, column=1, padx=(6, 2))
        sp.bind("<KeyRelease>", lambda e: self._invalidate())
        ttk.Label(f2, text="格").grid(row=0, column=2, sticky="w")
        self.var_check = tk.BooleanVar(value=bool(self.cfg.get("check_server_process", True)))
        ttk.Checkbutton(f2, text="删除前检测服务器进程", variable=self.var_check).grid(row=0, column=3, padx=(18, 0))

        ttk.Label(f2, text="备份目录").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.var_backup = tk.StringVar(value=str(self.cfg.get("backup_root") or ""))
        self.ent_backup = ttk.Entry(f2, textvariable=self.var_backup)
        self.ent_backup.grid(row=1, column=1, columnspan=4, sticky="ew", padx=(6, 0), pady=(8, 0))
        ttk.Button(f2, text="浏览…", command=self._pick_backup).grid(row=1, column=5, padx=(6, 0), pady=(8, 0))
        tk.Label(f2, text="留空 = 不备份（删除后无法恢复）", bg=self.BG_WIN, fg=self.FG_DIM,
                 ).grid(row=2, column=1, columnspan=5, sticky="w", pady=(2, 0))

        # —— 按钮
        f3 = ttk.Frame(root); f3.grid(row=2, column=0, sticky="ew", pady=(12, 6))
        self.btn_scan = ttk.Button(f3, text="① 分析", command=self._on_scan)
        self.btn_scan.pack(side="left")
        self.btn_web = ttk.Button(f3, text="打开网页预览", command=self._open_web)
        self.btn_web.pack(side="left", padx=8)
        self.btn_go = ttk.Button(f3, text="② 执行清理", command=self._on_delete)
        self.btn_go.pack(side="left")
        self.btn_go.state(["disabled"])

        # —— 统计卡片
        card = tk.Frame(root, bg=self.BG_CARD, highlightthickness=1,
                        highlightbackground="#cbd5e1", padx=14, pady=10)
        card.grid(row=3, column=0, sticky="ew", pady=(4, 8))
        for i in range(5):
            card.columnconfigure(i, weight=1)
        self.lbl_stats = {}
        for i, (key, text, color) in enumerate([
                ("total", "已生成区块", "#1d4ed8"), ("keep", "保留（安全屋）", "#15803d"),
                ("del", "将被删除", "#b91c1c"), ("sh", "安全屋", "#334155"),
                ("size", "存档体积", "#334155")]):
            tk.Label(card, text=text, bg=self.BG_CARD, fg=self.FG_DIM).grid(row=0, column=i)
            v = tk.Label(card, text="—", bg=self.BG_CARD, fg=color,
                         font=("Microsoft YaHei UI", 15, "bold"))
            v.grid(row=1, column=i, pady=(2, 0))
            self.lbl_stats[key] = v

        # —— 安全屋列表
        mid = ttk.Frame(root); mid.grid(row=4, column=0, sticky="nsew")
        mid.columnconfigure(0, weight=1); mid.rowconfigure(0, weight=1)
        cols = ("i", "owner", "name", "town", "pos", "members")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=8)
        for c, t, w in [("i", "#", 40), ("owner", "屋主", 110), ("name", "安全屋名", 140),
                        ("town", "城镇", 110), ("pos", "位置", 150), ("members", "成员", 260)]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w", stretch=(c == "members"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)

        # —— 日志
        tk.Label(root, text="运行日志", bg=self.BG_WIN, fg=self.FG_DIM).grid(row=5, column=0, sticky="w", pady=(10, 2))
        logf = ttk.Frame(root); logf.grid(row=6, column=0, sticky="nsew")
        logf.columnconfigure(0, weight=1); logf.rowconfigure(0, weight=1)
        self.txt = tk.Text(logf, height=9, bg="#ffffff", fg=self.FG_LOG, insertbackground=self.FG_LOG,
                           relief="solid", borderwidth=1, wrap="none", font=("Consolas", 9))
        self.txt.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.txt.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.txt.configure(yscrollcommand=lsb.set, state="disabled")

        # —— 进度
        f5 = ttk.Frame(root); f5.grid(row=7, column=0, sticky="ew", pady=(8, 0))
        f5.columnconfigure(0, weight=1)
        self.pb = ttk.Progressbar(f5, mode="determinate", maximum=100)
        self.pb.grid(row=0, column=0, sticky="ew")
        self.lbl_status = tk.Label(f5, text="就绪", bg=self.BG_WIN, fg=self.FG_DIM, anchor="e")
        self.lbl_status.grid(row=0, column=1, padx=(10, 0))

        root.rowconfigure(4, weight=3)
        root.rowconfigure(6, weight=2)

        self.var_save.trace_add("write", lambda *_: self._invalidate())
        self.var_pad.trace_add("write", lambda *_: self._invalidate())

    def _load_defaults(self) -> None:
        saved = str(self.cfg.get("save_root") or "")
        if saved and eng.resolve_save_root(saved) is None:
            self._log(f"上次的存档路径已失效：{saved}")
            saved = ""
        self.var_save.set(saved)
        self._log("僵毁区块清理器 — 图形界面")
        self._log("步骤：① 选存档 → ② 分析 → （可选看网页预览）→ ③ 执行清理")
        self._log("提醒：执行前务必关闭僵尸毁灭工程服务器。")

    # ------------------------------------------------------------------ 日志
    def _log(self, msg: str) -> None:
        self.txt.configure(state="normal")
        self.txt.insert("end", msg.rstrip() + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _status(self, text: str) -> None:
        self.lbl_status.configure(text=text)

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    done, total, nbytes = payload
                    self.pb["value"] = done * 100.0 / max(1, total)
                    self._status(f"删除中 {done:,}/{total:,}（{eng.human_size(nbytes)}）")
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self.after(80, self._pump)

    # ------------------------------------------------------------------ 交互
    def _invalidate(self) -> None:
        """输入变了，之前分析的结果就作废。"""
        if self.res is not None:
            self.res = None
            self.btn_go.state(["disabled"])
            self._status("参数已改动，请重新分析")

    def _pick_save(self) -> None:
        d = filedialog.askdirectory(title="选择存档根目录（含 map 子目录的那一层）")
        if d:
            self.var_save.set(d)

    def _auto_save(self) -> None:
        self._status("正在查找存档…")
        self.update_idletasks()
        found = eng.discover_saves()
        if not found:
            self._status("就绪")
            messagebox.showinfo("没找到存档",
                                "常见位置（C:\\Users\\*\\Zomboid\\Saves）下没找到含 map 目录的存档。\n"
                                "请用「浏览…」手动选择。")
            return
        if len(found) == 1:
            self.var_save.set(str(found[0]))
            self._status("就绪")
            return
        win = tk.Toplevel(self)
        win.title("选择存档")
        win.transient(self)
        win.grab_set()
        win.geometry("620x300")
        ttk.Label(win, text="找到以下存档，双击选择：", padding=8).pack(anchor="w")
        lb = tk.Listbox(win, bg="#ffffff", fg="#1e293b", relief="solid", borderwidth=1,
                        selectbackground="#bfdbfe", selectforeground="#0f172a")
        lb.pack(fill="both", expand=True, padx=8)
        for p in found:
            lb.insert("end", str(p))

        def choose(_evt=None):
            sel = lb.curselection()
            if sel:
                self.var_save.set(lb.get(sel[0]))
                win.destroy()
                self._status("就绪")

        lb.bind("<Double-Button-1>", choose)
        ttk.Button(win, text="确定", command=choose).pack(pady=8)

    def _pick_backup(self) -> None:
        d = filedialog.askdirectory(title="选择备份目录（留空则不备份）")
        if d:
            self.var_backup.set(d)

    def _open_web(self) -> None:
        web = eng.ensure_web_dir()
        page = web / "index.html"
        if not page.is_file():
            messagebox.showerror("打不开", f"找不到网页预览：{page}")
            return
        webbrowser.open(page.resolve().as_uri())

    # ------------------------------------------------------------------ 分析
    def _plan_key(self) -> tuple:
        return (self.var_save.get().strip(), int(self.var_pad.get() or 0))

    def _on_scan(self) -> None:
        if self.busy:
            return
        raw = self.var_save.get().strip()
        if not raw:
            messagebox.showwarning("还没选存档", "请先选择存档根目录。")
            return
        root = eng.resolve_save_root(raw)
        if root is None:
            messagebox.showerror("路径不对", "这个目录下没有 map 子目录，请选存档根目录。")
            return
        self.var_save.set(str(root))
        pad = max(0, min(eng.MAX_SAFEHOUSE_PAD, int(self.var_pad.get() or 0)))
        self._set_busy(True, "正在分析…")
        self.pb["value"] = 0
        threading.Thread(target=self._scan_worker, args=(root, pad), daemon=True).start()

    def _scan_worker(self, root: Path, pad: int) -> None:
        try:
            keep = eng.base_dir() / str(self.cfg.get("keep_file") or eng.KEEP_NAME)
            res = eng.analyze(root, pad, keep, bool(self.cfg.get("use_keep_file", True)))
            if res is None:
                self.q.put(("done", {"ok": False, "msg": "存档里没有区块文件，无需清理。"}))
                return
            res["web_index"] = eng.write_index(root, res, eng.ensure_web_dir())
            self.q.put(("done", {"ok": True, "res": res, "root": root, "pad": pad}))
        except Exception:
            self.q.put(("done", {"ok": False, "msg": traceback.format_exc()}))

    # ------------------------------------------------------------------ 删除
    def _on_delete(self) -> None:
        if self.busy or not self.res:
            return
        res = self.res
        if self._plan_key() != self.last_plan_key:
            messagebox.showwarning("请重新分析", "存档或参数改过了，请先重新点「分析」。")
            self._invalidate()
            return
        if not res["safehouses"]:
            messagebox.showerror("已中止", "没有解析到任何安全屋，删除会清空整张地图，已中止。")
            return
        if res["del_count"] == 0:
            messagebox.showinfo("无需清理", "没有需要删除的区块。")
            return

        backup_base = self.var_backup.get().strip().strip('"')
        word = str(self.cfg.get("confirm_word") or "删除")

        if not self._confirm_dialog(res, backup_base, word):
            self._log("已取消，未删除任何文件。")
            return

        if self.var_check.get():
            self._set_busy(True, "检测服务器进程…")
            running = eng.server_process_running()
            if running:
                self._set_busy(False, "已中止")
                messagebox.showerror("检测到服务器在运行",
                                     f"发现进程：{running}\n\n请先关闭服务器再执行，否则可能损坏存档。")
                return
            if running is None:
                self._set_busy(False, "已中止")
                if not messagebox.askyesno("无法检测",
                                           "检测不了服务器进程（PowerShell 不可用）。\n"
                                           "你确认服务器已经完全关闭了吗？"):
                    return
            else:
                self._log("服务器进程检测：未发现运行中的服务器。")

        backup_root = None
        if backup_base:
            bp = Path(backup_base).expanduser()
            if not bp.is_absolute():
                bp = eng.base_dir() / bp
            if bp.is_file():
                messagebox.showerror("备份位置无效", f"这个路径被同名文件占用了：\n{bp}")
                return
            import time as _t
            backup_root = bp / f"{res['root_name']}_{_t.strftime('%Y%m%d_%H%M%S')}" \
                if "root_name" in res else bp / f"{Path(self.var_save.get()).name}_{_t.strftime('%Y%m%d_%H%M%S')}"
            try:
                backup_root.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                messagebox.showerror("建不了备份目录", str(e))
                return
            self._log(f"备份到：{backup_root}")

        self._set_busy(True, "正在删除…")
        self.pb["value"] = 0
        self.btn_go.state(["disabled"])
        threading.Thread(target=self._delete_worker,
                         args=(res, backup_root), daemon=True).start()

    def _delete_worker(self, res: Dict[str, Any], backup_root: Optional[Path]) -> None:
        try:
            stat = eng.execute_delete(
                res["map_dir"], res["bins"], res["protected"], backup_root,
                progress=lambda d, t, b: self.q.put(("progress", (d, t, b))))
            self.q.put(("done", {"ok": True, "stat": stat, "backup": backup_root}))
        except Exception:
            self.q.put(("done", {"ok": False, "msg": traceback.format_exc()}))

    # ------------------------------------------------------------------ 确认框
    def _confirm_dialog(self, res: Dict[str, Any], backup_base: str, word: str) -> bool:
        dlg = tk.Toplevel(self)
        dlg.title("确认清理")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)
        f = ttk.Frame(dlg, padding=16)
        f.pack(fill="both", expand=True)

        tk.Label(f, text="即将删除以下区块（不可撤销）",
                 font=("Microsoft YaHei UI", 11, "bold"), fg="#b91c1c").pack(anchor="w")
        lines = [
            f"存档：{res['map_dir'].parent}",
            f"删除：{res['del_count']:,} 个区块文件",
            f"保留：{res['kept_count']:,} 个（安全屋 {len(res['safehouses'])} 个 / 缓冲 {res['pad']} 格）",
            f"备份：{backup_base if backup_base else '不备份（删除后无法恢复）'}",
        ]
        body = tk.Text(f, height=len(lines), bg="#f8fafc", fg="#1e293b", relief="solid",
                       borderwidth=1, font=("Microsoft YaHei UI", 9), wrap="none")
        body.pack(fill="x", pady=(8, 10))
        body.insert("1.0", "\n".join(lines))
        body.configure(state="disabled")

        ttk.Label(f, text=f"确认请输入「{word}」两个字：").pack(anchor="w")
        var = tk.StringVar()
        ent = ttk.Entry(f, textvariable=var, width=24)
        ent.pack(anchor="w", pady=(4, 12))
        ent.focus_set()

        result = {"ok": False}
        btns = ttk.Frame(f); btns.pack(fill="x")

        def ok(_e=None):
            if var.get().strip() != word:
                messagebox.showwarning("输入不对", f"请准确输入「{word}」两个字。", parent=dlg)
                return
            result["ok"] = True
            dlg.destroy()

        ttk.Button(btns, text="确认删除", command=ok).pack(side="right")
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right", padx=8)
        ent.bind("<Return>", ok)
        dlg.wait_window()
        return result["ok"]

    # ------------------------------------------------------------------ 状态切换
    def _set_busy(self, busy: bool, status: str) -> None:
        self.busy = busy
        state = ["disabled"] if busy else ["!disabled"]
        for b in (self.btn_scan, self.btn_web):
            b.state(state)
        if busy:
            self.btn_go.state(["disabled"])
        elif self.res:
            self.btn_go.state(["!disabled"])
        self._status(status)

    def _finish(self, payload: Dict[str, Any]) -> None:
        self._set_busy(False, "就绪")
        if not payload.get("ok"):
            self._set_busy(False, "出错")
            self._log("出错：\n" + str(payload.get("msg", "")))
            messagebox.showerror("出错了", str(payload.get("msg", ""))[:1500])
            return
        if "stat" in payload:                       # 删除完成
            st = payload["stat"]
            self.pb["value"] = 100
            self._log("")
            self._log(f"完成：已删除 {st['deleted']:,} 个区块（{eng.human_size(st['bytes'])}），"
                      f"耗时 {st['seconds']} 秒；保留 {st['kept']:,} 个。")
            if st.get("failed"):
                self._log(f"注意：{st['failed']} 个文件删除失败（可能被占用）。")
            if payload.get("backup"):
                self._log(f"备份位置：{payload['backup']}")
                self._log("还原方法：把备份里的 X\\Y.bin 按原路径拷回存档的 map\\ 目录。")
            self._log("现在可以启动服务器了，被删除的区域会重新生成。")
            self.res = None
            self.btn_go.state(["disabled"])
            self._status("清理完成")
            return

        res = payload["res"]                        # 分析完成
        self.res = res
        self.last_plan_key = self._plan_key()
        self.lbl_stats["total"].configure(text=f"{res['total_bins']:,}")
        self.lbl_stats["keep"].configure(text=f"{res['kept_count']:,}")
        self.lbl_stats["del"].configure(text=f"{res['del_count']:,}")
        self.lbl_stats["sh"].configure(text=f"{len(res['safehouses']):,}")
        self.lbl_stats["size"].configure(text=eng.human_size(res.get("total_bytes", 0)))
        self.tree.delete(*self.tree.get_children())
        for i, sh in enumerate(res["safehouses"], 1):
            self.tree.insert("", "end", values=(
                i, sh.get("owner", ""), sh.get("name") or "-", sh.get("town") or "-",
                f"({sh['x']},{sh['y']}) {sh['w']}×{sh['h']}",
                "、".join(sh.get("members") or [])))
        self.cfg["save_root"] = str(payload["root"])
        self.cfg["pad_tiles"] = payload["pad"]
        self.cfg["backup_root"] = self.var_backup.get().strip() or None
        self.cfg["check_server_process"] = bool(self.var_check.get())
        eng.save_config(self.cfg)
        self._log(f"分析完成：删除 {res['del_count']:,} / 保留 {res['kept_count']:,}。"
                  "可点「打开网页预览」在地图上核对。")
        if not res["safehouses"]:
            self._log("警告：没有解析到安全屋，出于安全考虑不会允许删除。")
        else:
            self.btn_go.state(["!disabled"])

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno("正在执行", "任务还没结束，确定要退出吗？"):
            return
        try:
            self.cfg["save_root"] = self.var_save.get().strip()
            self.cfg["pad_tiles"] = int(self.var_pad.get() or 0)
            self.cfg["backup_root"] = self.var_backup.get().strip() or None
            eng.save_config(self.cfg)
        except Exception:
            pass
        self.destroy()


def main() -> int:
    _dpi_aware()
    if "--selftest" in sys.argv:
        # 打包后用来自检：建好界面立刻销毁，不弹窗。窗口模式没有 stdout，
        # 所以结果写进临时文件，靠退出码判断成败。
        import tempfile
        out = Path(tempfile.gettempdir()) / "pz_cleaner_selftest.txt"
        try:
            app = App()
            app.withdraw()
            app.update_idletasks()
            app.destroy()
            out.write_text("OK", encoding="utf-8")
            return 0
        except Exception:
            out.write_text(traceback.format_exc(), encoding="utf-8")
            return 1
    try:
        app = App()
    except Exception:
        # 窗口起不来时把错误显出来，别默默退出
        import tkinter.messagebox as mb

        r = tk.Tk(); r.withdraw()
        mb.showerror("启动失败", traceback.format_exc()[:1500])
        return 1
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
