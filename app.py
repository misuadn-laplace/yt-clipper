#!/usr/bin/env python3
"""YT Clipper: a local web app for cutting clips out of YouTube videos and ended livestreams,
and for downloading whole videos from YouTube and the other sites yt-dlp supports.

Open the "YT Clipper" shortcut (or run: pythonw app.py [link]) and it opens in your browser.
It runs in the background without a window and quits by itself once no page has been open for a while.
Everything runs on this computer; the page is only reachable from this machine.
"""
import collections
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import urlopen

from ytclip import DEFAULT_OUT, build_command, find_ffmpeg, hms, safe_name, split_args

HERE = Path(__file__).resolve().parent

if sys.stdout is None or sys.stderr is None:  # started with pythonw: no console, so log to a file
    sys.stdout = sys.stderr = open(HERE / "app.log", "w", encoding="utf-8", buffering=1)

INDEX = HERE / "web" / "index.html"
PORT = 8765
IDLE_SECONDS = 600  # quit after 10 minutes with no open page and nothing downloading
WIN = sys.platform == "win32"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if WIN else 0
RUNNING = ("starting", "downloading", "finishing")
ACTIVE = ("queued", *RUNNING)
FINISHED = ("done", "failed", "cancelled")
last_seen = time.time()

URL = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.I)
YOUTUBE = re.compile(r"^https?://([a-z0-9-]+\.)*(youtube\.com|youtu\.be|youtube-nocookie\.com)/", re.I)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
PROGRESS = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
SPEED = re.compile(r"speed=\s*([\d.]+)x")
FORMATS = re.compile(r"Downloading \d+ format\(s\): (\S+)")
FINISHING = ("[Merger]", "[Fixup", "[VideoConvertor]", "[MoveFiles]", "[ExtractAudio]", "[Embed",
             "[Metadata]", "[ThumbnailsConvertor]", "[SubtitlesConvertor]")

jobs = {}
jobs_lock = threading.RLock()
info_cache = {}
order = itertools.count()


class ApiError(Exception):
    pass


def clean_error(msg):
    msg = ANSI.sub("", str(msg)).strip()
    return re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*[\w-]+:\s*)?", "", msg)


def check_url(url):
    url = (url or "").strip()
    if len(url) > 2000 or not URL.match(url):
        raise ApiError("That doesn't look like a link.")
    return url


def youtube_id(url):
    if not YOUTUBE.match(url):
        return ""
    parts = urlparse(url)
    query = parse_qs(parts.query)
    if "v" in query:
        return query["v"][0][:11]
    pattern = r"^/([\w-]{11})" if "youtu.be" in parts.netloc else r"^/(?:live|shorts|embed)/([\w-]{11})"
    m = re.match(pattern, parts.path)
    return m[1] if m else ""


def is_playlist(url):
    if not YOUTUBE.match(url):
        return False
    parts = urlparse(url)
    query = parse_qs(parts.query)
    return parts.path.rstrip("/") == "/playlist" or ("list" in query and "v" not in query)


def tool_env():
    """Environment for yt-dlp runs: UTF-8 output, and this Python's Scripts folder on PATH so Deno is found."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    dirs = [sysconfig.get_path("scripts"), sysconfig.get_path("scripts", f"{os.name}_user")]
    env["PATH"] = os.pathsep.join([d for d in dirs if d and os.path.isdir(d)] + [env.get("PATH", "")])
    return env


def run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, env=tool_env(), creationflags=NO_WINDOW, timeout=timeout)


def last_error(stderr, fallback):
    lines = [line for line in stderr.splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("ERROR:")]
    return clean_error((errors or lines or [fallback])[-1])


def printable(cmd):
    return subprocess.list2cmdline(cmd) if WIN else shlex.join(cmd)


# ---------- settings & history ----------

def config_dir():
    if WIN:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    path = base / "YT Clipper"
    path.mkdir(parents=True, exist_ok=True)
    return path


CONFIG = config_dir()
SETTINGS_FILE = CONFIG / "settings.json"
HISTORY_FILE = CONFIG / "history.json"

DEFAULT_SETTINGS = {
    "folder": str(DEFAULT_OUT),
    "concurrency": 1,          # downloads running at the same time
    "rate_limit": None,        # KB/s, None = no limit
    "mtime_now": True,         # file date = time downloaded (instead of the upload date)
    "embed_thumbnail": False,
    "embed_chapters": True,    # full videos
    "subtitles": "off",        # full videos: off | embed | srt
    "sub_langs": "en",
    "custom_args": "",         # extra yt-dlp arguments
}


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


stored = read_json(SETTINGS_FILE, {})
settings = {**DEFAULT_SETTINGS, **{k: v for k, v in stored.items() if k in DEFAULT_SETTINGS}} \
    if isinstance(stored, dict) else dict(DEFAULT_SETTINGS)


def apply_settings(changes):
    new = dict(settings)
    for key, value in changes.items():
        if key not in DEFAULT_SETTINGS:
            continue
        try:
            if key == "folder":
                path = Path(os.path.expanduser(str(value or "").strip()))
                if not path.is_absolute():
                    raise ApiError("Use a full folder path.")
                try:
                    path.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    raise ApiError(f"Can't use that folder: {e.strerror or e}")
                value = str(path)
            elif key == "concurrency":
                value = min(max(int(value), 1), 4)
            elif key == "rate_limit":
                value = int(value) if value not in (None, "") else None
                value = value if value and value > 0 else None
            elif key in ("mtime_now", "embed_thumbnail", "embed_chapters"):
                value = bool(value)
            elif key == "subtitles":
                if value not in ("off", "embed", "srt"):
                    raise ApiError("Unknown subtitles option.")
            elif key == "sub_langs":
                value = re.sub(r"\s+", "", str(value or "")) or "en"
                if not re.fullmatch(r"[\w.*,-]+", value):
                    raise ApiError("Languages look like: en,ro")
            elif key == "custom_args":
                value = str(value or "").strip()
                split_args(value)
        except (TypeError, ValueError):
            raise ApiError("That setting doesn't look right.")
        new[key] = value
    settings.update(new)
    write_json(SETTINGS_FILE, settings)
    pump()
    return settings


def public(job):
    return {k: v for k, v in job.items() if not k.startswith("_")}


def save_history():
    with jobs_lock:
        items = sorted((public(j) for j in jobs.values() if j["status"] in FINISHED),
                       key=lambda j: j.get("created", 0))
    try:
        write_json(HISTORY_FILE, items[-300:])
    except OSError:
        pass


def load_history():
    items = read_json(HISTORY_FILE, [])
    for j in items if isinstance(items, list) else []:
        if isinstance(j, dict) and j.get("id"):
            if j.get("status") not in FINISHED:
                j["status"] = "cancelled"
            jobs[j["id"]] = j


# ---------- video info ----------

def preview_source(info):
    """Something the browser can play for a non-YouTube video: an mp4/webm file, or an HLS stream for hls.js."""
    fmts = [f for f in (info.get("formats") or [info]) if f.get("url")]

    def near_720(f):
        h = f.get("height") or 480
        return (h <= 720, h if h <= 720 else -h)

    files = [f for f in fmts if f.get("vcodec") != "none" and f.get("acodec") != "none"
             and str(f.get("protocol") or "http").startswith("http") and f.get("ext") in ("mp4", "webm", "m4v", "mov")]
    if files:
        return {"type": "file", "url": max(files, key=near_720)["url"]}
    hls = [f for f in fmts if "m3u8" in str(f.get("protocol") or "") and f.get("vcodec") != "none"]
    if hls:
        f = max(hls, key=near_720)
        return {"type": "hls", "url": f.get("manifest_url") or f["url"]}
    return None


def video_info(url):
    cached = info_cache.get(url)
    if cached and time.time() - cached[0] < 3600:
        return cached[1]

    # yt-dlp runs as a separate process so an update takes effect without restarting the app.
    try:
        r = run([sys.executable, "-m", "yt_dlp", "-J", "--flat-playlist", "--no-playlist", "--no-warnings", "--", url])
    except subprocess.TimeoutExpired:
        raise ApiError("The site took too long to answer. Try again.")
    if r.returncode != 0:
        raise ApiError(last_error(r.stderr, "Couldn't read this link."))
    info = json.loads(r.stdout)

    if info.get("_type") == "playlist":
        return {"playlist": True, "title": info.get("title") or "", "count": len(info.get("entries") or [])}
    youtube = info.get("extractor_key") == "Youtube"
    status = info.get("live_status")
    if status == "is_live":
        raise ApiError("This stream is still live. Wait until it ends, then try again.")
    if status == "is_upcoming":
        raise ApiError("This stream hasn't started yet.")
    if youtube and not info.get("duration"):
        raise ApiError("Couldn't find the video length. If the stream just ended, YouTube may still be processing it.")

    formats = info.get("formats") or []
    # Preview thumbnails ("storyboards"): sprite sheets of small frames, the same ones YouTube shows on hover.
    boards = [f for f in formats if f.get("format_note") == "storyboard" and f.get("fragments") and f.get("rows")]
    board = max(boards, key=lambda f: f.get("width") or 0, default=None)
    storyboard = None
    if board:
        per_sheet = board["rows"] * board["columns"]
        interval = 1 / board["fps"] if board.get("fps") else board["fragments"][0]["duration"] / per_sheet
        storyboard = {
            "width": board["width"], "height": board["height"],
            "rows": board["rows"], "cols": board["columns"],
            "interval": interval,
            "urls": [fr["url"] for fr in board["fragments"]],
        }

    best_fps = {}
    for f in formats:
        if f.get("height") and f.get("vcodec") != "none" and f.get("ext") != "mhtml":
            best_fps[f["height"]] = max(best_fps.get(f["height"], 0), f.get("fps") or 0)
    qualities = [{"height": h, "label": f"{h}p{round(fps)}" if fps and fps > 30 else f"{h}p"}
                 for h, fps in sorted(best_fps.items(), reverse=True)]

    result = {
        "id": info.get("id") or "",
        "url": info.get("webpage_url") or url,
        "youtube": youtube,
        "site": info.get("extractor_key") or "",
        "title": info.get("title") or info.get("id") or url,
        "channel": info.get("channel") or info.get("uploader") or "",
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "width": info.get("width"),
        "height": info.get("height"),
        "qualities": qualities,
        "was_live": status in ("was_live", "post_live"),
        "post_live": status == "post_live",
        "embeddable": info.get("playable_in_embed", True) is not False,
        "chapters": [{"start": c["start_time"], "end": c["end_time"], "title": c.get("title") or ""}
                     for c in info.get("chapters") or []],
        "heatmap": [{"start": h["start_time"], "end": h["end_time"], "value": h["value"]}
                    for h in info.get("heatmap") or []],
        "storyboard": storyboard,
        "preview": None if youtube else preview_source(info),
    }
    info_cache[url] = (time.time(), result)
    return result


def expand_playlist(url):
    """[(link, title, youtube id)] for every video in a playlist (or just the one video)."""
    try:
        r = run([sys.executable, "-m", "yt_dlp", "-J", "--flat-playlist", "--no-warnings", "--", url], timeout=300)
    except subprocess.TimeoutExpired:
        raise ApiError("The playlist took too long to load.")
    if r.returncode != 0:
        raise ApiError(last_error(r.stderr, "Couldn't read that playlist."))
    info = json.loads(r.stdout)
    if info.get("_type") != "playlist":
        return [(info.get("webpage_url") or url, info.get("title"), youtube_id(info.get("webpage_url") or url))]
    items = []
    for e in info.get("entries") or []:
        if not e:
            continue
        link = e.get("url") or e.get("webpage_url")
        if e.get("ie_key") == "Youtube" and e.get("id") and not str(link or "").startswith("http"):
            link = f"https://www.youtube.com/watch?v={e['id']}"
        if link and URL.match(link):
            items.append((link, e.get("title"), youtube_id(link)))
    return items[:500]


# ---------- download queue ----------

def new_job(kind, url, **fields):
    job = {
        "id": uuid.uuid4().hex[:10], "kind": kind, "url": url, "title": url, "status": "queued",
        "progress": 0.0, "speed": None, "speed_unit": None, "eta": None, "created": time.time(), "seq": next(order),
        "start": None, "end": None, "fast": False, "quality": None, "crop": None, "crop_pos": 0.5, "name": None,
        "video_id": "", "thumb": None,
    }
    job.update({k: v for k, v in fields.items() if v is not None or k not in ("title",)})
    job["title"] = job["title"] or url
    with jobs_lock:
        jobs[job["id"]] = job
    return job


def pump():
    """Start queued downloads while there's room (frame grabs never wait)."""
    starting = []
    with jobs_lock:
        busy = sum(1 for j in jobs.values() if j["status"] in RUNNING and j["kind"] != "frame")
        for j in sorted((j for j in jobs.values() if j["status"] == "queued"), key=lambda j: j.get("seq", 0)):
            if j["kind"] == "frame" or busy < settings["concurrency"]:
                j["status"] = "starting"
                busy += j["kind"] != "frame"
                starting.append(j)
    for j in starting:
        threading.Thread(target=run_frame if j["kind"] == "frame" else run_job, args=(j,), daemon=True).start()


def finish(job):
    job["finished"] = time.time()
    save_history()
    pump()


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return ""


def handle_line(job, line):
    if line.startswith("@@["):
        if job["kind"] == "clip":
            return  # clips report progress through ffmpeg's time= lines
        try:
            done, total, est, speed, eta, fmt_id, title = json.loads(
                re.sub(r"(?<=[\[,])NA(?=[,\]])", "null", line[2:]))
        except (ValueError, TypeError):
            return
        if title and job["title"] == job["url"]:
            job["title"] = title
        total = total or est
        seen = job.setdefault("_fmts", [])
        if fmt_id and fmt_id not in seen:
            seen.append(fmt_id)
        part = seen.index(fmt_id) if fmt_id in seen else 0
        parts = max(job.get("_parts", 1), len(seen), 1)
        frac = min(done / total, 1.0) if done and total else 0.0
        job.update(status="downloading", progress=min((part + frac) / parts, 0.999),
                   speed=speed, speed_unit="bytes", eta=eta)
        job["_last"] = (f"[download] {frac * 100:.1f}% of {human_bytes(total or 0)}"
                        + (f" at {human_bytes(speed)}/s" if speed else "") + (f", {int(eta)} s left" if eta else ""))
        return
    m = PROGRESS.search(line)
    if m:
        done = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
        length = (job["end"] - job["start"]) if job["kind"] == "clip" else None
        job["_last"] = line
        if length:
            job["progress"] = min(done / length, 0.999)
            job["status"] = "downloading"
            s = SPEED.search(line)
            if s and float(s[1]) > 0:
                job.update(speed=float(s[1]), speed_unit="x", eta=max(0, (length - done) / float(s[1])))
        return

    job["_log"].append(line)
    m = FORMATS.search(line)
    if m:
        job["_parts"] = m[1].count("+") + 1
    if line.startswith("[download] Destination:"):
        job.setdefault("_dests", []).append(line.split(":", 1)[1].strip())
        job["status"] = "downloading"
    elif line.startswith("[download]") and line.endswith("has already been downloaded"):
        job.setdefault("_dests", []).append(line[len("[download]"):-len("has already been downloaded")].strip())
    elif line.startswith(FINISHING):
        job["status"] = "finishing"
    elif line.startswith("ERROR:"):
        job["error"] = clean_error(line)


def run_job(job):
    job["_log"] = collections.deque(maxlen=400)
    fd, result_file = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    code = None
    saved = []
    try:
        opts = dict(settings)
        cmd = build_command(job["url"], opts["folder"], result_file, start=job["start"], end=job["end"],
                            fast=job["fast"], quality=job["quality"], crop=job["crop"], crop_pos=job["crop_pos"],
                            name=job["name"], opts=opts, app=True)
        job["_cmd"] = printable(cmd)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=tool_env(), creationflags=NO_WINDOW)
        job["_proc"] = proc
        if job.get("cancelled"):
            kill_tree(proc)
        buf = b""
        while chunk := proc.stdout.read1(4096):
            buf += chunk
            *lines, buf = re.split(rb"[\r\n]", buf)
            for raw in lines:
                if raw.strip():
                    handle_line(job, raw.decode("utf-8", "replace").strip())
        if buf.strip():
            handle_line(job, buf.decode("utf-8", "replace").strip())
        code = proc.wait()
        saved = Path(result_file).read_text(encoding="utf-8").strip().splitlines()
    except Exception as e:  # noqa: BLE001 - shown to the user in the downloads list
        job["error"] = clean_error(e)
        job["_log"].append(f"ERROR: {e}")
    finally:
        try:
            os.remove(result_file)
        except OSError:
            pass

    dests = job.get("_dests") or []
    final = saved[-1] if saved else (dests[-1] if dests else None)
    if job.get("cancelled"):
        job["status"] = "cancelled"
        for dest in dests:
            remove_partial(dest)
    elif code == 0 and final and Path(final).exists():
        if job["title"] == job["url"]:
            job["title"] = Path(final).stem
        job.update(status="done", progress=1.0, eta=0, file=final, size=Path(final).stat().st_size)
    else:
        job["status"] = "failed"
        job["error"] = job.get("error") or "Download failed."
    finish(job)


def run_frame(job):
    """Save one full-quality frame as a PNG."""
    job["_log"] = collections.deque(maxlen=100)
    job["status"] = "downloading"
    try:
        r = run([sys.executable, "-m", "yt_dlp", "-J", "--no-playlist", "--no-warnings", "-f", "bv*/b", "--", job["url"]])
        if r.returncode != 0:
            raise RuntimeError(last_error(r.stderr, "Couldn't read this video."))
        info = json.loads(r.stdout)
        src = info.get("url") or next((f.get("url") for f in info.get("requested_formats") or []
                                       if f.get("vcodec") != "none"), None)
        if not src:
            raise RuntimeError("Couldn't find the video stream.")
        if job["title"] == job["url"]:
            job["title"] = info.get("title") or job["title"]
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found.")
        folder = Path(settings["folder"])
        folder.mkdir(parents=True, exist_ok=True)
        t = float(job["start"])
        whole, tenth = divmod(round(t * 10), 10)
        name = safe_name(job["title"], template=False) or "Frame"
        out = folder / f"{name} [frame {hms(whole, '.')}.{tenth}].png"
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        headers = info.get("http_headers") or {}
        if headers:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        cmd += ["-ss", f"{t:.3f}", "-i", src, "-frames:v", "1", "-update", "1", str(out)]
        job["_cmd"] = printable(cmd[:cmd.index("-i") + 1] + ["<video stream>"] + cmd[cmd.index("-i") + 2:])
        r = run(cmd, timeout=180)
        job["_log"].extend(line for line in r.stderr.splitlines() if line.strip())
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(last_error(r.stderr, "Couldn't save the frame."))
        job.update(status="done", progress=1.0, file=str(out), size=out.stat().st_size)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
        job.update(status="failed", error=clean_error(e))
    finish(job)


def kill_tree(proc):
    if proc.poll() is not None:
        return
    if WIN:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, creationflags=NO_WINDOW)
    else:
        proc.kill()


def remove_partial(dest):
    if not dest:
        return
    for path in (dest + ".part", dest + ".ytdl", dest):
        for _ in range(10):  # the file can stay locked for a moment after ffmpeg is killed
            try:
                if os.path.exists(path):
                    os.remove(path)
                break
            except OSError:
                time.sleep(0.3)


def open_path(path, reveal=False):
    """Open a file or folder with its default app, or show the file selected in Explorer / Finder."""
    path = str(path)
    if WIN:
        if reveal:
            subprocess.Popen(f'explorer /select,"{path}"')
        else:
            os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) if reveal else path])


picker_lock = threading.Lock()


def pick_folder(start):
    """Show the system folder picker and return the chosen path ('' if cancelled)."""
    if sys.platform == "darwin":
        r = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "Save clips to")'],
                           capture_output=True, text=True)
        return r.stdout.strip()
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        raise ApiError("The folder picker isn't available. Type the path instead.")
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    try:
        return filedialog.askdirectory(parent=root, initialdir=start, title="Save clips to", mustexist=False) or ""
    finally:
        root.destroy()


# ---------- tool versions & updates ----------

versions_cache = {"at": 0, "data": None}
update_state = {"running": False, "message": "", "ok": None}


def version_key(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def first_match(cmd, pattern):
    try:
        r = run(cmd, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(pattern, r.stdout) if r.returncode == 0 else None
    return m[1] if m else None


def latest_ytdlp():
    try:
        with urlopen("https://pypi.org/pypi/yt-dlp/json", timeout=8) as r:
            return json.load(r)["info"]["version"]
    except Exception:  # noqa: BLE001 - offline is fine, we just can't tell
        return None


def tool_versions(force=False):
    if not force and versions_cache["data"] and time.time() - versions_cache["at"] < 3600:
        return versions_cache["data"]
    ffmpeg = find_ffmpeg()
    deno = shutil.which("deno", path=tool_env()["PATH"])
    data = {
        "ytdlp": first_match([sys.executable, "-m", "yt_dlp", "--version"], r"(\S+)"),
        "ytdlp_latest": latest_ytdlp(),
        "ffmpeg": first_match([ffmpeg, "-version"], r"ffmpeg version (\S+)") if ffmpeg else None,
        "deno": first_match([deno, "--version"], r"deno (\S+)") if deno else None,
    }
    if data["ffmpeg"] and data["ffmpeg"][0].isdigit():
        data["ffmpeg"] = data["ffmpeg"].split("-")[0]  # "9.0.2-full_build-www.gyan.dev" -> "9.0.2"
    data["update_available"] = bool(data["ytdlp"] and data["ytdlp_latest"]
                                    and version_key(data["ytdlp_latest"]) > version_key(data["ytdlp"]))
    versions_cache.update(at=time.time(), data=data)
    return data


def run_update():
    before = tool_versions()
    notes = []
    ok = True
    try:
        pip = [sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check",
               "yt-dlp[default]", "deno"]
        r = run(pip, timeout=600)
        if r.returncode != 0:  # e.g. Python installed for all users: fall back to a per-user install
            r = run(pip[:4] + ["--user"] + pip[4:], timeout=600)
        if r.returncode != 0:
            ok = False
            notes.append("yt-dlp update failed: " + last_error(r.stderr, "pip error"))

        ffmpeg = find_ffmpeg() or ""
        if WIN and ("Gyan.FFmpeg" in ffmpeg or "WinGet" in ffmpeg):  # only an ffmpeg that winget installed
            r = run(["winget", "upgrade", "--id", "Gyan.FFmpeg", "-e", "--source", "winget",
                     "--disable-interactivity"], timeout=900)
            out = r.stdout + r.stderr
            if r.returncode != 0 and not re.search(r"No (applicable|available) upgrade|No newer package", out, re.I):
                notes.append("FFmpeg update failed")
    except (OSError, subprocess.TimeoutExpired) as e:
        ok = False
        notes.append(f"Update failed: {e}")

    after = tool_versions(force=True)
    changed = [f"{name} {after[key]}" for key, name in (("ytdlp", "yt-dlp"), ("ffmpeg", "FFmpeg"), ("deno", "Deno"))
               if after.get(key) and after.get(key) != before.get(key)]
    if changed:
        notes.insert(0, "Updated to " + ", ".join(changed))
    elif ok and not notes:
        notes.append("Everything is up to date")
    update_state.update(running=False, ok=ok and not any("failed" in n for n in notes), message=". ".join(notes))


# ---------- API ----------

def job_fields(body):
    kind = body.get("kind") or "clip"
    if kind not in ("clip", "video"):
        raise ApiError("Unknown download type.")
    quality = body.get("quality") or None
    if quality is not None and not (str(quality).isdigit() or quality in ("audio-mp3", "audio-m4a")):
        raise ApiError("Unknown quality.")
    thumb = str(body.get("thumb") or "")
    fields = {
        "quality": str(quality) if quality else None,
        "fast": bool(body.get("fast")),
        "title": str(body.get("title") or "")[:300] or None,
        "video_id": str(body.get("video_id") or "")[:20],
        "thumb": thumb if thumb.startswith("https://") and len(thumb) < 2000 else None,
    }
    if kind == "clip":
        try:
            start, end = float(body["start"]), float(body["end"])
            crop_pos = min(max(float(body.get("crop_pos", 0.5)), 0.0), 1.0)
        except (KeyError, TypeError, ValueError):
            raise ApiError("Missing start or end time.")
        if start < 0 or end - start < 0.5:
            raise ApiError("The clip must be at least half a second long.")
        fields.update(start=round(start, 2), end=round(end, 2), crop_pos=crop_pos,
                      crop=body.get("crop") if body.get("crop") in ("9:16", "1:1") else None,
                      name=safe_name(body.get("name"), template=False) or None)
    return kind, fields


def no_update_running():
    if update_state["running"]:
        raise ApiError("Updating the tools. Try again in a minute.")


def api_info(body):
    return video_info(check_url(body.get("url")))


def api_download(body):
    url = check_url(body.get("url"))
    no_update_running()
    kind, fields = job_fields(body)
    with jobs_lock:
        for j in jobs.values():
            if (j["status"] in ACTIVE and j["url"] == url and j["kind"] == kind
                    and all(j.get(k) == fields.get(k) for k in ("start", "end", "quality", "crop", "name"))):
                raise ApiError("That one is already in the queue.")
        job = new_job(kind, url, **fields)
    pump()
    return public(job)


def api_queue(body):
    """Queue whole videos: several links and/or playlists."""
    no_update_running()
    urls = [check_url(u) for u in (body.get("urls") or [])][:200]
    if not urls:
        raise ApiError("No links to download.")
    _, fields = job_fields({"kind": "video", "quality": body.get("quality")})
    items = []
    for url in urls:
        items += expand_playlist(url) if is_playlist(url) or body.get("expand") else [(url, None, youtube_id(url))]
    for link, title, vid in items:
        new_job("video", link, **{**fields, "title": title, "video_id": vid or ""})
    pump()
    return {"count": len(items)}


def api_frame(body):
    url = check_url(body.get("url"))
    try:
        t = max(float(body["t"]), 0.0)
    except (KeyError, TypeError, ValueError):
        raise ApiError("Missing the time.")
    job = new_job("frame", url, start=round(t, 2), title=str(body.get("title") or "")[:300] or None,
                  video_id=str(body.get("video_id") or "")[:20])
    pump()
    return public(job)


def get_job(body):
    job = jobs.get(str(body.get("id")))
    if not job:
        raise ApiError("Unknown download.")
    return job


def cancel(job):
    if job["status"] == "queued":
        job["status"] = "cancelled"
        finish(job)
    elif job["status"] in RUNNING:
        job["cancelled"] = True
        if job.get("_proc"):
            kill_tree(job["_proc"])


def api_cancel(body):
    cancel(get_job(body))
    return {"ok": True}


def api_stop_all(_body):
    with jobs_lock:
        active = [j for j in jobs.values() if j["status"] in ACTIVE]
    for job in sorted(active, key=lambda j: j["status"] != "queued"):  # queued first so nothing new starts
        cancel(job)
    return {"ok": True}


def api_retry(body):
    old = get_job(body)
    no_update_running()
    keys = ("start", "end", "fast", "quality", "crop", "crop_pos", "name", "title", "video_id", "thumb")
    job = new_job(old.get("kind", "clip"), old["url"], **{k: old.get(k) for k in keys})
    with jobs_lock:
        jobs.pop(old["id"], None)
    save_history()
    pump()
    return public(job)


def api_log(body):
    job = get_job(body)
    lines = list(job.get("_log") or [])
    if job.get("_last") and job["status"] in RUNNING:
        lines.append(job["_last"])
    return {"cmd": job.get("_cmd"), "lines": lines}


def api_reveal(body):
    job = get_job(body)
    if job.get("file") and os.path.exists(job["file"]):
        open_path(job["file"], reveal=True)
    return {"ok": True}


def api_play(body):
    job = get_job(body)
    if job.get("file") and os.path.exists(job["file"]):
        open_path(job["file"])
    return {"ok": True}


def api_open_folder(_body):
    folder = Path(settings["folder"])
    folder.mkdir(parents=True, exist_ok=True)
    open_path(folder)
    return {"ok": True}


def api_clear(_body):
    with jobs_lock:
        for jid in [k for k, j in jobs.items() if j["status"] in FINISHED]:
            del jobs[jid]
    save_history()
    return {"ok": True}


def api_settings(body):
    return apply_settings(body) if body else settings


def api_pick_folder(_body):
    if not picker_lock.acquire(blocking=False):
        raise ApiError("The folder window is already open.")
    try:
        path = pick_folder(settings["folder"])
    finally:
        picker_lock.release()
    return apply_settings({"folder": path}) if path else settings


def api_versions(body):
    return {**tool_versions(force=bool(body.get("refresh"))), "update": update_state}


def api_update(_body):
    if update_state["running"]:
        return {"update": update_state}
    if any(j["status"] in ACTIVE for j in list(jobs.values())):
        raise ApiError("Wait for your downloads to finish, then update.")
    update_state.update(running=True, ok=None, message="")
    threading.Thread(target=run_update, daemon=True).start()
    return {"update": update_state}


ROUTES = {
    "/api/info": api_info,
    "/api/download": api_download,
    "/api/queue": api_queue,
    "/api/frame": api_frame,
    "/api/cancel": api_cancel,
    "/api/stop-all": api_stop_all,
    "/api/retry": api_retry,
    "/api/log": api_log,
    "/api/reveal": api_reveal,
    "/api/play": api_play,
    "/api/open-folder": api_open_folder,
    "/api/clear": api_clear,
    "/api/settings": api_settings,
    "/api/pick-folder": api_pick_folder,
    "/api/versions": api_versions,
    "/api/update": api_update,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "YTClipper"

    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def local_only(self):
        # Blocks other websites from talking to this server (DNS rebinding / cross-site requests).
        global last_seen
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host in ("127.0.0.1", "localhost"):
            last_seen = time.time()
            return True
        return False

    def do_GET(self):
        if not self.local_only():
            return self.send(403, {"error": "Forbidden"})
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self.send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/ping":
            return self.send(200, {"app": "yt-clipper"})
        if path == "/api/jobs":
            with jobs_lock:
                listed = sorted(jobs.values(), key=lambda j: j.get("created", 0), reverse=True)
                return self.send(200, {"jobs": [public(j) for j in listed], "folder": settings["folder"]})
        self.send(404, {"error": "Not found"})

    def do_POST(self):
        # Requiring a JSON content type forces a CORS preflight, which this server never approves.
        if not self.local_only() or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(403, {"error": "Forbidden"})
        fn = ROUTES.get(self.path)
        if not fn:
            return self.send(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            self.send(200, fn(body if isinstance(body, dict) else {}))
        except ApiError as e:
            self.send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self.send(500, {"error": f"{type(e).__name__}: {clean_error(e)}"})


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets two servers share a port, so leave it off there.
    allow_reuse_address = not WIN
    daemon_threads = True


def quit_when_idle(server):
    # An open page polls every second (at least once a minute in a background tab), so this only
    # fires once every tab is closed.
    while True:
        time.sleep(15)
        busy = any(j["status"] in ACTIVE for j in list(jobs.values())) or update_state["running"]
        if not busy and time.time() - last_seen > IDLE_SECONDS:
            print("No page open for a while; quitting.")
            server.shutdown()
            return


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    query = f"?url={quote(args[0], safe='')}" if args else ""
    url = f"http://127.0.0.1:{PORT}/"
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        try:
            with urlopen(url + "api/ping", timeout=2) as r:
                if b"yt-clipper" in r.read():
                    print("YT Clipper is already running; opening it in your browser.")
                    webbrowser.open(url + query)
                    return
        except OSError:
            pass
        server = Server(("127.0.0.1", 0), Handler)
        url = f"http://127.0.0.1:{server.server_address[1]}/"

    load_history()
    print(f"YT Clipper is running at {url}")
    print(f"Saving to {settings['folder']}")
    threading.Thread(target=quit_when_idle, args=(server,), daemon=True).start()
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, webbrowser.open, [url + query]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for job in list(jobs.values()):
            if job.get("_proc"):
                job["cancelled"] = True
                kill_tree(job["_proc"])


if __name__ == "__main__":
    main()
