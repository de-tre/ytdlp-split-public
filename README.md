# ytdlp-split

Note: This project was developed with substantial assistance from AI-based coding tools.
All architectural decisions, feature definitions, testing, and project direction were provided manually.

A Python-based downloader and segmentation tool built on **yt-dlp**, featuring a clipboard-driven URL collector, optional trimming via timecodes (including fades), chapter-based splitting, and persistent history tracking. This public version contains no personal paths or identifiers.

---

## Core Features

### ⭐ Key Capabilities

* **Timecode trimming** with optional fades (`@0.5`, `@1.2`, `@0`). Fades are always applied *within* the defined range.
* **Splitpoint notation (`sp`)** allows referencing chapter boundaries.
* **Flexible filename suffixes** for time ranges and fades (configurable in `settings.json`).
* **Playlist handling** with optional application of timecodes.
* **Duration lookup**, metadata extraction, and thumbnail retrieval.
* **Persistent history** with timestamp, title, channel, thumbnail, and timecode markers.
* **Clipboard-based URL collector** with background job queue and mode hotkeys.

---

## Download Modes

### Audio

* MP3 extraction with embedded metadata and thumbnails
* Optional chapter splitting
* Removal of original source file unless configured otherwise

### Video

* Full video download (MP4/MKV/WEBM)
* Timecode-based trimming of video segments

### Timecodes

* Supports formats like `M:S`, `H:M:S`, absolute seconds (`5m`, `90`), ranges, open ranges, multiple ranges, and fades. Also `sp` (see above)
* When timecodes are used, only trimming occurs (no chapter splitting).

---

## URL Collector

* Monitors clipboard for new URLs
* Normalizes URLs (e.g., strips unnecessary YouTube parameters)
* De-duplicates by session and history
* Retrieves metadata and duration
* Appends entries to `urls_history.tsv`
* Enqueues jobs processed by a background worker running `python -m ytdlp_split`
* Hotkeys: `s` (split), `v` (video), `t` (timecodes), `p` (playlists), `r` (re-run), `q` (quit)

---

## Supported Platforms & Requirements

* Python 3.10+
* `yt-dlp`
* `ffmpeg`/`ffprobe` (auto-downloaded on Windows)
* Optional: `Send2Trash`, `pyperclip`, browser cookies via `--cookies-from-browser`

---

## Installation

### Virtual Environment (recommended)

```bash
git clone <repo-url>
cd ytdlp-split
python -m venv .venv
.venv/Scripts/activate  # Windows
source .venv/bin/activate  # Linux/macOS
pip install .
```

### Windows Launcher

Double-click:

```
run_url_collector.bat
```

This prepares a virtual environment and downloads ffmpeg if needed.

---

## Command-Line Usage

Show help:

```bash
ytdlp-split --help
```

Examples:

```bash
ytdlp-split --download-only "https://youtu.be/..."
ytdlp-split --download-video "https://youtube.com/watch?v=..."
ytdlp-split --timecodes "0:30-1:00;1:55-2:05@1.2" "https://youtu.be/..."
```

---

## Automatic yt-dlp Fallback

If yt-dlp extraction fails, the tool automatically attempts an update and retries the operation.

---

## Repository Contents

* `ytdlp_split.py` — core downloader and splitting logic
* `ytdlp_url_collector.py` — clipboard collector and job manager
* `run_url_collector.bat` — Windows launcher
* `urls.txt`, `urls_history.tsv` — session and history data
* `ffmpeg-bin/` — Windows-only binaries

---

## Portability

To use the tool on a new machine:

1. Install Python 3.10+.
2. Clone or extract the repository.
3. Run `run_url_collector.bat` (Windows) or `python ytdlp_url_collector.py`.

---

## Legal / Terms of Service Notice

This project is provided for **personal, lawful use only**. Users are responsible for ensuring that their usage complies with the **Terms of Service** of YouTube and all other supported platforms. The maintainers do not encourage or endorse downloading content in violation of applicable laws, platform policies, or copyright restrictions.

---

## Reliability & Expectation Disclaimer

This tool is provided **as-is**, without guarantees of availability, accuracy, or compatibility with future platform changes. Video platforms regularly modify their internal APIs and delivery formats; functionality may break at any time. Users should expect that ongoing maintenance may be required.

---

## License

MIT License
