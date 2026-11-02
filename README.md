# YT Clipper

Cut clips out of YouTube videos, including multi-hour livestreams that have ended, and download only those parts. It also downloads whole videos and playlists, from YouTube and the other sites yt-dlp supports.

- Watch the video in the app and mark the start and end with **I** and **O**, or drag on a timeline with preview thumbnails
- Zoomed strips around the start and end for cuts accurate to a fraction of a second
- Export clips as **9:16** (Reels, TikTok, Shorts) or **1:1**, dragging the frame to where you want it
- Mark several moments, name them, and download them all at once
- Save any frame as a full-quality PNG
- Choose from the resolutions the video actually has (up to 4K), or audio only as MP3 or M4A
- Only the selected part is downloaded, so a short clip from a 3-hour stream takes seconds
- Downloads queue up, with progress, history and each download's yt-dlp output
- One-click updates for yt-dlp, FFmpeg and Deno
- Runs entirely on your computer

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [FFmpeg](https://ffmpeg.org).

## Install

### Windows

1. Download this repository (**Code → Download ZIP**) and unzip it somewhere it can stay.
2. Double-click **setup.bat**. It installs Python if needed, then yt-dlp, Deno and FFmpeg, and adds a **YT Clipper** shortcut to the folder and the Start menu.
3. Open **YT Clipper**. It opens in your browser.

### macOS and Linux

You need Python 3.10 or newer and FFmpeg (`brew install ffmpeg` or `sudo apt install ffmpeg`).

```bash
pip install -r requirements.txt
python3 app.py
```

## Using it

Paste a link. A YouTube link with a timestamp (`&t=...`) starts with a selection at that moment. Paste several links, or a playlist, to queue all of them as full videos.

| Key | Action |
| --- | --- |
| `I` / `O` | Set the start / end to the current video position |
| `P` | Play the clip |
| `A` | Add the current selection to the clip list |
| `F` | Save the current frame as a PNG |
| `Space` or `K` | Play / pause |
| `←` / `→` | Back / forward 5 s (with `Shift`: 1 s) |
| `J` / `L` | Back / forward 10 s |

- **Clip or Full Video:** the switch at the top of the panel on the right.
- **Timeline:** drag to select a range, drag the yellow handles to adjust it, and scroll to zoom. The **Start** and **End** strips underneath show one minute around each cut point for fine adjustments.
- **Nudging:** the arrows next to the times move them by 1 s (`Shift`: 0.1 s, `Alt`: 5 s). `↑` / `↓` work inside the time fields.
- **Frame:** 9:16 and 1:1 put a frame on the video. Drag it into place (double-click to center it). These clips come out at 1080×1920 or 1080×1080.
- **Cut:** **Precise** lands exactly on your timestamps by re-encoding. **Fast** skips re-encoding and is much quicker, but may start a moment early.
- **Clip list:** **Add** saves the current selection. Click a clip to jump to it and adjust it, rename it in place, then **Download All**.
- **Other sites:** they play in a basic player without timeline thumbnails. Some sites don't allow a preview, but downloading still works.

Clips are saved to **Downloads/YT Clips** by default. You can change the folder, and set a speed limit, how many downloads run at once, thumbnail, chapter and subtitle embedding, and extra yt-dlp arguments, in **Settings** (the gear, top right).

The app runs in the background without a window and quits by itself about 10 minutes after you close its tab, unless a download is still running.

## Command line

```bash
python ytclip.py "https://www.youtube.com/watch?v=..." 1:02:30 1:05:10
python ytclip.py "https://youtu.be/..." 2h10m 2h12m30s --fast --max-height 720
```

Run `python ytclip.py` with no arguments to be asked for each value.

## Troubleshooting

- **Downloads suddenly stop working.** YouTube changes often. Open **Settings** and click **Update** to get the latest yt-dlp. A blue dot on the gear means an update is available.
- **Something failed.** Open the downloads list and click the lines icon next to the download to see exactly what yt-dlp said.
- **A stream that just ended fails or has missing parts.** YouTube may still be processing it. Try again in a few hours.
- **A site needs you to be logged in.** Add `--cookies-from-browser firefox` (or `chrome`, `edge`) under **Extra yt-dlp arguments** in Settings.
- **"YT Clipper isn't running."** Open it again from its shortcut.

Only download videos you have the right to download.

## License

[MIT](LICENSE)
