#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
僵毁区块重置器 (PZ Chunk Resetter) — 网页版
------------------------------------------------
在 Windows 僵尸毁灭工程 (Project Zomboid) 专用服务器环境下运行：
  * 网页界面选择存档，直接删除 map/<区块X>/<区块Y>.bin 文件（B42）
    或 map/r<X>_<Y>.bin（B41），服务器重启后该区块自动重新生成。
  * 底图来自 pzmap.org 的 DZI 瓦片（Cloudflare 用 curl_cffi 绕过，
    失败时自动回退到开源镜像 pzmap.net）。
  * 仅标准库 + 可选 curl_cffi；删除默认先备份。

用法:
    python server.py                 # 启动并自动打开浏览器
    python server.py --no-browser    # 不自动打开浏览器
    python server.py --host 0.0.0.0 --port 9000   # 允许局域网访问(注意防火墙)
"""

import argparse
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

APP_TITLE = "僵毁区块重置器"
APP_VERSION = "1.0.0"
CONFIRM_WORD = "删除"
MAX_CELLS = 200_000          # 单次请求最多选择的区块数（安全阀）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# --------------------------------------------------------------------------
# 路径 / 配置
# --------------------------------------------------------------------------

def base_dir() -> Path:
    """可写目录：exe 所在目录（或脚本所在目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """静态资源目录（PyInstaller 打包时为临时解包目录）。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return base_dir()


STATIC_DIR = resource_dir() / "static"
CACHE_DIR = base_dir() / "mapcache"
CONFIG_FILE = base_dir() / "config.json"

DEFAULT_CONFIG = {
    "port": 8765,
    "host": "127.0.0.1",
    "save_roots": [
        r"C:\Users\Administrator\Zomboid\Saves\Multiplayer",
    ],
    "selected_save": None,
    "chunk_divisor": 8,        # B42: 1 个区块 = 8×8 个世界方格（存档实测）
    "chunk_divisor_b41": 10,   # B41: 1 个区块 = 10×10 个世界方格
    "backup_root": None,       # None = exe 同目录的 区块备份\
    "map_source": "auto",      # auto | pzmap.org | pzmap.net
    "open_browser": True,
}

_cfg_lock = threading.Lock()
_config = dict(DEFAULT_CONFIG)


def load_config():
    global _config
    try:
        if CONFIG_FILE.is_file():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _config.update(data)
    except Exception:
        pass


def save_config():
    try:
        with _cfg_lock:
            CONFIG_FILE.write_text(
                json.dumps(_config, ensure_ascii=False, indent=2),
                encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------
# 底图（DZI 瓦片源）
# --------------------------------------------------------------------------

SOURCES = [
    {"id": "pzmap.org", "base": "https://tiles.pzmap.org/42.20.0/base/", "cffi": True},
    {"id": "pzmap.net", "base": "https://tiles.pzmap.net/base/", "cffi": False},
]


# ---- 上游下载：线程本地连接池 + 去重 + 耗时统计 ------------------------
_tlocal = threading.local()
_up_stats = {"n": 0, "avg_ms": 0.0}
_up_lock = threading.Lock()
_inflight = {}
_inflight_lock = threading.Lock()
_warm_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="warm")


def upstream_avg_ms() -> float:
    with _up_lock:
        return round(_up_stats["avg_ms"], 1)


def _record_upstream(ms: float):
    with _up_lock:
        _up_stats["n"] += 1
        _up_stats["avg_ms"] = (ms if _up_stats["n"] == 1
                               else _up_stats["avg_ms"] * 0.7 + ms * 0.3)


def get_session(src):
    """每线程 × 每瓦片源一个可复用连接的 Session（省去每块瓦片的 TCP+TLS 握手）。"""
    try:
        from curl_cffi import requests as creq
    except Exception:
        return None
    sessions = getattr(_tlocal, "sessions", None)
    if sessions is None:
        sessions = {}
        _tlocal.sessions = sessions
    s = sessions.get(src["id"])
    if s is None:
        s = creq.Session(impersonate="chrome")
        sessions[src["id"]] = s
    return s


def fetch_bytes(url: str, timeout: float = 20.0, cffi: bool = False,
                src=None) -> bytes:
    """下载字节：优先走连接池；cffi=True 时模拟 Chrome TLS 绕过 Cloudflare。"""
    if src is not None:
        s = get_session(src)
        if s is not None:
            r = s.get(url, timeout=timeout)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code} for {url}")
            return r.content
    if cffi:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} for {url}")
        return r.content
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        if status != 200:
            raise RuntimeError(f"HTTP {status} for {url}")
        return resp.read()


def tile_mime(meta) -> str:
    fmt = (meta.fmt or "jpg").lower()
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "webp": "image/webp"}.get(fmt, "image/" + fmt)


class MapMeta:
    """pzmap2dzi 渲染的投影参数（与 pzmap.org / pzmap.net 同源）。"""

    def __init__(self, src, tile_size, overlap, fmt, w, h, skip, x0, y0, sqr):
        self.src = src                    # {"id","base","cffi"}
        self.tile_size = int(tile_size)
        self.overlap = int(overlap)
        self.fmt = fmt                    # jpg / webp
        self.w = int(w)
        self.h = int(h)
        self.skip = int(skip or 0)
        self.x0 = float(x0)
        self.y0 = float(y0)
        self.sqr = float(sqr)
        self.scale = 1 << self.skip
        self.ref_level = math.ceil(math.log2(max(self.w, self.h)))

    def to_dict(self):
        return {
            "source": self.src["id"],
            "tileSize": self.tile_size,
            "overlap": self.overlap,
            "format": self.fmt,
            "w": self.w, "h": self.h,
            "skip": self.skip, "scale": self.scale,
            "x0": self.x0, "y0": self.y0, "sqr": self.sqr,
            "refLevel": self.ref_level,
            "avgUpstreamMs": upstream_avg_ms(),
        }

    @property
    def cache_key(self):
        """缓存目录名 = 源 + 渲染版本（base URL 变化时自动换新，避免投影错位）。"""
        tag = hashlib.md5(self.src["base"].encode("utf-8")).hexdigest()[:8]
        return f"{self.src['id']}_{tag}"


_map_meta = None
_map_error = None
_map_lock = threading.Lock()


def _parse_meta(src, info: dict, dzi: bytes) -> MapMeta:
    root = ET.fromstring(dzi)
    tile_size = root.attrib.get("TileSize", "256")
    overlap = root.attrib.get("Overlap", "0")
    fmt = root.attrib.get("Format", "jpg")
    size_el = None
    for el in root.iter():
        if el.tag.endswith("Size"):
            size_el = el
            break
    w = int(size_el.attrib.get("Width", info.get("w", 0))) if size_el is not None \
        else int(info.get("w", 0))
    h = int(size_el.attrib.get("Height", info.get("h", 0))) if size_el is not None \
        else int(info.get("h", 0))
    # pzmap.net 的 map_info 提供 tile_size 覆盖（其 dzi 为 1024）
    if info.get("tile_size"):
        tile_size = info["tile_size"]
    return MapMeta(src, tile_size, overlap, fmt, w, h,
                   info.get("skip", 0), info.get("x0", 0),
                   info.get("y0", 0), info.get("sqr", 128))


def _meta_cache_paths(src):
    tag = hashlib.md5(src["base"].encode("utf-8")).hexdigest()[:8]
    d = CACHE_DIR / f"{src['id']}_{tag}"
    return d / "map_info.json", d / "layer0.dzi"


def _load_meta_from_disk(src):
    """读本地缓存（7 天内有效）——启动不再等待网络。"""
    inf_p, dzi_p = _meta_cache_paths(src)
    if not (inf_p.is_file() and dzi_p.is_file()):
        return None
    age = max(time.time() - inf_p.stat().st_mtime,
              time.time() - dzi_p.stat().st_mtime)
    if age > 7 * 86400:
        return None
    try:
        info = json.loads(inf_p.read_text(encoding="utf-8"))
        return _parse_meta(src, info, dzi_p.read_bytes())
    except Exception:
        return None


def _save_meta_to_disk(src, info_b: bytes, dzi_b: bytes):
    try:
        inf_p, dzi_p = _meta_cache_paths(src)
        inf_p.parent.mkdir(parents=True, exist_ok=True)
        inf_p.write_bytes(info_b)
        dzi_p.write_bytes(dzi_b)
    except Exception:
        pass


def load_map_meta(force: bool = False) -> MapMeta:
    """瓦片源投影参数：本地缓存优先，过期/缺失才访问网络；全部失败抛 RuntimeError。"""
    global _map_meta, _map_error
    with _map_lock:
        if _map_meta is not None and not force:
            return _map_meta
        wanted = _config.get("map_source", "auto")
        candidates = [s for s in SOURCES if wanted in ("auto", s["id"])]
        errors = []
        for src in candidates:
            # 1) 本地缓存
            meta = _load_meta_from_disk(src)
            if meta is not None:
                _map_meta, _map_error = meta, None
                return meta
            # 2) 在线获取
            try:
                info_b = fetch_bytes(src["base"] + "map_info.json",
                                     cffi=src["cffi"], src=src)
                dzi_b = fetch_bytes(src["base"] + "layer0.dzi",
                                    cffi=src["cffi"], src=src)
                info = json.loads(info_b.decode("utf-8"))
                meta = _parse_meta(src, info, dzi_b)
                _save_meta_to_disk(src, info_b, dzi_b)
                _map_meta, _map_error = meta, None
                return meta
            except Exception as e:  # noqa: BLE001
                # 网络失败时回退到过期缓存（总比没有强）
                inf_p, dzi_p = _meta_cache_paths(src)
                if inf_p.is_file() and dzi_p.is_file():
                    try:
                        info = json.loads(inf_p.read_text(encoding="utf-8"))
                        meta = _parse_meta(src, info, dzi_p.read_bytes())
                        _map_meta, _map_error = meta, None
                        return meta
                    except Exception:
                        pass
                errors.append(f"{src['id']}: {e}")
        _map_meta = None
        _map_error = "；".join(errors) or "没有可用的瓦片源"
        raise RuntimeError(_map_error)


TILE_MISSING_DAYS = 30


def tile_range_ok(meta, z, x, y) -> bool:
    if z < 0 or z > meta.ref_level or x < 0 or y < 0:
        return False
    pw = math.ceil(meta.w / (2 ** (meta.ref_level - z)))
    ph = math.ceil(meta.h / (2 ** (meta.ref_level - z)))
    return (x < max(1, math.ceil(pw / meta.tile_size)) and
            y < max(1, math.ceil(ph / meta.tile_size)))


def warm_tile(meta, z, x, y, wait=True, want_data=True):
    """确保瓦片落在磁盘缓存里（同名请求去重、失败重试一次）。
    返回 (status, data|None)：
      ok      —— 缓存就绪（want_data=False 时 data 为 None）
      missing —— 上游确认没有这块
      busy    —— 其他线程正在下载（仅 wait=False 时返回）
      error   —— 下载失败
    """
    key = f"{meta.cache_key}|{z}|{x}|{y}"
    zdir = CACHE_DIR / meta.cache_key / "layer0_files" / str(z)
    fpath = zdir / f"{x}_{y}.{meta.fmt}"
    marker = Path(str(fpath) + ".missing")

    def disk():
        if fpath.is_file():
            return ("ok", fpath.read_bytes() if want_data else None)
        if marker.is_file() and \
                time.time() - marker.stat().st_mtime < TILE_MISSING_DAYS * 86400:
            return ("missing", None)
        return None

    hit = disk()
    if hit:
        return hit

    with _inflight_lock:
        ev = _inflight.get(key)
        owner = ev is None
        if owner:
            ev = threading.Event()
            _inflight[key] = ev
    if not owner:
        if wait:
            ev.wait(60)
            hit = disk()
            return hit if hit else ("error", None)
        return ("busy", None)

    try:
        url = f"{meta.src['base']}layer0_files/{z}/{x}_{y}.{meta.fmt}"
        last_err = None
        for attempt in range(2):
            t0 = time.time()
            try:
                data = fetch_bytes(url, timeout=25, cffi=meta.src["cffi"],
                                   src=meta.src)
                _record_upstream((time.time() - t0) * 1000)
                try:
                    zdir.mkdir(parents=True, exist_ok=True)
                    fpath.write_bytes(data)
                except Exception:
                    pass
                return ("ok", data if want_data else None)
            except Exception as e:  # noqa: BLE001
                last_err = e
                if "HTTP 404" in str(e):
                    try:
                        zdir.mkdir(parents=True, exist_ok=True)
                        marker.write_text(str(int(time.time())))
                    except Exception:
                        pass
                    return ("missing", None)
                time.sleep(0.4 * (attempt + 1))
        print(f"[瓦片下载失败] {url}: {last_err}", flush=True)
        return ("error", None)
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)
        ev.set()


def submit_prefetch(meta, z, x0, y0, x1, y1):
    """把一批瓦片丢进后台线程池预热缓存。返回 (queued, cached)。"""
    queued = cached = 0
    zdir = CACHE_DIR / meta.cache_key / "layer0_files" / str(z)
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            fpath = zdir / f"{x}_{y}.{meta.fmt}"
            marker = Path(str(fpath) + ".missing")
            if fpath.is_file() or (marker.is_file() and
                                   time.time() - marker.stat().st_mtime <
                                   TILE_MISSING_DAYS * 86400):
                cached += 1
                continue
            _warm_pool.submit(warm_tile, meta, z, x, y, False, False)
            queued += 1
    return queued, cached


# --------------------------------------------------------------------------
# 存档扫描
# --------------------------------------------------------------------------

B42_DIR_RE = re.compile(r"^-?\d+$")
B42_FILE_RE = re.compile(r"^(-?\d+)\.bin$")
B41_FILE_RE = re.compile(r"^r(-?\d+)_(-?\d+)\.bin$")
TILE_PATH_RE = re.compile(r"^/tiles/(\d+)/(-?\d+)/(-?\d+)$")

_cur_lock = threading.Lock()
_current = {"map_dir": None, "name": "", "fmt": None, "chunks": {}}
_current_pz = {"running": None, "procs": [], "checked": 0.0, "error": None}


def scan_map_dir(map_dir: Path):
    """返回 (格式 'b42'|'b41'|None, {(cx,cy): 字节数})。"""
    chunks = {}
    fmt = None
    try:
        entries = list(os.scandir(map_dir))
    except OSError:
        return None, chunks

    numeric_dirs = [e for e in entries if e.is_dir() and B42_DIR_RE.match(e.name)]
    if numeric_dirs:
        fmt = "b42"
        for d in numeric_dirs:
            cx = int(d.name)
            try:
                for f in os.scandir(d.path):
                    m = B42_FILE_RE.match(f.name)
                    if m and f.is_file():
                        chunks[(cx, int(m.group(1)))] = f.stat().st_size
            except OSError:
                continue
    else:
        flat = [e for e in entries if e.is_file() and B41_FILE_RE.match(e.name)]
        if flat:
            fmt = "b41"
            for f in flat:
                m = B41_FILE_RE.match(f.name)
                chunks[(int(m.group(1)), int(m.group(2)))] = f.stat().st_size
    return fmt, chunks


def chunk_payload():
    with _cur_lock:
        if _current["map_dir"] is None:
            return None
        chunks = _current["chunks"]
        if not chunks:
            bbox = None
        else:
            xs = [c[0] for c in chunks]
            ys = [c[1] for c in chunks]
            bbox = [min(xs), min(ys), max(xs), max(ys)]
        return {
            "fmt": _current["fmt"],
            "mapDir": str(_current["map_dir"]),
            "name": _current["name"],
            "divisor": int(_config.get(
                "chunk_divisor_b41" if _current["fmt"] == "b41" else "chunk_divisor",
                10 if _current["fmt"] == "b41" else 8)),
            "count": len(chunks),
            "bytes": sum(chunks.values()),
            "bbox": bbox,
            "chunks": [[cx, cy, b] for (cx, cy), b in sorted(chunks.items())],
        }


def resolve_map_dir(path: str) -> Path:
    """接受存档根目录（含 map 子目录）或 map 目录本身。"""
    p = Path(path.strip())
    if (p / "map").is_dir():
        return p / "map"
    if p.is_dir():
        return p
    raise RuntimeError(f"目录不存在或没有 map 子目录: {path}")


def save_display_name(map_dir: Path) -> str:
    save_dir = map_dir.parent
    parts = save_dir.parts
    user = None
    if "Users" in parts:
        try:
            user = parts[parts.index("Users") + 1]
        except Exception:
            user = None
    return f"{user}/{save_dir.name}" if user else save_dir.name


def candidate_roots():
    roots = []
    for r in _config.get("save_roots") or []:
        roots.append(r)
    up = os.environ.get("USERPROFILE")
    if up:
        roots.append(str(Path(up) / "Zomboid" / "Saves" / "Multiplayer"))
    roots.append(r"C:\Users\Administrator\Zomboid\Saves\Multiplayer")
    try:
        import glob
        roots.extend(glob.glob(r"C:\Users\*\Zomboid\Saves\Multiplayer"))
    except Exception:
        pass
    seen, out = set(), []
    for r in roots:
        rp = os.path.abspath(r)
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def list_saves():
    saves = []
    seen = set()
    for root in candidate_roots():
        try:
            subs = sorted(os.listdir(root))
        except OSError:
            continue
        for sub in subs:
            map_dir = Path(root) / sub / "map"
            if map_dir.is_dir():
                key = str(map_dir).lower()
                if key not in seen:
                    seen.add(key)
                    saves.append({
                        "name": save_display_name(map_dir),
                        "path": str(map_dir),
                    })
    return saves


def select_save(path: str, persist: bool = True) -> dict:
    map_dir = resolve_map_dir(path)
    fmt, chunks = scan_map_dir(map_dir)
    if fmt is None:
        raise RuntimeError(
            f"未识别的 map 目录结构: {map_dir}（既不是 B42 的 <X>/<Y>.bin，也不是 B41 的 r<X>_<Y>.bin）")
    with _cur_lock:
        _current.update({
            "map_dir": map_dir,
            "name": save_display_name(map_dir),
            "fmt": fmt,
            "chunks": chunks,
        })
    if persist:
        _config["selected_save"] = str(map_dir)
        save_config()
    return {"name": _current["name"], "fmt": fmt, "count": len(chunks)}


# --------------------------------------------------------------------------
# 服务器进程检测
# --------------------------------------------------------------------------

PS_PROC_CMD = (
    "Get-CimInstance Win32_Process | "
    "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
)


def _match_pz(name, cmdline):
    n = (name or "").lower()
    c = (cmdline or "").lower()
    if "projectzomboid" in n or "projectzomboid" in c:
        return True
    if "zombie.network.gameserver" in c or "projectzomboidserver" in c:
        return True
    return False


def check_pz(force: bool = False):
    now = time.time()
    if not force and now - _current_pz["checked"] < 5:
        return _current_pz
    procs = []
    running = None
    err = None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_PROC_CMD],
            capture_output=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        data = json.loads(out.stdout.decode("utf-8", "replace") or "null")
        if isinstance(data, dict):
            data = [data]
        for it in data or []:
            if _match_pz(it.get("Name"), it.get("CommandLine")):
                procs.append({
                    "pid": it.get("ProcessId"),
                    "name": it.get("Name"),
                    "cmd": (it.get("CommandLine") or "")[:160],
                })
        running = bool(procs)
    except Exception as e:  # noqa: BLE001
        err = str(e)
        # 回退：tasklist 只能看进程名
        try:
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            text = out.stdout.decode("gbk", "replace")
            hit = [ln for ln in text.splitlines()
                   if "ProjectZomboid" in ln or "projectzomboid" in ln]
            if hit:
                running = True
                procs = [{"pid": None, "name": ln.split(",")[0].strip('"'), "cmd": ""}
                         for ln in hit[:5]]
            else:
                running = None  # 无法确定
        except Exception:
            running = None
    _current_pz.update({"running": running, "procs": procs,
                        "checked": now, "error": err})
    return _current_pz


# --------------------------------------------------------------------------
# 删除逻辑
# --------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def backup_root() -> Path:
    configured = _config.get("backup_root")
    if configured:
        p = Path(configured)
    else:
        p = base_dir() / "区块备份"
    p.mkdir(parents=True, exist_ok=True)
    return p


def do_reset(cells, mode: str, force: bool):
    with _cur_lock:
        if _current["map_dir"] is None:
            raise ApiError(400, "尚未选择存档")
        map_dir = _current["map_dir"]
        fmt = _current["fmt"]
        chunks = _current["chunks"]
        name = _current["name"]

        pz = check_pz(force=True)
        if pz["running"] and not force:
            raise ApiError(409, "检测到僵毁服务器进程正在运行，请先停止服务器再重置")
        if pz["running"] is None and not force:
            raise ApiError(409, "无法检测服务器进程状态，请确认服务器已停止后勾选强制执行")

        if len(cells) > MAX_CELLS:
            raise ApiError(400, f"一次最多选择 {MAX_CELLS} 个区块，请缩小范围")

        # 组装文件路径
        planned = []
        missing = 0
        for cx, cy in cells:
            if fmt == "b42":
                f = map_dir / str(cx) / f"{cy}.bin"
            else:
                f = map_dir / f"r{cx}_{cy}.bin"
            key = (int(cx), int(cy))
            if f.is_file():
                planned.append((key, f))
            else:
                missing += 1
        if not planned:
            return {"deleted": 0, "missing": missing, "bytes": 0,
                    "backupDir": None, "running": pz["running"]}

        ts = time.strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^\w\-.]+", "_", name) or "save"
        dest_root = backup_root() / f"{safe_name}_{ts}"
        moved = 0
        total = 0
        errors = []
        for key, f in planned:
            size = f.stat().st_size
            try:
                if mode == "backup":
                    rel = f.relative_to(map_dir)
                    dest = dest_root / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), str(dest))
                else:
                    f.unlink()
                chunks.pop(key, None)
                moved += 1
                total += size
            except Exception as e:  # noqa: BLE001
                errors.append(f"{f.name}: {e}")
        return {
            "deleted": moved,
            "missing": missing,
            "bytes": total,
            "backupDir": str(dest_root) if mode == "backup" else None,
            "errors": errors,
            "running": pz["running"],
        }


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"PZChunkReset/{APP_VERSION}"
    protocol_version = "HTTP/1.1"

    # ---- helpers ----------------------------------------------------
    def _send(self, code, body: bytes, ctype: str, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def send_err(self, code, msg):
        self.send_json({"error": msg}, code)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 10 * 1024 * 1024:
            raise ApiError(400, "请求体为空或过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(400, "JSON 解析失败")
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return data

    # ---- routing ----------------------------------------------------
    def do_GET(self):
        try:
            self._route_get()
        except ApiError as e:
            self.send_err(e.code, e.msg)
        except Exception as e:  # noqa: BLE001
            self.send_err(500, f"服务器内部错误: {e}")

    def do_POST(self):
        try:
            self._route_post()
        except ApiError as e:
            self.send_err(e.code, e.msg)
        except Exception as e:  # noqa: BLE001
            self.send_err(500, f"服务器内部错误: {e}")

    def _route_get(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/api/bootstrap":
            return self._api_bootstrap()
        if path == "/api/chunks":
            return self.send_json(chunk_payload() or {"error": "未选择存档"}, 200)
        if path == "/api/map":
            return self._api_map()
        if path == "/api/pz":
            return self.send_json(check_pz())
        m = TILE_PATH_RE.match(path)
        if m:
            return self._tile(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        self.send_err(404, "not found")

    def _route_post(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        if path == "/api/select":
            data = self.read_json()
            p = data.get("path") or ""
            if not p:
                raise ApiError(400, "缺少 path")
            info = select_save(p)
            return self.send_json({"ok": True, **info, "chunks": chunk_payload()})
        if path == "/api/prefetch":
            return self._do_prefetch()
        if path == "/api/rescan":
            with _cur_lock:
                cur = str(_current["map_dir"] or "")
            if not cur:
                raise ApiError(400, "尚未选择存档")
            select_save(cur, persist=False)
            return self.send_json({"ok": True, "chunks": chunk_payload()})
        if path == "/api/reset":
            data = self.read_json()
            if (data.get("confirm") or "").strip() != CONFIRM_WORD:
                raise ApiError(400, f'请在确认框输入「{CONFIRM_WORD}」')
            mode = data.get("mode") or "backup"
            if mode not in ("backup", "hard"):
                mode = "backup"
            force = bool(data.get("force"))
            cells = data.get("cells")
            if not cells:
                rect = data.get("rect")
                if not (isinstance(rect, (list, tuple)) and len(rect) == 4):
                    raise ApiError(400, "缺少选择范围 cells 或 rect")
                x0, y0, x1, y1 = [int(v) for v in rect]
                x0, x1 = sorted((x0, x1))
                y0, y1 = sorted((y0, y1))
                if (x1 - x0 + 1) * (y1 - y0 + 1) > MAX_CELLS:
                    raise ApiError(400, f"范围过大（超过 {MAX_CELLS} 区块）")
                cells = [[x, y] for x in range(x0, x1 + 1)
                         for y in range(y0, y1 + 1)]
            else:
                if not isinstance(cells, list):
                    raise ApiError(400, "cells 必须是数组")
                norm = []
                for c in cells:
                    if isinstance(c, (list, tuple)) and len(c) == 2:
                        norm.append([int(c[0]), int(c[1])])
                    else:
                        raise ApiError(400, "cells 元素必须是 [x,y]")
                cells = norm
            report = do_reset(cells, mode, force)
            report["ok"] = True
            report["chunks"] = chunk_payload()
            report["pz"] = check_pz(force=True)
            return self.send_json(report)
        self.send_err(404, "not found")

    # ---- endpoints ---------------------------------------------------
    def _api_bootstrap(self):
        saves = list_saves()
        selected = _current["map_dir"] and str(_current["map_dir"])
        if selected is None:
            want = _config.get("selected_save")
            if want and Path(want).is_dir():
                selected = want
            elif saves:
                # 优先 servertest
                pick = next((s for s in saves
                             if s["path"].rstrip("/\\").endswith("servertest")), saves[0])
                selected = pick["path"]
            if selected:
                try:
                    select_save(selected, persist=False)
                except Exception:
                    selected = None
        try:
            meta = load_map_meta()
            map_cfg, map_err = meta.to_dict(), None
        except Exception as e:  # noqa: BLE001
            map_cfg, map_err = None, str(e)
        return self.send_json({
            "version": APP_VERSION,
            "saves": saves,
            "selected": selected,
            "chunks": chunk_payload(),
            "map": map_cfg,
            "mapError": map_err,
            "pz": check_pz(),
            "config": {
                "chunkDivisor": int(_config.get("chunk_divisor", 8)),
                "backupRoot": str(backup_root()),
                "confirmWord": CONFIRM_WORD,
                "maxCells": MAX_CELLS,
                "mapSource": _config.get("map_source", "auto"),
            },
        })

    def _api_map(self):
        try:
            meta = load_map_meta()
            return self.send_json(meta.to_dict())
        except Exception as e:  # noqa: BLE001
            return self.send_err(502, str(e))

    # ---- tiles --------------------------------------------------------
    def _tile(self, z, x, y):
        try:
            meta = load_map_meta()
        except Exception as e:  # noqa: BLE001
            return self.send_err(502, f"底图源不可用: {e}")
        if not tile_range_ok(meta, z, x, y):
            return self.send_err(404, "tile out of range")
        status, data = warm_tile(meta, z, x, y, wait=True, want_data=True)
        if status == "ok" and data is not None:
            return self._send(200, data, tile_mime(meta),
                              {"Cache-Control": "public, max-age=31536000, immutable"})
        if status == "missing":
            return self.send_err(404, "no tile")
        return self.send_err(502, "上游瓦片下载失败，请稍后重试")

    def _do_prefetch(self):
        """后台预热一批瓦片（视野移动后调用，之后平移/缩放即读本地缓存）。"""
        data = self.read_json()
        try:
            z = int(data.get("z"))
            x0, y0 = int(data.get("x0")), int(data.get("y0"))
            x1, y1 = int(data.get("x1")), int(data.get("y1"))
        except Exception:
            raise ApiError(400, "参数必须是整数")
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        if x1 - x0 + 1 > 16 or y1 - y0 + 1 > 16:
            raise ApiError(400, "预取范围过大")
        try:
            meta = load_map_meta()
        except Exception as e:  # noqa: BLE001
            raise ApiError(502, f"底图源不可用: {e}")
        if not tile_range_ok(meta, z, x0, y0):
            return self.send_json({"queued": 0, "cached": 0,
                                   "avgMs": upstream_avg_ms()})
        x1 = min(x1, x0 + 15)
        y1 = min(y1, y0 + 15)
        queued, cached = submit_prefetch(meta, z, x0, y0, x1, y1)
        return self.send_json({"queued": queued, "cached": cached,
                               "avgMs": upstream_avg_ms()})

    # ---- static -------------------------------------------------------
    def _static(self, rel: str):
        rel = rel.replace("\\", "/")
        if rel.startswith("/") or ".." in rel.split("/"):
            return self.send_err(403, "forbidden")
        fpath = STATIC_DIR / rel
        if not fpath.is_file():
            return self.send_err(404, "not found")
        ctype = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "application/json"):
            ctype += "; charset=utf-8"
        data = fpath.read_bytes()
        if rel == "index.html":
            nonce = time.strftime("%H:%M:%S").encode() + \
                b" pid=" + str(os.getpid()).encode()
            data = data.replace(b"<!--NONCE-->",
                                b'<span class="nonce">[' + nonce + b"]</span>")
        self._send(200, data, ctype, {"Cache-Control": "no-store"})

    def log_request(self, code="-", size="-"):
        print(time.strftime("%H:%M:%S"), f"{self.command} {self.path} -> {code}",
              flush=True)

    def log_message(self, fmt, *args):
        pass  # 静默访问日志


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def smoke_check() -> int:
    """启动自检：依赖、静态资源、底图源、存档扫描、进程检测。"""
    import json as _json
    result = {"version": APP_VERSION, "ok": True, "checks": {}}

    result["checks"]["python"] = sys.version.split()[0]
    result["checks"]["static"] = STATIC_DIR.joinpath("index.html").is_file()

    try:
        import curl_cffi  # noqa: F401
        result["checks"]["curl_cffi"] = True
    except Exception as e:  # noqa: BLE001
        result["checks"]["curl_cffi"] = f"缺失({e})；将回退 pzmap.net"

    try:
        meta = load_map_meta()
        result["checks"]["map_source"] = meta.src["id"]
        result["checks"]["map_size"] = [meta.w, meta.h]
    except Exception as e:  # noqa: BLE001
        result["checks"]["map_source"] = f"失败: {e}"
        result["ok"] = False

    saves = list_saves()
    result["checks"]["saves_found"] = len(saves)

    pz = check_pz(force=True)
    result["checks"]["pz_running"] = pz["running"]

    result["checks"]["backup_root"] = str(backup_root())
    print(_json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def main():
    ap = argparse.ArgumentParser(description=APP_TITLE)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--save", default=None, help="初始存档 map 目录")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--smoke", action="store_true",
                    help="运行自检并输出 JSON 后退出")
    args = ap.parse_args()

    load_config()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if args.smoke:
        sys.exit(smoke_check())

    host = args.host or _config.get("host") or "127.0.0.1"
    port = args.port or int(_config.get("port") or 8765)

    if args.save:
        try:
            select_save(args.save, persist=True)
        except Exception as e:  # noqa: BLE001
            print(f"[警告] 指定存档加载失败: {e}")

    # 预热底图配置（不阻塞启动失败）
    threading.Thread(target=lambda: load_map_meta(), daemon=True).start()

    httpd = None
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer((host, p), Handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        print(f"[错误] 端口 {port}~{port+19} 均被占用")
        sys.exit(1)
    httpd.daemon_threads = True

    url = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}/"
    print(f"{APP_TITLE} v{APP_VERSION}")
    print(f"  地址: {url}")
    if host == "0.0.0.0":
        print("  [提示] 已允许局域网访问，请确保防火墙放行该端口")
    print("  按 Ctrl+C 退出")

    if _config.get("open_browser") and not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
