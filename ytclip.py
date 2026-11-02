#!/usr/bin/env python3
"""Download just a section of a YouTube video, including long livestreams that have ended.

Only the part you ask for is downloaded, not the whole 3-hour stream.

Examples:
  python ytclip.py "https://www.youtube.com/watch?v=XXXX" 1:02:30 1:05:10
  python ytclip.py "https://youtu.be/XXXX" 2h10m 2h12m30s --fast
  python ytclip.py "https://youtu.be/XXXX" 45:00 47:00 --max-height 720 -o "C:\\Clips"
  python ytclip.py        (no arguments: it asks you for everything)
"""
import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_OUT = Path.home() / "Downloads" / "YT Clips"
WIN = sys.platform == "win32"


def parse_time(text):
    """Turn '1:02:03', '62:03', '3723', '1h2m3s', '1h02m' or '45.5' into seconds."""
    t = text.strip().lower().replace(" ", "").strip(",;")
    if ":" in t:
        parts = t.split(":")
        if len(parts) > 3:
            raise ValueError(text)
        secs = 0.0
        for p in parts:
            secs = secs * 60 + float(p)
        return secs
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s?)?", t)
    if not t or not m or not any(m.groups()):
        raise ValueError(text)
    h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mi * 60 + s


def hms(sec, sep=":"):
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}{sep}{m:02d}{sep}{s:02d}" if h else f"{m}{sep}{s:02d}"


def find_ffmpeg():
    """Full path to ffmpeg, or None. Also finds a winget install that isn't on this process's PATH yet."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet"
    for candidate in [winget / "Links" / "ffmpeg.exe",
                      *sorted((winget / "Packages").glob("Gyan.FFmpeg*/*/bin/ffmpeg.exe"), reverse=True)]:
        if candidate.exists():
            return str(candidate)
    return None


def find_ffmpeg_dir():
    """The folder to pass to yt-dlp's --ffmpeg-location, or None when ffmpeg is already on PATH."""
    exe = find_ffmpeg()
    if not exe:
        raise RuntimeError("ffmpeg not found. Install it (Windows: winget install Gyan.FFmpeg, "
                           "macOS: brew install ffmpeg, Linux: sudo apt install ffmpeg).")
    return None if shutil.which("ffmpeg") else str(Path(exe).parent)


# Social-media frames: target width/height ratio and the output size.
CROPS = {"9:16": (9 / 16, 1080, 1920), "1:1": (1.0, 1080, 1080)}

# Machine-readable progress for the app: downloaded, total, total estimate, speed, eta, format id, title.
PROGRESS_TEMPLATE = ("download:@@[%(progress.downloaded_bytes)j,%(progress.total_bytes)j,"
                     "%(progress.total_bytes_estimate)j,%(progress.speed)j,%(progress.eta)j,"
                     "%(info.format_id)j,%(info.title)j]")


def safe_name(name, template=True):
    """A user-typed name made safe for a file name (and for a yt-dlp output template)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name or "")
    name = re.sub(r"\s+", " ", name).strip().strip(".")[:100]
    return name.replace("%", "%%") if template else name.replace("%", "")


def split_args(text):
    """Split extra yt-dlp arguments typed by the user. Raises ValueError on unbalanced quotes."""
    if not text or not text.strip():
        return []
    parts = shlex.split(text, posix=not WIN)
    return [p[1:-1] if WIN and len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]


def format_args(quality):
    """yt-dlp format options. quality: None (best H.264), a max height like "1080", "audio-mp3" or "audio-m4a"."""
    if quality == "audio-mp3":
        return ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "0"]
    if quality == "audio-m4a":
        return ["-f", "ba[ext=m4a]/ba/b", "-x", "--audio-format", "m4a"]
    h = f"[height<=?{int(quality)}]" if quality and str(quality).isdigit() else ""
    # Prefer H.264 + AAC so the .mp4 plays everywhere (Windows, phones, WhatsApp, editors).
    # H.264 tops out at 1080p on YouTube, so above that take the best codec there is.
    avc = "" if quality and str(quality).isdigit() and int(quality) > 1080 else f"bv*{h}[vcodec^=avc1]+ba[ext=m4a]/"
    fmt = f"{avc}bv*{h}+ba[ext=m4a]/bv*{h}+ba/b{h}/b"
    return ["-f", fmt, "--merge-output-format", "mp4"]


def build_command(url, out_dir, result_file, *, start=None, end=None, fast=False, quality=None,
                  crop=None, crop_pos=0.5, name=None, opts=None, app=False):
    """The yt-dlp command for a clip (start/end given) or a whole video. Writes the final path to result_file.

    opts are the app settings (rate limit, embedding, subtitles, extra arguments).
    app=True adds machine-readable progress lines for the web app.
    """
    opts = opts or {}
    clip = start is not None and end is not None
    audio = str(quality or "").startswith("audio")
    if clip and end <= start:
        raise ValueError("End time must be after start time.")
    crop = crop if crop in CROPS and clip and not audio else None

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if clip:
        label = f"{hms(start, '.')}-{hms(end, '.')}"
        base = safe_name(name) or "%(title).80B"
        template = f"{base} [{label}]{' (' + crop.replace(':', 'x') + ')' if crop else ''}.%(ext)s"
    else:
        template = "%(title).150B.%(ext)s"

    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", *format_args(quality),
           "-P", str(out_dir), "-o", template, "--print-to-file", "after_move:filepath", result_file]
    if app:
        cmd += ["--newline", "--progress-template", PROGRESS_TEMPLATE]

    ff = ["-hide_banner", "-loglevel", "error", "-stats"]
    if clip:
        cmd += ["--download-sections", f"*{start}-{end}"]
        if audio:
            # Without re-encoding, audio can often only be cut at a cue point several seconds early.
            # Audio is cheap to re-encode, so always cut it exactly.
            cmd.append("--force-keyframes-at-cuts")
            ff += ["-b:a", "192k"]
        elif not fast or crop:
            # Re-encodes the clip so it starts/ends exactly on your timestamps (slower for long clips).
            cmd.append("--force-keyframes-at-cuts")
            ff += ["-preset", "veryfast", "-crf", "20"]
            if crop:
                ratio, w, h = CROPS[crop]
                pos = min(max(float(crop_pos), 0.0), 1.0)
                ff += ["-vf", f"crop=ih*{ratio:.5f}:ih:(iw-ow)*{pos:.3f}:0,scale={w}:{h}:flags=lanczos,setsar=1"]
    cmd += ["--downloader-args", "ffmpeg:" + " ".join(ff)]

    if opts.get("rate_limit"):
        cmd += ["-r", f"{int(opts['rate_limit'])}K"]
    if opts.get("embed_thumbnail"):
        cmd.append("--embed-thumbnail")
    if not clip:
        if opts.get("embed_chapters"):
            cmd.append("--embed-chapters")
        if opts.get("subtitles") in ("embed", "srt") and not audio:
            cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", opts.get("sub_langs") or "en"]
            cmd += ["--embed-subs"] if opts["subtitles"] == "embed" else ["--convert-subs", "srt"]
    cmd.append("--no-mtime" if opts.get("mtime_now", True) else "--mtime")

    ffmpeg_dir = find_ffmpeg_dir()
    if ffmpeg_dir:
        cmd += ["--ffmpeg-location", ffmpeg_dir]
    return cmd + split_args(opts.get("custom_args")) + ["--", url]


def download_clip(url, start, end, out_dir, fast=False, max_height=None):
    with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False, encoding="utf-8") as tmp:
        result_file = tmp.name
    try:
        cmd = build_command(url, out_dir, result_file, start=start, end=end, fast=fast,
                            quality=str(max_height) if max_height else None)
    except Exception:
        os.remove(result_file)
        raise

    print(f"\nDownloading {hms(start)} -> {hms(end)} ({hms(end - start)} long)"
          f"{'  [fast mode]' if fast else ''}\n", flush=True)
    try:
        code = subprocess.call(cmd)
        saved = Path(result_file).read_text(encoding="utf-8").strip().splitlines()
    finally:
        os.remove(result_file)

    if code != 0 or not saved:
        raise RuntimeError("Download failed (see the messages above).")
    return Path(saved[-1])


def ask(prompt, parse, hint):
    while True:
        raw = input(prompt).strip().strip('"')
        try:
            return parse(raw)
        except ValueError:
            print(f"  {hint}")


def nonempty(s):
    if not s:
        raise ValueError
    return s


def interactive(args):
    print("=== YouTube clip downloader ===")
    print("Times can be 1:02:30, 62:30, 1h2m30s, or plain seconds.\n")
    url = args.url or ask("YouTube link: ", nonempty, "Paste the video link.")
    time_hint = "Didn't understand that, try again (e.g. 1:02:30 or 1h2m30s)."
    while True:
        start = ask("Start time: ", parse_time, time_hint)
        end = ask("End time:   ", parse_time, time_hint)
        try:
            path = download_clip(url, start, end, args.output, args.fast, args.max_height)
            print(f"\nSaved: {path}")
        except (ValueError, RuntimeError) as e:
            print(f"\n{e}")
        if input("\nAnother clip from the same video? [y/N] ").strip().lower() != "y":
            break
    if sys.platform == "win32" and args.output.exists():
        os.startfile(args.output)


def main():
    p = argparse.ArgumentParser(description="Download a section of a YouTube video or ended livestream.")
    p.add_argument("url", nargs="?", help="YouTube video / livestream link")
    p.add_argument("start", nargs="?", help="clip start, e.g. 1:02:30")
    p.add_argument("end", nargs="?", help="clip end, e.g. 1:05:00")
    p.add_argument("-o", "--output", type=Path, default=DEFAULT_OUT,
                   help=f"folder to save clips in (default: {DEFAULT_OUT})")
    p.add_argument("--fast", action="store_true",
                   help="no re-encoding: much faster, but cuts snap to the nearest keyframe (a few seconds off)")
    p.add_argument("--max-height", type=int, metavar="PX",
                   help="limit video quality, e.g. 720 or 1080")
    args = p.parse_args()

    if not (args.url and args.start and args.end):
        try:
            interactive(args)
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
        return

    try:
        path = download_clip(args.url, parse_time(args.start), parse_time(args.end),
                             args.output, args.fast, args.max_height)
    except ValueError as e:
        sys.exit(f"Bad time: {e}")
    except RuntimeError as e:
        sys.exit(str(e))
    print(f"\nSaved: {path}")


if __name__ == "__main__":
    main()
