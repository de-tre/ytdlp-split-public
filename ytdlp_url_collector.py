#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ytdlp URL-Collector with:
- Clipboard-based URL collection
- Normalization (e.g., stripping YouTube parameters)
- Persistent history (urls_history.tsv) with timestamp, title, thumbnail URL, timecodes
- History-based duplicate detection
- Optional re-download despite history
- Background job queue for immediate downloads
"""

import time
import re
import os
import sys
import subprocess
import webbrowser
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from datetime import datetime
import json
import threading
from collections import deque
import msvcrt  # available on Windows only
from typing import List, Dict, Tuple, Optional
import atexit

# pyperclip is imported lazily inside main(), once the language is known.
pyperclip = None  # type: ignore

# ---------------------------------------------------------------------------
# Settings & global paths
# ---------------------------------------------------------------------------

# Default base directory for this project (repository root).
DEFAULT_BASE_DIR = Path(r"C:\ytdlp-split")

# Location of the new unified settings file.
SETTINGS_JSON = DEFAULT_BASE_DIR / "settings.json"

# Legacy modes file (will be read once for migration, if present).
LEGACY_MODES_JSON = DEFAULT_BASE_DIR / "collector_modes.json"

# These globals will be initialized in main() after reading settings.
YTDL_DIR: Path
URLS_TXT: Path
HISTORY_TSV: Path

# Global settings object (filled in main()).
SETTINGS: Dict = {}

# Current language (filled in main() via get_language()).
CURRENT_LANG: str = "de"


def current_lang() -> str:
    """
    Returns the currently active language code ('de' or 'en').
    """
    global CURRENT_LANG
    return CURRENT_LANG or "de"


def tr(de: str, en: str) -> str:
    """
    Simple translation helper that returns either the German or
    English variant depending on the current language.
    """
    return de if current_lang() == "de" else en


def info_msg(de: str, en: str) -> None:
    """
    Prints an informational message in the currently selected language.
    """
    print(tr(de, en))


def warn_msg(de: str, en: str) -> None:
    """
    Prints a warning message in the currently selected language.
    """
    print(tr(de, en))


def error_msg(de: str, en: str) -> None:
    """
    Prints an error message in the currently selected language.
    """
    print(tr(de, en))

# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def _load_legacy_modes() -> Dict[str, bool]:
    """
    Loads split/video/timecode/playlist modes from the legacy collector_modes.json
    if it exists. Returns an empty dict if not available or invalid.
    """
    if not LEGACY_MODES_JSON.exists():
        return {}
    try:
        with LEGACY_MODES_JSON.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "split_mode": bool(data.get("split_mode", False)),
            "video_mode": bool(data.get("video_mode", False)),
            "timecode_mode": bool(data.get("timecode_mode", False)),
            "playlist_mode": bool(data.get("playlist_mode", False)),
        }
    except Exception:
        return {}


def _default_settings() -> Dict:
    """
    Returns a default settings structure used when settings.json is missing
    or incomplete.
    """
    base = str(DEFAULT_BASE_DIR)
    # default audio/video/split directories – can be customized in settings.json
    default_audio_dir = r"C:\ytdlp-split"
    default_video_dir = r"C:\ytdlp-split"
    default_split_dir = r"C:\ytdlp-split"

    return {
        "language": "de",
        "paths": {
            "base_dir": base,
            "audio_dl_dir": default_audio_dir,
            "video_dl_dir": default_video_dir,
            "split_dir": default_split_dir,
            "urls_txt": str(DEFAULT_BASE_DIR / "urls.txt"),
            "history_tsv": str(DEFAULT_BASE_DIR / "urls_history.tsv"),
        },
        "modes": {
            "split_mode": False,
            "video_mode": False,
            "timecode_mode": False,
            "playlist_mode": False,
        },
    }


def load_settings() -> Dict:
    """
    Loads settings from settings.json, creating it if necessary.
    If the legacy collector_modes.json exists, its values are migrated
    into the new settings structure once.
    """
    settings = _default_settings()

    # Load existing settings.json if available
    if SETTINGS_JSON.exists():
        try:
            with SETTINGS_JSON.open("r", encoding="utf-8") as f:
                data = json.load(f)
            # Shallow merge: keep defaults for missing sections/keys
            if isinstance(data, dict):
                # language
                if "language" in data:
                    settings["language"] = data["language"]
                # paths
                if isinstance(data.get("paths"), dict):
                    settings["paths"].update(data["paths"])
                # modes
                if isinstance(data.get("modes"), dict):
                    settings["modes"].update(data["modes"])
        except Exception:
            # If settings.json is corrupted, fall back to defaults and recreate later.
            pass
    else:
        # No settings file yet: migrate legacy modes if present.
        legacy_modes = _load_legacy_modes()
        if legacy_modes:
            settings["modes"].update(legacy_modes)
        save_settings(settings)

    # Normalize path entries to absolute paths
    paths = settings.get("paths", {})
    base_dir = Path(paths.get("base_dir", str(DEFAULT_BASE_DIR))).expanduser().resolve()
    paths["base_dir"] = str(base_dir)

    # Derive default urls.txt and history.tsv if not explicitly set
    urls_txt = Path(paths.get("urls_txt", str(base_dir / "urls.txt"))).expanduser().resolve()
    history_tsv = Path(paths.get("history_tsv", str(base_dir / "urls_history.tsv"))).expanduser().resolve()

    paths["urls_txt"] = str(urls_txt)
    paths["history_tsv"] = str(history_tsv)

    settings["paths"] = paths

    # Persist normalized settings
    save_settings(settings)
    return settings


def save_settings(settings: Dict) -> None:
    """
    Writes the given settings dictionary to settings.json.
    """
    try:
        SETTINGS_JSON.parent.mkdir(parents=True, exist_ok=True)
        with SETTINGS_JSON.open("w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
    except Exception as e:
        warn_msg(
            f"[WARN] Konnte settings.json nicht speichern: {e}",
            f"[WARN] Could not save settings.json: {e}",
        )


def get_language(settings: Dict) -> str:
    """
    Returns 'de' or 'en' based on settings['language'].
    Any non-'en' value is treated as 'de'.
    """
    lang = str(settings.get("language", "de")).strip().lower()
    return "en" if lang.startswith("en") else "de"


def load_modes_from_settings(settings: Dict) -> Tuple[bool, bool, bool, bool]:
    """
    Extracts the current mode flags from settings['modes'].
    """
    modes = settings.get("modes", {})
    return (
        bool(modes.get("split_mode", False)),
        bool(modes.get("video_mode", False)),
        bool(modes.get("timecode_mode", False)),
        bool(modes.get("playlist_mode", False)),
    )


def save_modes_to_settings(settings: Dict, split: bool, video: bool, tc: bool, pl: bool) -> None:
    """
    Updates the mode flags in the global settings object and writes settings.json.
    """
    settings.setdefault("modes", {})
    settings["modes"].update(
        {
            "split_mode": bool(split),
            "video_mode": bool(video),
            "timecode_mode": bool(tc),
            "playlist_mode": bool(pl),
        }
    )
    save_settings(settings)


def print_language_hint(lang: str) -> None:
    """
    Prints a short hint at startup explaining how to change the UI language.
    """
    if lang == "en":
        print(f"[INFO] Interface language: English.")
        print(f"       You can change this in '{SETTINGS_JSON.name}' (key: 'language', values: 'de' or 'en').\n")
    else:
        print(f"[INFO] Sprache: Deutsch.")
        print(f"       Dies kann in '{SETTINGS_JSON.name}' über den Schlüssel 'language' geändert werden (Werte: 'de' oder 'en').\n")


# ---------------------------------------------------------------------------
# Job queue for immediate downloads
# ---------------------------------------------------------------------------

job_queue: List[dict] = []           # FIFO list of jobs
job_lock = threading.Lock()          # protects job_queue
worker_should_stop = False           # flag for clean shutdown
worker_thread: Optional[threading.Thread] = None

split_mode: bool = False   # True = chapter splitting enabled
video_mode: bool = False   # True = download video instead of audio
timecode_mode: bool = False  # True = prompt for timecodes
playlist_mode: bool = False   # True = allow playlists (--playlists)

last_job_url: Optional[str] = None

# ----------------- YouTube-ID validation -----------------

YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def is_supported_url(url: str) -> bool:
    """
    Allows only hosts that are actually handled by the downloader.
    Other URLs are ignored and never enter history/queue.
    """
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False

    host = (parsed.netloc or "").lower()
    if not host:
        return False

    # YouTube
    if "youtube.com" in host or "youtu.be" in host:
        return True

    # SoundCloud
    if "soundcloud.com" in host or "sndcdn.com" in host:
        return True

    # Twitter / X
    if "twitter.com" in host or host.endswith("x.com"):
        return True

    return False


def _is_valid_yt_id(video_id: str) -> bool:
    """Simple validation whether a string looks like a YouTube video ID."""
    return bool(YT_ID_RE.match(video_id))


# ----------------- URL helpers -----------------


def extract_urls(text: str) -> List[str]:
    """Returns all http/https URLs in the given text."""
    pattern = r"https?://\S+"
    return re.findall(pattern, text)


def sanitize_url_for_video(u: str) -> str:
    """
    Removes all URL parameters and fragments.

    Goal: point only to the concrete video.

    - YouTube watch URLs: only ?v=ID is kept (all other parameters removed)
    - youtu.be: query + fragment removed, only valid 11-char video IDs accepted
    - youtube.com/shorts/...: only the path is kept if the last component
      looks like a video ID
    - other domains: query + fragment are removed
    """
    u = u.strip()
    if not u:
        return ""

    try:
        parsed = urlparse(u)
    except Exception:
        return ""

    if parsed.scheme not in ("http", "https"):
        return ""

    host = parsed.netloc.lower()

    # youtu.be short URLs
    if "youtu.be" in host:
        vid = parsed.path.lstrip("/")
        if not _is_valid_yt_id(vid):
            # Placeholder or invalid link -> discard
            return ""
        return urlunparse((parsed.scheme, parsed.netloc, f"/{vid}", "", "", ""))

    # Full YouTube URLs
    if "youtube.com" in host or "music.youtube.com" in host:
        # /watch?v=ID&list=...
        if parsed.path == "/watch":
            qs = dict(parse_qsl(parsed.query, keep_blank_values=False))
            vid = qs.get("v")
            if isinstance(vid, list):
                vid = vid[0]

            if not vid or not _is_valid_yt_id(vid):
                return ""

            new_query = urlencode({"v": vid})
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", new_query, ""))

        # /shorts/ID or other paths
        parts = [p for p in parsed.path.split("/") if p]
        if parts:
            last = parts[-1]
            if _is_valid_yt_id(last):
                # ID in path -> remove query/fragment
                return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
            else:
                # e.g. /@ChannelName/... without a concrete video ID
                return ""

    # Generic domains: drop query + fragment
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def read_urls_file() -> List[str]:
    """Reads urls.txt, normalizes URLs and removes duplicates."""
    urls: List[str] = []
    if not URLS_TXT.exists():
        return urls

    with URLS_TXT.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            norm = sanitize_url_for_video(s)
            if norm and norm not in urls:
                urls.append(norm)
    return urls


def write_urls_file(urls: List[str]) -> None:
    """
    Writes a clean, de-duplicated list of URLs to urls.txt.
    Preserves order while removing duplicates.
    """
    seen = set()
    cleaned: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            cleaned.append(u)

    with URLS_TXT.open("w", encoding="utf-8") as f:
        for u in cleaned:
            f.write(u + "\n")


def append_url(url: str) -> None:
    """Appends a URL to urls.txt for the current session."""
    with URLS_TXT.open("a", encoding="utf-8") as f:
        f.write(url + "\n")


def print_modes(prefix: str = "[MODES]") -> None:
    """
    Prints the current mode state to the console in a localized form.
    """
    global split_mode, video_mode, timecode_mode, playlist_mode
    if current_lang() == "de":
        on, off = "AN", "AUS"
    else:
        on, off = "ON", "OFF"

    s = on if split_mode else off
    v = on if video_mode else off
    t = on if timecode_mode else off
    p = on if playlist_mode else off

    print(f"{prefix} Split: {s} | Video: {v} | Timecodes: {t} | Playlists: {p}")


Job = Tuple[str, bool, bool, Optional[str]]  # (url, split_snapshot, video_snapshot, timecodes)
job_queue: deque[Job]
job_lock = threading.Lock()
stop_event = threading.Event()

# ----------------- Timecode prompting -----------------


def prompt_timecodes_for_url(url: str, title: Optional[str], channel: Optional[str]) -> Optional[str]:
    """
    Prompts for a timecode specification for a given URL if timecode mode is enabled.
    """
    if not timecode_mode:
        return None

    info_msg(
        "\n[TC] Timecodes für diese URL eingeben.",
        "\n[TC] Enter timecodes for this URL.",
    )
    if title or channel:
        info_msg(
            f"[TC] Titel: {title or '-'}  |  Kanal: {channel or '-'}",
            f"[TC] Title: {title or '-'}  |  Channel: {channel or '-'}",
        )

    # Examples / guidance
    info_msg(
        "[TC] Beispiele:",
        "[TC] Examples:",
    )
    info_msg(
        "     90          -> 90 Sekunden",
        "     90          -> 90 seconds",
    )
    info_msg(
        "     1:30        -> 1 Minute 30 Sekunden",
        "     1:30        -> 1 minute 30 seconds",
    )
    info_msg(
        "     01:02:03    -> 1 Stunde, 2 Minuten, 3 Sekunden",
        "     01:02:03    -> 1 hour, 2 minutes, 3 seconds",
    )
    info_msg(
        "     90s, 5m, 1h -> reine Sekunden/Minuten/Stunden",
        "     90s, 5m, 1h -> plain seconds/minutes/hours",
    )
    info_msg(
        "     1:00-2:30   -> Range von 1:00 bis 2:30",
        "     1:00-2:30   -> range from 1:00 to 2:30",
    )
    info_msg(
        "     -2:00       -> vom Anfang bis 2:00",
        "     -2:00       -> from start up to 2:00",
    )
    info_msg(
        "     1:00-       -> von 1:00 bis zum Ende",
        "     1:00-       -> from 1:00 to the end",
    )
    info_msg(
        "     0:30-1:00;2:00-3:00 -> mehrere Ranges, getrennt durch ';'",
        "     0:30-1:00;2:00-3:00 -> multiple ranges separated by ';'",
    )
    info_msg(
        "[TC] Leer lassen für komplette Datei.",
        "[TC] Leave empty to use the full file.",
    )

    prompt = tr("[TC] Timecodes: ", "[TC] Timecodes: ")
    spec = input(prompt).strip()
    return spec or None


def enqueue_job(
    url: str,
    split_mode: bool,
    video_mode: bool,
    playlist_mode: bool,
    timecodes: Optional[str],
) -> None:
    """
    Enqueues a new download job.

    timecodes is either a string (e.g. "0:30-1:00;1:55-2:05") or None.
    """
    job = {
        "url": url,
        "split": split_mode,
        "video": video_mode,
        "playlist": playlist_mode,
        "timecodes": timecodes,
    }

    with job_lock:
        job_queue.append(job)

    print(
        "[JOB] Eingereiht:"
        f" URL={url}"
        f" | Split={'AN' if split_mode else 'AUS'}"
        f" | Video={'AN' if video_mode else 'AUS'}"
        f" | Playlists={'AN' if playlist_mode else 'AUS'}"
        f" | Timecodes={timecodes or '–'}"
    )


def run_ytdl_job(job: Dict[str, object]) -> None:
    url = str(job["url"])
    split = bool(job["split"])
    video = bool(job["video"])
    playlist = bool(job.get("playlist", False))
    timecodes = job.get("timecodes")

    cmd = [sys.executable, "-m", "ytdlp_split"]

    if video:
        cmd.append("--download-video")
    elif not split:
        cmd.append("--download-only")

    if playlist:
        cmd.append("--playlists")

    if timecodes:
        cmd += ["--timecodes", str(timecodes)]

    cmd.append(url)

    info_msg(
        "\n[JOB] Starte:", " ".join(cmd),
        "\n[JOB] Start:", " ".join(cmd),
    )
    print("------------------------------------------------------------")
    try:
        proc = subprocess.run(cmd, check=False)
        rc = proc.returncode
    except OSError as e:
        error_msg(
            f"[ERROR] Konnte ytdlp-split nicht starten: {e}",
            f"[ERROR] Couldn't start ytdlp-split: {e}",
        )
        rc = -1

    print("------------------------------------------------------------")
    info_msg(
        f"[JOB] Beendet (Returncode: {rc})",
        f"[JOB] Ended (return code: {rc})",
    )


def worker_loop() -> None:
    """
    Background worker that processes jobs from job_queue (FIFO).
    """
    global worker_should_stop

    while not worker_should_stop:
        job = None

        # Retrieve next job in a thread-safe way
        with job_lock:
            if job_queue:
                job = job_queue.pop(0)

        if job is None:
            time.sleep(0.5)
            continue

        try:
            run_ytdl_job(job)
        except Exception as e:
            error_msg(
                f"[WORKER-ERROR] Fehler bei Job {job.get('url')}: {e}",
                f"[WORKER-ERROR] Error at job {job.get('url')}: {e}",
            )


def _shutdown_worker() -> None:
    """
    Called on interpreter shutdown (atexit).
    Attempts to stop the worker thread cleanly.
    """
    global worker_should_stop, worker_thread

    worker_should_stop = True
    if worker_thread is not None and worker_thread.is_alive():
        try:
            worker_thread.join(timeout=2.0)
        except RuntimeError:
            # Thread was never started or already finished
            pass


atexit.register(_shutdown_worker)


# ----------------- History handling -----------------


def load_history() -> Tuple[List[Dict], Dict[str, List[Dict]]]:
    """
    Reads urls_history.tsv.

    Column schema (new version):
        ts \t url \t title \t channel \t timecodes \t thumb

    Older lines with 5 columns (without 'timecodes') are still accepted.
    """
    entries: List[Dict] = []
    by_url: Dict[str, List[Dict]] = {}

    if not HISTORY_TSV.exists():
        return entries, by_url

    with HISTORY_TSV.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")

            # Old version: 5 columns (no timecodes)
            if len(parts) == 5:
                ts, url, title, channel, thumb = parts
                timecodes = ""
            # New version: >= 6 columns (with timecodes)
            elif len(parts) >= 6:
                ts, url, title, channel, timecodes, thumb = (
                    parts[0],
                    parts[1],
                    parts[2],
                    parts[3],
                    parts[4],
                    parts[5],
                )
            else:
                # malformed line -> skip
                continue

            entry = {
                "ts": ts,
                "url": url,
                "title": title,
                "channel": channel,
                "timecodes": timecodes,
                "thumb": thumb,
            }
            entries.append(entry)
            by_url.setdefault(url, []).append(entry)

    return entries, by_url


def append_history(
    url: str,
    title: Optional[str],
    channel: Optional[str],
    thumb: Optional[str],
    timecodes: Optional[str] = None,
) -> Dict:
    """
    Appends a new entry to urls_history.tsv.

    Schema:
        ts \t url \t title \t channel \t timecodes \t thumb

    Newest entries should appear at the top (reverse chronological order).
    """
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    title = title or ""
    channel = channel or ""
    thumb = thumb or ""
    timecodes = (timecodes or "").strip()

    entry = {
        "ts": ts,
        "url": url,
        "title": title,
        "channel": channel,
        "timecodes": timecodes,
        "thumb": thumb,
    }

    line = "\t".join([ts, url, title, channel, timecodes, thumb]) + "\n"

    HISTORY_TSV.parent.mkdir(parents=True, exist_ok=True)

    # New entries should appear at the top
    old_content = ""
    if HISTORY_TSV.exists():
        with HISTORY_TSV.open("r", encoding="utf-8") as f:
            old_content = f.read()

    with HISTORY_TSV.open("w", encoding="utf-8", newline="") as f:
        f.write(line)
        if old_content:
            f.write(old_content)

    return entry


def show_history_for_url(url: str, history_by_url: Dict[str, List[Dict]]) -> None:
    """
    Prints all existing history entries for a given URL.
    Entries with timecodes are marked with [TC].
    """
    entries = history_by_url.get(url, [])
    if not entries:
        info_msg(
            f"[HISTORY] Keine Einträge für URL: {url}",
            f"[HISTORY] No entries for URL: {url}",
        )
        return

    info_msg(
        f"[HISTORY] {len(entries)} frühere Eintrag(e) für {url}:",
        f"[HISTORY] {len(entries)} previous entry/entries for {url}:",
    )
    for e in entries:
        ts = e.get("ts", "?")
        title = e.get("title", "")
        channel = e.get("channel", "")
        tc = (e.get("timecodes") or "").strip()
        tc_flag = " [TC]" if tc else ""
        print(f"  - {ts}{tc_flag} | {title} — {channel}")


def maybe_open_thumbnail(entry: Dict) -> None:
    """
    Optionally opens the thumbnail URL in a browser so the user
    can see the image directly.
    """
    thumb = entry.get("thumb")
    if not thumb:
        return
    if not ask_yes_no(
        "Thumbnail im Browser anzeigen?",
        "Open thumbnail in browser?",
        default=False,
    ):
        return
    try:
        webbrowser.open(thumb)
    except Exception as e:
        warn_msg(
            f"[WARN] Konnte Thumbnail nicht öffnen: {e}",
            f"[WARN] Couldn't open thumbnail: {e}",
        )


# ----------------- Video info via yt_dlp -----------------


def get_video_info(url: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[float]]:
    """
    Fetches basic information for a URL via yt_dlp:
    - title
    - uploader (channel)
    - thumbnail
    - duration (seconds, if available)
    """
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "yt_dlp",
                "--skip-download",
                "--no-warnings",
                "--no-playlist",
                "--print",
                "%(title)s\t%(uploader)s\t%(thumbnail)s\t%(duration)s",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
    except Exception as e:
        warn_msg(
            f"[WARN] Konnte yt_dlp für Video-Info nicht ausführen: {e}",
            f"[WARN] Couldn't exec yt_dlp for video info: {e}",
        )
        return None, None, None, None

    if proc.returncode != 0:
        return None, None, None, None

    try:
        line = proc.stdout.strip().splitlines()[-1].strip()
    except IndexError:
        return None, None, None, None

    parts = line.split("\t")
    title = parts[0].strip() if len(parts) > 0 and parts[0] else None
    channel = parts[1].strip() if len(parts) > 1 and parts[1] else None
    thumb = parts[2].strip() if len(parts) > 2 and parts[2] else None

    duration_sec: Optional[float] = None
    if len(parts) > 3 and parts[3]:
        try:
            duration_sec = float(parts[3])
        except ValueError:
            duration_sec = None

    return title or None, channel or None, thumb or None, duration_sec


def format_duration(seconds: Optional[float]) -> Optional[str]:
    """
    Formats seconds as mm:ss or h:mm:ss for console display.
    """
    if seconds is None or seconds <= 0:
        return None
    total = int(round(seconds))
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


# ----------------- Interaction & ytdlp-split invocation -----------------


def ask_yes_no(de_prompt: str, en_prompt: str, default: bool = False) -> bool:
    """
    Simple localized yes/no prompt.

    For German:
      default=False -> [j/N]
      default=True  -> [J/n]

    For English:
      default=False -> [y/N]
      default=True  -> [Y/n]
    """
    lang = current_lang()
    prompt = de_prompt if lang == "de" else en_prompt

    if lang == "de":
        suffix = " [J/n] " if default else " [j/N] "
    else:
        suffix = " [Y/n] " if default else " [y/N] "

    while True:
        try:
            ans = input(prompt + suffix).strip().lower()
        except EOFError:
            return default

        if not ans:
            return default

        if ans in ("j", "ja", "y", "yes"):
            return True
        if ans in ("n", "nein", "no"):
            return False

        if lang == "de":
            print("Bitte j oder n eingeben.")
        else:
            print("Please enter y or n.")


def run_ytdl(download_only: bool) -> None:
    """
    Starts ytdlp-split with urls.txt using the currently running Python interpreter.
    """
    cmd = [sys.executable, "-m", "ytdlp_split"]
    if download_only:
        cmd.append("--download-only")
    cmd += ["--urls", str(URLS_TXT)]

    info_msg(
        "\n[INFO] Starte Download mit Befehl:",
        "\n[INFO] Start download with command:",
        )
    print("       " + " ".join(cmd))
    print("------------------------------------------------------------")

    rc = None
    try:
        proc = subprocess.run(cmd, check=False)
        rc = proc.returncode
    except OSError as e:
        error_msg(
            f"[ERROR] ytdlp-split konnte nicht gestartet werden: {e}",
            f"[ERROR] ytdlp-split couldn't be started: {e}",
            )
    print("------------------------------------------------------------")
    info_msg(
        f"[INFO] ytdlp-split wurde beendet. Rückgabecode: {rc}",
        f"[INFO] ytdlp-split was ended. Return code: {rc}",
        )


# ----------------- Main logic -----------------


def main() -> None:
    global YTDL_DIR, URLS_TXT, HISTORY_TSV
    global split_mode, video_mode, timecode_mode, playlist_mode
    global worker_should_stop, worker_thread
    global SETTINGS, CURRENT_LANG, pyperclip

    # Load settings (including language, paths, modes)
    SETTINGS = load_settings()
    CURRENT_LANG = get_language(SETTINGS)

    paths_cfg = SETTINGS.get("paths", {})
    YTDL_DIR = Path(paths_cfg.get("base_dir", str(DEFAULT_BASE_DIR))).expanduser().resolve()
    URLS_TXT = Path(paths_cfg.get("urls_txt", str(YTDL_DIR / "urls.txt"))).expanduser().resolve()
    HISTORY_TSV = Path(paths_cfg.get("history_tsv", str(YTDL_DIR / "urls_history.tsv"))).expanduser().resolve()

    # Ensure directories/files exist
    YTDL_DIR.mkdir(parents=True, exist_ok=True)
    URLS_TXT.touch(exist_ok=True)
    HISTORY_TSV.touch(exist_ok=True)

    # Always work in the repository/base directory
    os.chdir(YTDL_DIR)

    # Startup header
    info_msg(
        "=== ytdlp URL-Collector (mit History & Batch-Run) ===\n",
        "=== ytdlp URL Collector (with history & batch run) ===\n",
    )
    info_msg(
        f"Arbeitsordner: {YTDL_DIR}",
        f"Working directory: {YTDL_DIR}",
    )
    info_msg(
        f"URL-Datei    : {URLS_TXT}",
        f"URL file     : {URLS_TXT}",
    )
    info_msg(
        f"History-Datei: {HISTORY_TSV}\n",
        f"History file : {HISTORY_TSV}\n",
    )

    # Language hint (de/en)
    print_language_hint(CURRENT_LANG)

    # Try to import pyperclip with localized error message
    try:
        import pyperclip as _pyperclip  # type: ignore
        pyperclip = _pyperclip
    except Exception:
        error_msg(
            "[ERROR] Modul 'pyperclip' ist nicht installiert. Bitte mit 'pip install pyperclip' nachinstallieren.",
            "[ERROR] Module 'pyperclip' is not installed. Please install it via 'pip install pyperclip'.",
        )
        sys.exit(1)

    # Load modes from settings
    split_mode, video_mode, timecode_mode, playlist_mode = load_modes_from_settings(SETTINGS)
    print_modes("[MODES-INIT]")

    # Load history
    history_entries, history_by_url = load_history()
    info_msg(
        f"[INFO] History: {len(history_entries)} Einträge, {len(history_by_url)} unterschiedliche URLs.\n",
        f"[INFO] History: {len(history_entries)} entries, {len(history_by_url)} distinct URLs.\n",
    )

    # Use existing URLs in urls.txt as initial session state
    existing_urls = read_urls_file()
    if existing_urls:
        info_msg(
            f"[INFO] urls.txt enthält bereits {len(existing_urls)} URL(s).",
            f"[INFO] urls.txt already contains {len(existing_urls)} URL(s).",
        )
        info_msg(
            "      Diese werden in dieser Session mit berücksichtigt.\n",
            "      They will be considered in this session.\n",
        )
    else:
        info_msg(
            "[INFO] urls.txt ist aktuell leer.\n",
            "[INFO] urls.txt is currently empty.\n",
        )

    # Session set: URLs scheduled for this session
    session_urls = set(existing_urls)

    def process_single_url(url: str) -> None:
        nonlocal history_entries, history_by_url, session_urls
        global last_job_url

        # Restrict to supported hosts before any history/queue processing
        if not is_supported_url(url):
            info_msg(
                f"[SKIP] Host nicht unterstützt, URL wird nicht in History/Queue aufgenommen: {url}",
                f"[SKIP] Host isn't supported, URL will not be appended to history/queue: {url}",
            )
            return

        last_job_url = url

        # 1) Already in this session?
        if url in session_urls:
            if timecode_mode:
                if not ask_yes_no(
                    "Diese URL ist in dieser Session bereits vorhanden. Trotzdem erneut verarbeiten (z.B. mit anderen Timecodes)?",
                    "This URL is already present in this session. Process again (e.g. with different timecodes)?",
                    default=True,
                ):
                    info_msg(
                        f"[SKIP] Session-Duplikat nicht erneut verarbeitet: {url}",
                        f"[SKIP] Session duplicate not processed again: {url}",
                    )
                    return
            else:
                info_msg(
                    f"[SKIP] Bereits in dieser Session: {url}",
                    f"[SKIP] Already present in this session: {url}",
                )
                return

        # 2) Already in history?
        if url in history_by_url:
            entries_for_url = history_by_url[url]

            show_history_for_url(url, history_by_url)
            last_entry = entries_for_url[-1]
            maybe_open_thumbnail(last_entry)

            has_tc = any((e.get("timecodes") or "").strip() for e in entries_for_url)
            if has_tc:
                info_msg(
                    "[HINWEIS] Diese URL wurde bereits mindestens einmal mit Timecodes bearbeitet.",
                    "[INFO] This URL has been processed at least once with time codes.",
                )

            default_answer = True if has_tc else False

            if not ask_yes_no(
                "Diese URL war bereits in der History. Trotzdem in diese Session (Download-Liste) aufnehmen?",
                "This URL already exists in history. Add it to the current session (download list) anyway?",
                default=default_answer,
            ):
                info_msg(
                    f"[SKIP] History-Duplikat nicht erneut aufgenommen: {url}",
                    f"[SKIP] History duplicate not added again: {url}",
                )
                return

            title = last_entry.get("title")
            channel = last_entry.get("channel")
            thumb = last_entry.get("thumb")

            # Refresh duration via yt_dlp for display
            _, _, _, duration = get_video_info(url)
            dur_str = format_duration(duration)

            session_urls.add(url)
            append_url(url)

            if title and channel:
                if dur_str:
                    info_msg(
                        f"[ADD] {url}  |  {title} — {channel} | {dur_str}  (erneuter Download)",
                        f"[ADD] {url}  |  {title} — {channel} | {dur_str}  (anew download)",
                    )
                else:
                    info_msg(
                        f"[ADD] {url}  |  {title} — {channel}  (erneuter Download)",
                        f"[ADD] {url}  |  {title} — {channel}  (anew download)",
                    )
            elif title:
                if dur_str:
                    info_msg(
                        f"[ADD] {url}  |  {title} | {dur_str}  (erneuter Download)",
                        f"[ADD] {url}  |  {title} | {dur_str}  (anew download)",
                    )
                else:
                    info_msg(
                        f"[ADD] {url}  |  {title}  (erneuter Download)",
                        f"[ADD] {url}  |  {title}  (anew download)",
                    )
            else:
                if dur_str:
                    info_msg(
                        f"[ADD] {url}  |  {dur_str}  (erneuter Download)",
                        f"[ADD] {url}  |  {dur_str}  (anew download)",
                    )
                else:
                    info_msg(
                        f"[ADD] {url}  (erneuter Download)",
                        f"[ADD] {url}  (anew download)",
                    )

            # Timecode / playlist logic
            tc_spec = None

            if timecode_mode:
                if playlist_mode:
                    if ask_yes_no(
                        "Timecodes auf die gesamte Playlist anwenden? (Standard: Nein)",
                        "Apply timecodes to the entire playlist? (default: No)",
                        default=False,
                    ):
                        tc_spec = prompt_timecodes_for_url(url, title, channel)
                    else:
                        info_msg(
                            "[TC] Timecodes werden für diese Playlist-URL ignoriert.",
                            "[TC] Timecodes are ignored for this playlist URL.",
                        )
                else:
                    tc_spec = prompt_timecodes_for_url(url, title, channel)

            # Update history (including timecodes)
            new_entry = append_history(url, title, channel, thumb, tc_spec)
            history_entries.append(new_entry)
            history_by_url.setdefault(url, []).append(new_entry)

            # Enqueue job
            enqueue_job(url, split_mode, video_mode, playlist_mode, tc_spec)
            return

        # 3) New URL (not in history and not in session)
        title, channel, thumb, duration = get_video_info(url)
        session_urls.add(url)
        append_url(url)

        dur_str = format_duration(duration)

        if title and channel:
            if dur_str:
                print(f"[ADD] {url}  |  {title} — {channel} | {dur_str}")
            else:
                print(f"[ADD] {url}  |  {title} — {channel}")
        elif title:
            if dur_str:
                print(f"[ADD] {url}  |  {title} | {dur_str}")
            else:
                print(f"[ADD] {url}  |  {title}")
        else:
            if dur_str:
                print(f"[ADD] {url}  |  {dur_str}")
            else:
                print(f"[ADD] {url}")

        if thumb:
            info_msg(
                f"      Thumbnail-URL: {thumb}",
                f"      Thumbnail URL: {thumb}",
            )

        # Timecode / playlist logic
        tc_spec = None

        if timecode_mode:
            if playlist_mode:
                if ask_yes_no(
                    "Timecodes auf die gesamte Playlist anwenden? (Standard: Nein)",
                    "Apply timecodes to the entire playlist? (default: No)",
                    default=False,
                ):
                    tc_spec = prompt_timecodes_for_url(url, title, channel)
                else:
                    info_msg(
                        "[TC] Timecodes werden für diese Playlist-URL ignoriert.",
                        "[TC] Timecodes are ignored for this playlist URL.",
                    )
            else:
                tc_spec = prompt_timecodes_for_url(url, title, channel)

        # Add to history (including timecodes)
        entry = append_history(url, title, channel, thumb, tc_spec)
        history_entries.append(entry)
        history_by_url.setdefault(url, []).append(entry)

        # Enqueue job
        enqueue_job(url, split_mode, video_mode, playlist_mode, tc_spec)

    info_msg("Anleitung:", "Instructions:")
    info_msg(
        "  - Kopiere YouTube-URLs (oder andere HTTP-Links) in die Zwischenablage.",
        "  - Copy YouTube URLs (or other HTTP links) to the clipboard.",
    )
    info_msg(
        "  - Jede neue URL wird automatisch in urls.txt eingetragen (Session),",
        "  - Each new URL is automatically appended to urls.txt (session).",
    )
    info_msg(
        "    außer sie war bereits in der History und du lehnst sie ab.",
        "    unless it is already in history and you skip it.",
    )
    info_msg(
        "  - STRG+C drücken, wenn die Sammlung beendet werden soll – danach kann die Liste abgearbeitet werden.\n",
        "  - Press CTRL+C when collection is finished – the list can be processed afterwards.\n",
    )

    # Start worker thread for immediate downloads
    worker_thread = threading.Thread(target=worker_loop, daemon=False)
    worker_thread.start()

    last_clip = ""
    try:
        while True:
            try:
                while msvcrt.kbhit():
                    ch = msvcrt.getch()
                    if not ch:
                        continue
                    try:
                        c = ch.decode("utf-8").lower()
                    except Exception:
                        continue

                    if c == "s":
                        split_mode = not split_mode
                        save_modes_to_settings(SETTINGS, split_mode, video_mode, timecode_mode, playlist_mode)
                        print_modes("[MODES-TOGGLE]")
                    elif c == "v":
                        video_mode = not video_mode
                        save_modes_to_settings(SETTINGS, split_mode, video_mode, timecode_mode, playlist_mode)
                        print_modes("[MODES-TOGGLE]")
                    elif c == "t":
                        timecode_mode = not timecode_mode
                        save_modes_to_settings(SETTINGS, split_mode, video_mode, timecode_mode, playlist_mode)
                        print_modes("[MODES-TOGGLE]")
                    elif c == "p":
                        playlist_mode = not playlist_mode
                        save_modes_to_settings(SETTINGS, split_mode, video_mode, timecode_mode, playlist_mode)
                        print_modes("[MODES-TOGGLE]")
                    elif c == "r":
                        # Re-run last job URL (if available)
                        if last_job_url:
                            info_msg(
                                f"[RE-RUN] Letzte URL erneut verarbeiten: {last_job_url}",
                                f"[RE-RUN] Process last URL again: {last_job_url}",
                            )
                            process_single_url(last_job_url)
                        else:
                            info_msg(
                                "[RE-RUN] Keine letzte URL bekannt.",
                                "[RE-RUN] No last URL known.",
                            )
            except Exception as e:
                warn_msg(
                    f"[WARN] Fehler bei Hotkey-Handling: {e}",
                    f"[WARN] Error in hotkey handling: {e}",
                )
            try:
                clip = pyperclip.paste().strip()
            except Exception as e:
                warn_msg(
                    f"[WARN] Konnte Zwischenablage nicht lesen: {e}",
                    f"[WARN] Couldn't read clipboard: {e}",
                )
                time.sleep(0.8)
                continue

            if clip and clip != last_clip:
                urls_in_clip = extract_urls(clip)
                if urls_in_clip:
                    for raw in urls_in_clip:
                        url = sanitize_url_for_video(raw)
                        if not url:
                            continue
                        process_single_url(url)

                last_clip = clip

            time.sleep(0.5)
    except KeyboardInterrupt:
        info_msg(
            "\n[INFO] Erfassung beendet (STRG+C).",
            "\n[INFO] Collection stopped (CTRL+C).",
        )

        worker_should_stop = True
        if worker_thread is not None and worker_thread.is_alive():
            try:
                worker_thread.join(timeout=5.0)
            except RuntimeError:
                pass
        info_msg(
            "[INFO] Collector beendet.",
            "[INFO] Collector finished.",
        )

        try:
            input(tr("\n[INFO] Drücke Enter, um das Fenster zu schließen...", "\n[INFO] Press Enter to close this window..."))
        except EOFError:
            pass


if __name__ == "__main__":
    main()