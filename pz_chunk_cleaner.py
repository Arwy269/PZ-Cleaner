#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
僵毁区块清理器 (PZ Chunk Cleaner)
=======================================
Project Zomboid Build 42 (42.x) 存档清理工具：

    删除存档 map/<X>/<Y>.bin 中【除安全屋范围以外】的所有区块文件，
    服务器重启后这些区域会按地图包重新生成（地形/建筑/战利品/丧尸）。

安全屋坐标从存档根目录的 map_meta.bin 解析（B42 未公开格式，采用签名扫描）。
思路参考开源项目 HDRcade pz-b42-map-cleaner，本程序为独立实现 + 中文界面。

用法:
    区块清理器.exe                            # 交互式，自动找存档
    区块清理器.exe "D:\\Zomboid\\Saves\\Multiplayer\\servertest"
    区块清理器.exe --dry                      # 只分析不删除
    区块清理器.exe --pad 20 --backup-dir E:\\PZ备份   # 外扩 20 格保护 + 删除前备份
    区块清理器.exe --no-backup                        # 本次不备份（即使配置里设了位置）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

APP_NAME = "僵毁区块清理器"
APP_VERSION = "1.0.0"
CONFIG_NAME = "config.json"
INDEX_NAME = "save_index.json"
KEEP_NAME = "keep.json"

BIN_TILE_SIZE = 8          # B42：1 个区块 = 8×8 个世界方格
MAX_SAFEHOUSE_PAD = 500    # 缓冲上限，防止手滑填 999999
TOWN_RE = re.compile(r"^[^,]+,\s*KY$")

# ---------------------------------------------------------------------------
# 控制台输出（中文 / 颜色）
# ---------------------------------------------------------------------------

_USE_COLOR = False


def _enable_ansi() -> bool:
    """在 Windows 控制台打开 ANSI 颜色支持。"""
    if os.name != "nt":
        return sys.stdout.isatty()
    try:
        import ctypes

        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


def _setup_stdout() -> None:
    """保证中文输出不会因编码问题报错（重定向到文件/管道时也一样）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                continue          # 控制台：Python 走 Unicode API，保持默认即可
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


C_RESET = "\033[0m"
C_DIM = "\033[2m"
C_RED = "\033[31m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_CYAN = "\033[36m"
C_BOLD = "\033[1m"


def _c(text: str, color: str) -> str:
    return f"{color}{text}{C_RESET}" if _USE_COLOR else text


_SINK = None                      # GUI 会接管输出；None = 直接打印到控制台


def set_log_sink(fn) -> None:
    """把日志输出交给回调（图形界面用），传 None 恢复打印到控制台。"""
    global _SINK
    _SINK = fn


def log(msg: str = "") -> None:
    if _SINK is not None:
        _SINK(str(msg))
    else:
        print(msg, flush=True)


def ok(msg: str) -> None:
    log(_c("  ✓ ", C_GREEN) + msg)


def warn(msg: str) -> None:
    log(_c("  ! ", C_YELLOW) + msg)


def err(msg: str) -> None:
    log(_c("  × ", C_RED) + msg)


def info(msg: str) -> None:
    log(_c("  · ", C_CYAN) + msg)


def headline(step: str, msg: str) -> None:
    log(f"{_c(step, C_BOLD)} {msg}")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def human_int(n: int) -> str:
    return f"{n:,}"


# ---------------------------------------------------------------------------
# map_meta.bin 解析
# ---------------------------------------------------------------------------
# 安全屋记录结构（实测）：
#   [int32 x][int32 y][int32 w][int32 h][u16 len][UTF-8 屋主]...[int64][u16 len][UTF-8 屋主]
#                                                                  ^ 屋主字符串出现两次，作为签名
# 本程序用一条正则一次性定位该签名，比按字节遍历快两个数量级。

_OWNER_CHARS = rb"[A-Za-z0-9_\-\. ]"
_SIG_RE = re.compile(rb"\x00([\x01-\x28])(" + _OWNER_CHARS + rb"{1,40})[\s\S]{8}\x00\1\2")
_STR_RE = re.compile(rb"\x00([\x01-\x8c])([\x20-\x7e]{1,140})")

_NAME_OK_RE = re.compile(r"^[A-Za-z0-9_\-\. ]{1,40}$")


def _is_reasonable_name(s: str) -> bool:
    if not (1 <= len(s) <= 40):
        return False
    if s.strip() != s or not s.strip():
        return False
    return bool(_NAME_OK_RE.match(s))


def _find_signatures(b: bytes) -> List[Tuple[int, int, int, int, int, str, int]]:
    """返回 [(记录起点, x, y, w, h, 屋主, 屋主字符串结束位置)]。"""
    out: List[Tuple[int, int, int, int, int, str, int]] = []
    for m in _SIG_RE.finditer(b):
        i = m.start() - 16
        if i < 0:
            continue
        x, y, w, h = struct.unpack_from(">iiii", b, i)
        if not (0 <= x <= 40000 and 0 <= y <= 40000):
            continue
        if not (1 <= w <= 800 and 1 <= h <= 800):
            continue
        owner = m.group(2).decode("ascii", "ignore").strip()
        if not owner:
            continue
        out.append((i, x, y, w, h, owner, m.end()))
    return out


def _scan_strings(b: bytes, start: int, end: int) -> List[Tuple[int, str]]:
    """扫描 [start,end) 内所有长度前缀自洽的 UTF-8 字符串。"""
    out: List[Tuple[int, str]] = []
    for m in _STR_RE.finditer(b, start, end):
        raw = m.group(2)
        if len(raw) != m.group(1)[0]:        # 长度前缀必须和内容一致
            continue
        s = raw.decode("utf-8", "ignore").strip()
        if s:
            out.append((m.start(), s))
    return out


def parse_safehouses(meta_path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """解析 map_meta.bin，返回 (安全屋列表, 警告列表)。"""
    warnings: List[str] = []
    if not meta_path.is_file():
        warnings.append(f"找不到 {meta_path}")
        return [], warnings

    try:
        b = meta_path.read_bytes()
    except OSError as e:
        warnings.append(f"无法读取 map_meta.bin：{e}")
        return [], warnings

    cands = _find_signatures(b)
    if not cands:
        warnings.append("map_meta.bin 中没有找到任何安全屋签名（存档里可能确实没有安全屋）")
        return [], warnings

    records: List[Dict[str, Any]] = []
    for idx, (off, x, y, w, h, owner, p2) in enumerate(cands):
        next_off = cands[idx + 1][0] if idx + 1 < len(cands) else min(len(b), p2 + 2500)
        window_end = max(p2, next_off)

        strings: List[Tuple[int, str]] = []
        seen: Set[str] = set()
        for pos, s in _scan_strings(b, p2, window_end):
            if s in seen:
                continue
            seen.add(s)
            strings.append((pos, s))

        # 城镇：形如 "Muldraugh, KY"
        town, town_pos = "", None
        for pos, s in strings:
            if s != owner and TOWN_RE.match(s):
                town, town_pos = s, pos
                break
        if town_pos is None:
            continue

        # 安全屋名：城镇之前最近的一个字符串（排除屋主 / 其它城镇名）
        name = ""
        before = sorted(
            (p for p, s in strings if p < town_pos and s != owner and not TOWN_RE.match(s)),
            key=lambda p: town_pos - p,
        )
        for p in before:
            if town_pos - p <= 140:
                name = next(s for q, s in strings if q == p)
                break
        if not name:
            after = [(p, s) for p, s in strings
                     if p > town_pos and s != owner and not TOWN_RE.match(s)]
            after.sort(key=lambda t: t[0])
            if after and after[0][0] - town_pos <= 220:
                name = after[0][1]

        # 成员名单：窗口内其它像人名的字符串（城镇之后 300 字节为界）
        members: List[str] = []
        for pos, s in strings:
            if pos > town_pos + 300:
                continue
            if s in (owner, town, name) or TOWN_RE.match(s):
                continue
            if _is_reasonable_name(s) and s not in members:
                members.append(s)

        records.append({
            "x": int(x), "y": int(y), "w": int(w), "h": int(h),
            "owner": owner, "town": town, "name": name, "members": members,
        })

    if not records:
        warnings.append("找到了候选签名，但没有一条通过城镇校验（格式可能已变）")
    return records, warnings


# ---------------------------------------------------------------------------
# 区块扫描
# ---------------------------------------------------------------------------

def scan_bins(map_dir: Path) -> Tuple[Dict[int, List[int]], int, int]:
    """扫描 map/<X>/<Y>.bin，返回 ({X: [Y,...]}, 警告数, 总字节数)。"""
    bins: Dict[int, List[int]] = {}
    warnings = 0
    total_bytes = 0
    try:
        entries = list(os.scandir(map_dir))
    except OSError as e:
        raise RuntimeError(f"无法读取 map 目录：{e}") from e

    for e in entries:
        try:
            if not e.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        if not e.name.isdigit():
            continue
        bx = int(e.name)
        ys: List[int] = []
        try:
            with os.scandir(e.path) as it:
                for f in it:
                    if not f.is_file(follow_symlinks=False):
                        continue
                    stem, dot, ext = f.name.partition(".")
                    if ext.lower() == "bin" and dot and stem.isdigit():
                        ys.append(int(stem))
                        try:
                            total_bytes += f.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
        except OSError:
            warnings += 1
            continue
        if ys:
            bins[bx] = sorted(ys)
    return bins, warnings, total_bytes


def compress_ranges(values: Sequence[int]) -> List[List[int]]:
    """把有序整数列表压成 [[起,止], ...] 区间。"""
    if not values:
        return []
    out: List[List[int]] = []
    a = b = values[0]
    for v in values[1:]:
        if v == b + 1:
            b = v
        else:
            out.append([a, b])
            a = b = v
    out.append([a, b])
    return out


# ---------------------------------------------------------------------------
# 保护范围
# ---------------------------------------------------------------------------

def build_protected(
    safehouses: Sequence[Dict[str, Any]],
    extra_rects: Sequence[Sequence[float]],
    pad: int,
) -> Dict[int, Set[int]]:
    """
    算出受保护的区块集合 {X: {Y,...}}。
    安全屋矩形 + 外扩 pad 格；extra_rects 为额外保护区（世界方格坐标 [x1,y1,x2,y2]）。
    """
    prot: Dict[int, Set[int]] = {}
    p = max(0, int(pad))
    t = BIN_TILE_SIZE

    def add_rect(x1: float, y1: float, x2: float, y2: float) -> None:
        bx1 = max(0, int((min(x1, x2) - p) // t))
        by1 = max(0, int((min(y1, y2) - p) // t))
        bx2 = int((max(x1, x2) + p) // t)
        by2 = int((max(y1, y2) + p) // t)
        for bx in range(bx1, bx2 + 1):
            s = prot.setdefault(bx, set())
            s.update(range(by1, by2 + 1))

    for sh in safehouses:
        add_rect(sh["x"], sh["y"], sh["x"] + sh["w"] - 1, sh["y"] + sh["h"] - 1)
    for r in extra_rects:
        if isinstance(r, (list, tuple)) and len(r) >= 4:
            try:
                add_rect(float(r[0]), float(r[1]), float(r[2]), float(r[3]))
            except (TypeError, ValueError):
                continue
    return prot


def load_keep_rects(path: Path) -> Tuple[List[List[float]], Optional[str]]:
    """读取网页导出的 keep.json（额外保护区）。"""
    if not path.is_file():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [], f"{path.name} 解析失败：{e}"
    rects = data.get("rects") if isinstance(data, dict) else data
    if not isinstance(rects, list):
        return [], f"{path.name} 格式不正确（缺少 rects 数组）"
    return [r for r in rects if isinstance(r, (list, tuple)) and len(r) >= 4], None


# ---------------------------------------------------------------------------
# 配置 / 存档发现
# ---------------------------------------------------------------------------

def base_dir() -> Path:
    """exe（或脚本）所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """内置资源目录（PyInstaller 打包后为临时解包目录）。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return base_dir()


def ensure_web_dir() -> Path:
    """
    网页预览目录：优先用 exe 同目录的 web\\（可自行修改），
    不存在时把打包内置的那份释放出来。
    """
    target = base_dir() / "web"
    if (target / "index.html").is_file():
        return target
    src = resource_dir() / "web"
    if src.is_dir() and src.resolve() != target.resolve():
        try:
            shutil.copytree(src, target, dirs_exist_ok=True)
        except OSError:
            return src
    return target



DEFAULT_CONFIG: Dict[str, Any] = {
    "save_root": "",
    "pad_tiles": 0,
    # 备份开关就在这一项：填了路径才备份，留空(null/"") 则完全不备份、也不建备份目录
    "backup_root": None,
    "confirm_word": "删除",
    "check_server_process": True,
    "use_keep_file": True,
    "keep_file": KEEP_NAME,
    "open_web": True,
}


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    p = base_dir() / CONFIG_NAME
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
        except (OSError, ValueError):
            warn(f"配置文件解析失败，使用默认配置：{p}")
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    try:
        (base_dir() / CONFIG_NAME).write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def discover_saves() -> List[Path]:
    """在常见位置搜索存档（含 map 子目录的目录）。"""
    bases: List[Path] = []
    profile = os.environ.get("USERPROFILE")
    if profile:
        bases.append(Path(profile) / "Zomboid" / "Saves")
    users = Path("C:/Users")
    if users.is_dir():
        try:
            for d in users.iterdir():
                bases.append(d / "Zomboid" / "Saves")
        except OSError:
            pass

    found: List[Path] = []
    seen: Set[str] = set()
    for base in bases:
        if not base.is_dir():
            continue
        for sub in (base / "Multiplayer", base):
            if not sub.is_dir():
                continue
            try:
                children = list(sub.iterdir())
            except OSError:
                continue
            for save in children:
                try:
                    if not (save / "map").is_dir():
                        continue
                    key = str(save.resolve()).lower()
                except OSError:
                    continue
                if key not in seen:
                    seen.add(key)
                    found.append(save)
    return found


def resolve_save_root(raw: str) -> Optional[Path]:
    """接受存档根目录（含 map\\）或 map 目录本身，返回存档根目录。"""
    p = Path(raw.strip().strip('"')).expanduser()
    if (p / "map").is_dir():
        return p.resolve()
    if p.is_dir() and p.name.lower() == "map":
        return p.parent.resolve()
    if p.is_dir():
        found = [d for d in p.iterdir() if d.is_dir() and (d / "map").is_dir()]
        if len(found) == 1:
            return found[0].resolve()
    return None


# ---------------------------------------------------------------------------
# 服务器进程检测
# ---------------------------------------------------------------------------

def server_process_running() -> Optional[str]:
    """返回正在运行的服务器进程名；未运行返回 ""；无法检测返回 None。"""
    if os.name != "nt":
        return ""
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match 'ProjectZomboid' -or "
        "$_.CommandLine -match 'zombie\\.network\\.GameServer' } | "
        "Select-Object -First 1 -ExpandProperty Name"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


# ---------------------------------------------------------------------------
# 执行删除
# ---------------------------------------------------------------------------

def execute_delete(
    map_dir: Path,
    bins: Dict[int, List[int]],
    protected: Dict[int, Set[int]],
    backup_root: Optional[Path],
    progress=None,
) -> Dict[str, int]:
    """删除（或备份后删除）所有未受保护的区块文件。"""
    stat = {"deleted": 0, "kept": 0, "bytes": 0, "failed": 0}
    made_dirs: Set[Path] = set()
    t0 = time.time()
    shown = 0.0
    total = sum(
        sum(1 for by in ys if by not in protected.get(bx, ())) for bx, ys in bins.items()
    )

    for bx, ys in bins.items():
        prot = protected.get(bx, ())
        src_dir = map_dir / str(bx)
        dst_dir = backup_root / str(bx) if backup_root else None

        for by in ys:
            if by in prot:
                stat["kept"] += 1
                continue
            src = src_dir / f"{by}.bin"
            try:
                if dst_dir is not None:
                    if dst_dir not in made_dirs:
                        dst_dir.mkdir(parents=True, exist_ok=True)
                        made_dirs.add(dst_dir)
                    stat["bytes"] += src.stat().st_size
                    shutil.move(str(src), str(dst_dir / src.name))
                else:
                    stat["bytes"] += src.stat().st_size
                    os.remove(src)
                stat["deleted"] += 1
            except OSError:
                stat["failed"] += 1
                continue

            now = time.time()
            if now - shown > 0.15:
                shown = now
                if progress is not None:
                    progress(stat["deleted"], total, stat["bytes"])
                else:
                    pct = stat["deleted"] * 100.0 / max(1, total)
                    print(
                        f"\r  {_c('删除中', C_CYAN)} {human_int(stat['deleted'])}/"
                        f"{human_int(total)} ({pct:5.1f}%)  {human_size(stat['bytes'])}   ",
                        end="", flush=True,
                    )

    elapsed = time.time() - t0
    stat["seconds"] = int(elapsed)  # type: ignore[assignment]
    stat["total"] = total          # type: ignore[assignment]
    if progress is None:
        print("\r" + " " * 72 + "\r", end="", flush=True)
    else:
        progress(stat["deleted"], total, stat["bytes"])
    return stat


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def banner() -> None:
    log()
    log(_c("=" * 62, C_CYAN))
    log(_c(f"  {APP_NAME}  v{APP_VERSION}", C_BOLD))
    log(_c("  Project Zomboid Build 42 · 删除安全屋以外的所有区块", C_DIM))
    log(_c("=" * 62, C_CYAN))
    log()


def choose_save(cfg: Dict[str, Any], cli_path: Optional[str]) -> Optional[Path]:
    if cli_path:
        root = resolve_save_root(cli_path)
        if root is None:
            err(f"路径无效（找不到 map\\ 子目录）：{cli_path}")
        return root

    if cfg.get("save_root"):
        root = resolve_save_root(str(cfg["save_root"]))
        if root is not None:
            info(f"使用配置里的存档：{_c(str(root), C_BOLD)}")
            return root
        warn(f"配置里的存档已失效：{cfg['save_root']}")

    found = discover_saves()
    if found:
        log("  自动找到以下存档：")
        for i, p in enumerate(found, 1):
            log(f"    [{i}] {p}")
        log("    [0] 手动输入路径")
        log()
        try:
            raw = input("  请选择序号并回车：").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if raw == "0" or not raw:
            pass
        elif raw.isdigit() and 1 <= int(raw) <= len(found):
            root = found[int(raw) - 1]
            cfg["save_root"] = str(root)
            save_config(cfg)
            return root
        else:
            warn("序号无效，改为手动输入。")

    try:
        raw = input("  请输入存档路径（含 map\\ 的那一层）后回车：").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        return None
    root = resolve_save_root(raw)
    if root is None:
        err("路径无效：该目录下没有 map\\ 子目录。")
        return None
    cfg["save_root"] = str(root)
    save_config(cfg)
    return root


def analyze(root: Path, pad: int, keep_path: Path, use_keep: bool):
    map_dir = root / "map"
    meta_path = root / "map_meta.bin"

    headline("[1/4]", "扫描区块文件 …")
    t0 = time.time()
    bins, scan_warn, total_bytes = scan_bins(map_dir)
    total_bins = sum(len(v) for v in bins.values())
    info(f"已生成区块：{_c(human_int(total_bins), C_BOLD)} 个"
         f"（{len(bins)} 个 X 目录，{human_size(total_bytes)}，耗时 {time.time() - t0:.1f}s）")
    if scan_warn:
        warn(f"{scan_warn} 个目录读取失败，已跳过")
    if total_bins == 0:
        err("该存档的 map 目录里没有任何区块文件，无需清理。")
        return None

    headline("[2/4]", "解析 map_meta.bin 安全屋 …")
    t0 = time.time()
    safehouses, sh_warn = parse_safehouses(meta_path)
    for w in sh_warn:
        warn(w)
    info(f"安全屋：{_c(human_int(len(safehouses)), C_BOLD)} 个"
         f"（耗时 {time.time() - t0:.1f}s）")

    headline("[3/4]", "计算保护范围 …")
    extra_rects: List[List[float]] = []
    if use_keep:
        extra_rects, keep_err = load_keep_rects(keep_path)
        if keep_err:
            warn(keep_err)
        elif extra_rects:
            info(f"额外保护区（{keep_path.name}）：{len(extra_rects)} 块")
    protected = build_protected(safehouses, extra_rects, pad)
    prot_count = sum(len(v) for v in protected.values())
    info(f"保护区：缓冲 {pad} 格 → {human_int(prot_count)} 个区块")

    headline("[4/4]", "统计待删除 …")
    to_delete: List[Tuple[int, List[int]]] = []
    del_count = 0
    for bx, ys in bins.items():
        prot = protected.get(bx, ())
        rest = [by for by in ys if by not in prot]
        if rest:
            to_delete.append((bx, rest))
            del_count += len(rest)
    kept_count = total_bins - del_count

    return {
        "map_dir": map_dir,
        "meta_path": meta_path,
        "bins": bins,
        "safehouses": safehouses,
        "protected": protected,
        "extra_rects": extra_rects,
        "to_delete": to_delete,
        "total_bins": total_bins,
        "total_bytes": total_bytes,
        "del_count": del_count,
        "kept_count": kept_count,
        "pad": pad,
    }


def write_index(root: Path, res: Dict[str, Any], web_dir: Path) -> Optional[Path]:
    """写出网页预览用的 save_index.json。"""
    payload = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "save_root": str(root),
        "bin_tile_size": BIN_TILE_SIZE,
        "bins": res["total_bins"],
        "bins_by_x": {str(bx): compress_ranges(ys) for bx, ys in res["bins"].items()},
        "safehouses": res["safehouses"],
        "extra_rects": res["extra_rects"],
        "pad_tiles": res["pad"],
        # 用区间压缩，缓冲设得很大时体积也不会爆
        "protected_bins": {str(bx): compress_ranges(sorted(v))
                           for bx, v in res["protected"].items()},
        "summary": {
            "total": res["total_bins"],
            "delete": res["del_count"],
            "keep": res["kept_count"],
        },
    }
    for target in (web_dir / INDEX_NAME, base_dir() / INDEX_NAME):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return target
        except OSError:
            continue
    return None


def print_safehouse_table(safehouses: Sequence[Dict[str, Any]]) -> None:
    if not safehouses:
        return
    log()
    log(f"  {'#':>3}  {'屋主':<14}{'安全屋名':<20}{'城镇':<16}{'位置':<22}成员")
    log("  " + "-" * 88)
    for i, sh in enumerate(safehouses, 1):
        pos = f"({sh['x']},{sh['y']}) {sh['w']}×{sh['h']}"
        name = (sh["name"] or "-")[:18]
        owner = (sh["owner"] or "-")[:12]
        town = (sh["town"] or "-")[:14]
        members = ",".join(sh["members"][:4])
        if len(sh["members"]) > 4:
            members += f" 等{len(sh['members'])}人"
        log(f"  {i:>3}  {owner:<14}{name:<20}{town:<16}{pos:<22}{members}")
    log()


def confirm(prompt_word: str, backup_root: Optional[Path], backup_note: str = "") -> bool:
    """二次确认：必须手动输入确认词。backup_root 为 None 表示本次不备份。"""
    log()
    log(_c("  ⚠ 即将执行不可恢复的删除操作。", C_YELLOW))
    if backup_root:
        info(f"已开启备份：文件会先移动到 {backup_root}")
    else:
        warn(f"{backup_note or '本次不备份'}，本次不备份：删除后无法通过本工具恢复。")
        warn("想备份就把 config.json 的 backup_root 填成备份目录，例如 E:\\PZ备份。")
    log()
    try:
        answer = input(f"  输入 {_c(prompt_word, C_BOLD)} 两个字并回车以继续（其它任意输入取消）：").strip()
    except (EOFError, KeyboardInterrupt):
        log()
        return False
    return answer == prompt_word


def main(argv: Optional[Sequence[str]] = None) -> int:
    global _USE_COLOR
    _setup_stdout()
    _USE_COLOR = _enable_ansi()

    parser = argparse.ArgumentParser(
        prog=APP_NAME, add_help=False,
        description="删除僵尸毁灭工程 B42 存档中安全屋以外的所有区块。",
    )
    parser.add_argument("save", nargs="?", help="存档路径（含 map 目录的那一层）")
    parser.add_argument("--pad", type=int, help=f"安全屋外扩缓冲格数（默认读配置，0~{MAX_SAFEHOUSE_PAD}）")
    parser.add_argument("--backup-dir", help="本次备份到指定目录（不写则用 config.json 的 backup_root）")
    parser.add_argument("--no-backup", action="store_true", help="本次不备份，即使 backup_root 已设置")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过二次确认（危险）")
    parser.add_argument("--force", action="store_true", help="跳过服务器进程检测")
    parser.add_argument("--dry", action="store_true", help="只分析并写出预览，不删除")
    parser.add_argument("--no-keep", action="store_true", help="忽略 keep.json 额外保护区")
    parser.add_argument("--no-web", action="store_true", help="不自动打开网页预览")
    parser.add_argument("--list", action="store_true", help="只列出安全屋后退出")
    parser.add_argument("--help", "-h", action="store_true", help="显示帮助")
    args = parser.parse_args(argv)

    cfg = load_config()

    if args.help:
        banner()
        log(__doc__)
        log("  可选参数：")
        for a in parser._actions:
            if a.option_strings:
                log(f"    {', '.join(a.option_strings):<18} {a.help or ''}")
        return 0

    banner()

    root = choose_save(cfg, args.save)
    if root is None:
        err("没有选择存档，已退出。")
        return 1
    log()
    info(f"存档目录：{_c(str(root), C_BOLD)}")

    pad = args.pad if args.pad is not None else int(cfg.get("pad_tiles") or 0)
    pad = max(0, min(MAX_SAFEHOUSE_PAD, pad))

    if args.list:
        safehouses, w = parse_safehouses(root / "map_meta.bin")
        for x in w:
            warn(x)
        log()
        info(f"共 {len(safehouses)} 个安全屋：")
        print_safehouse_table(safehouses)
        return 0

    keep_path = base_dir() / str(cfg.get("keep_file") or KEEP_NAME)
    use_keep = bool(cfg.get("use_keep_file", True)) and not args.no_keep

    try:
        res = analyze(root, pad, keep_path, use_keep)
    except RuntimeError as e:
        err(str(e))
        return 1
    if res is None:
        return 1

    print_safehouse_table(res["safehouses"])

    web_dir = ensure_web_dir()
    index_path = write_index(root, res, web_dir)

    # ── 备份：只有给出备份位置才执行（backup_root 为空 = 不备份，也不会建任何目录）──
    backup_base = "" if args.no_backup else (args.backup_dir or cfg.get("backup_root") or "")
    backup_base = str(backup_base).strip().strip('"')
    backup_root: Optional[Path] = None
    if backup_base:
        bp = Path(backup_base).expanduser()
        if not bp.is_absolute():
            bp = base_dir() / bp
        if bp.is_file():
            err(f"备份位置被同名文件占用，无法作为目录：{bp}")
            return 4
        backup_root = bp / f"{root.name}_{time.strftime('%Y%m%d_%H%M%S')}"
        backup_note = ""
    elif args.no_backup:
        backup_note = "已用 --no-backup 关闭"
    else:
        backup_note = "未设置（config.json 的 backup_root 为空）"

    if not res["safehouses"]:
        log()
        err("没有解析到任何安全屋，为防误删已中止（这种情况删下去会清空整张图）。")
        warn("请用 --list 检查 map_meta.bin 是否正常。")
        if index_path:
            info(f"已写出预览索引，可在网页上确认：{index_path}")
        return 2

    log()
    log(_c("  ── 清理计划 ──", C_BOLD))
    log(f"  已生成区块   {human_int(res['total_bins'])}")
    log(f"  {_c('保留', C_GREEN)}       {human_int(res['kept_count'])}"
        f"  （安全屋 {len(res['safehouses'])} 个 / 缓冲 {pad} 格）")
    log(f"  {_c('删除', C_RED)}       {human_int(res['del_count'])}")
    if backup_root:
        log(f"  {_c('备份', C_CYAN)}       {backup_root}")
    else:
        log(f"  {_c('备份', C_YELLOW)}       {backup_note}，本次不备份")
    if index_path:
        log(f"  预览索引     {index_path}")
        if (web_dir / "index.html").is_file():
            log(f"  网页预览     {web_dir / 'index.html'}（可先核对再删除）")

    if res["del_count"] == 0:
        ok("没有需要删除的区块。")
        return 0

    if not args.dry:
        if cfg.get("check_server_process", True) and not args.force:
            log()
            info("检测服务器进程 …")
            running = server_process_running()
            if running:
                err(f"检测到服务器进程正在运行：{running}")
                err("请先关闭服务器再执行，否则可能损坏存档（确认已关可加 --force 跳过）。")
                return 3
            if running is None:
                warn("无法检测服务器进程（PowerShell 不可用）。")
                log()
                log(_c("  ⚠ 请自行确认服务器已完全关闭！", C_YELLOW))
                try:
                    if input("  已确认关闭请输入 yes 回车：").strip().lower() != "yes":
                        log("  已取消。")
                        return 1
                except (EOFError, KeyboardInterrupt):
                    return 1
            else:
                ok("未检测到服务器进程。")

    open_web = (cfg.get("open_web", True) and not args.no_web
                and index_path is not None and (web_dir / "index.html").is_file())
    if open_web:
        log()
        try:
            if input("  是否先打开网页核对安全屋位置？(y/N)：").strip().lower() == "y":
                import webbrowser

                webbrowser.open((web_dir / "index.html").resolve().as_uri())
        except (EOFError, KeyboardInterrupt):
            pass

    if args.dry:
        log()
        ok("已按 --dry 结束，未删除任何文件。")
        return 0

    word = str(cfg.get("confirm_word") or "删除")

    if not args.yes and not confirm(word, backup_root, backup_note):
        log("  已取消，未删除任何文件。")
        return 0

    if backup_root is not None:
        try:
            backup_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            err(f"无法创建备份目录：{e}")
            return 4
        info(f"备份到：{backup_root}")

    log()
    log(_c("  开始清理 …", C_BOLD))
    stat = execute_delete(res["map_dir"], res["bins"], res["protected"], backup_root)

    log()
    log(_c("  ── 完成 ──", C_BOLD))
    log(f"  {_c('已删除', C_GREEN)}   {human_int(stat['deleted'])} 个区块"
        f"（{human_size(stat['bytes'])}），耗时 {stat['seconds']} 秒")
    log(f"  保留     {human_int(stat['kept'])} 个区块（安全屋范围）")
    if stat["failed"]:
        warn(f"{stat['failed']} 个文件删除失败（可能被占用）")
    if backup_root:
        log(f"  备份位置 {backup_root}")
        log(_c("  （如需还原：把备份里的 X\\Y.bin 按原路径拷回 map\\ 即可）", C_DIM))
    log()
    ok("现在可以启动服务器了，被删除的区域会重新生成。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log()
        log("  已中断。")
        sys.exit(130)
