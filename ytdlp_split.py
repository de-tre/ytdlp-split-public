#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YouTube downloader and chapter splitter.

Main capabilities:
1) Accepts a list of YouTube URLs or playlists (from a text file or CLI) and
   downloads audio as MP3 (192 kb/s, libmp3lame) via yt-dlp.
   - Embeds thumbnail as album art.
   - Embeds basic metadata.
   - Writes uploader/channel name as artist/album-artist tags.

2) Detects chapters in the source file via ffprobe and splits each source
   into individual files.
   - Per-source numbering starts at 1 (01, 02, ...).
   - Output filename is derived from the chapter title.
   - Album tag is set to the source filename (without extension).
   - Embedded album art from the source file is preserved during rendering.

3) Optional cleanup step: move source files to the recycle bin:
   - per source (--trash-confirm / --trash-after), or
   - once at the end of the run (--trash-confirm-batch / --trash-after-batch).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
import urllib.request
import zipfile
import time

# Repository root directory (directory containing ytdlp-split.py)
ROOT_DIR = Path(__file__).resolve().parent

# Shared settings file (also used by the URL collector)
SETTINGS_JSON = ROOT_DIR / "settings.json"
SETTINGS: Optional[Dict] = None


def load_settings() -> Dict:
    """
    Loads settings from settings.json, if present.
    Uses sensible defaults otherwise.
    Expected structure (relevant to this script):

    {
      "language": "de" | "en",
      "paths": {
        "audio_dl_dir": "...",
        "video_dl_dir": "...",
        "split_dir": "..."
      },
      "timecode_filename": {
        "include_range": true,
        "include_fade": true
      }
      ...
    }
    """
    # Defaults (are overwritten by settings.json, if it exists)
    settings: Dict = {
        "language": "de",
        "paths": {
            "audio_dl_dir": os.path.normpath(r"C:\ytdlp-split"),
            "video_dl_dir": os.path.normpath(r"C:\ytdlp-split"),
            "split_dir": os.path.normpath(r"C:\ytdlp-split"),
        },
        "timecode_filename": {
            "include_range": True,
            "include_fade": True,
        },
    }

    if SETTINGS_JSON.exists():
        try:
            with SETTINGS_JSON.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # Apply language
                if "language" in data:
                    settings["language"] = data["language"]

                # Apply/add paths
                if isinstance(data.get("paths"), dict):
                    settings["paths"].update(data["paths"])

                # Apply/add timecode-filename-options
                if isinstance(data.get("timecode_filename"), dict):
                    settings["timecode_filename"].update(data["timecode_filename"])
        except Exception:
            # Continue with defaults if error
            pass

    return settings


def get_language(settings: Dict) -> str:
    """
    Return normalized language code 'de' or 'en' based on settings['language'].
    Any value not clearly starting with 'en' is treated as 'de'.
    """
    lang = str(settings.get("language", "de")).strip().lower()
    return "en" if lang.startswith("en") else "de"


# Current runtime language; will be set in main() based on settings.json
LANG = "de"


def tr(de: str, en: str) -> str:
    """Chooses the appropriate strings depending on the language setting."""
    return en if LANG == "en" else de


def info_msg(de: str, en: str) -> None:
    print(tr(de, en))


def warn_msg(de: str, en: str) -> None:
    print(tr(de, en))


def error_msg(de: str, en: str) -> None:
    print(tr(de, en))


def ok_msg(de: str, en: str) -> None:
    print(tr(de, en))


def print_language_hint(lang: str) -> None:
    """
    Print a short hint at startup that the interface language can be changed
    in settings.json.
    """
    if lang == "en":
        print("[INFO] Interface language: English.")
        print(
            f"       You can change this in '{SETTINGS_JSON.name}' "
            "(key: 'language', values: 'de' or 'en').\n"
        )
    else:
        print("[INFO] Sprache: Deutsch.")
        print(
            f"       Dies kann in '{SETTINGS_JSON.name}' über den Schlüssel "
            "'language' geändert werden (Werte: 'de' oder 'en').\n"
        )


# Directory into which ffmpeg/ffprobe binaries are downloaded/extracted (Windows)
FFMPEG_DIR = ROOT_DIR / "ffmpeg-bin"
FFMPEG_DIR.mkdir(exist_ok=True)

# Platform detection
IS_WINDOWS = os.name == "nt"

# Paths to ffmpeg/ffprobe
if IS_WINDOWS:
    FFMPEG_EXE = FFMPEG_DIR / "ffmpeg.exe"
    FFPROBE_EXE = FFMPEG_DIR / "ffprobe.exe"
else:
    # On Linux/macOS use system ffmpeg from PATH
    FFMPEG_EXE = "ffmpeg"
    FFPROBE_EXE = "ffprobe"

# Default fade duration (in seconds) for timecode cuts
DEFAULT_FADE_SECONDS = 0.5

try:
    from send2trash import send2trash  # type: ignore
except Exception:
    # Fallback if send2trash is not available; checked later where needed
    send2trash = None


# ------------------------- Helper functions -------------------------


def run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    """
    Subprocess wrapper: always decodes as UTF-8 and avoids cp1252 decoding issues.
    """
    try:
        return subprocess.run(
            cmd,
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\nSTDOUT:\n{e.stdout}\nSTDERR:\n{e.stderr}"
        )


def run_stream(cmd: List[str]) -> None:
    """
    Run a subprocess and stream its output while keeping a single progress
    line "alive" (overwritten in place).
    """
    live = LiveLine()
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as p:
        assert p.stdout is not None
        for raw in p.stdout:
            line = raw.rstrip("\r\n")

            # Heuristic: progress lines usually start with '[' and contain '%'
            # and 'ETA' or 'Elapsed'.
            is_progress = line.startswith("[") and ("%" in line) and (
                "ETA" in line or "Elapsed" in line
            )

            if is_progress:
                live.update(line)
            else:
                # Normal log line: finish any active live line first
                live.done()
                print(line)

        ret = p.wait()
        live.done()
        if ret != 0:
            # User-friendly hint for common YouTube 403 cases
            hint = ""
            joined = " ".join(cmd)
            if "yt-dlp" in joined:
                hint = tr(
                    "\nHinweis: YouTube 403 kann an IPv6/CDN/Cookies liegen. "
                    "Versuche z.B. --force-ipv4, --cookies-from-browser edge (oder chrome), "
                    "und/oder --yt-android-client. Außerdem ggf. URL in Anführungszeichen setzen.",
                    "\nHint: YouTube 403 may be caused by IPv6/CDN/cookie issues. "
                    "Try e.g. --force-ipv4, --cookies-from-browser edge (or chrome), "
                    "and/or --yt-android-client. Also consider wrapping the URL in quotes.",
                )
            raise RuntimeError(f"Command failed: {' '.join(cmd)} (exit {ret}){hint}")


class LiveLine:
    """
    Helper to keep a single console line "alive" via carriage returns instead
    of printing new lines for progress updates.
    """

    def __init__(self) -> None:
        self._last_len = 0
        self._active = False

    def update(self, text: str) -> None:
        # Remove visible newlines so we remain on a single line
        text = text.replace("\r", " ").replace("\n", " ")
        sys.stdout.write("\r" + text)
        # Overwrite leftover characters from previous line
        if len(text) < self._last_len:
            sys.stdout.write(" " * (self._last_len - len(text)))
            sys.stdout.write("\r" + text)
        sys.stdout.flush()
        self._last_len = len(text)
        self._active = True

    def done(self) -> None:
        if self._active:
            sys.stdout.write("\n")
            sys.stdout.flush()
        self._last_len = 0
        self._active = False


def ensure_ffmpeg_available() -> None:
    """
    Ensure that ffmpeg and ffprobe are available.

    - On Windows: download and extract ffmpeg/ffprobe into ffmpeg-bin/ if needed.
    - On other platforms: expect ffmpeg/ffprobe to be available on PATH.
    """
    if not IS_WINDOWS:
        # On non-Windows, rely on system PATH
        if shutil.which(str(FFMPEG_EXE)) is None or shutil.which(str(FFPROBE_EXE)) is None:
            raise RuntimeError(
                tr(
                    "ffmpeg/ffprobe wurden nicht gefunden. Bitte installieren und in den PATH aufnehmen.",
                    "ffmpeg/ffprobe were not found. Please install them and ensure they are available on PATH.",
                )
            )
        return

    # Windows: use local binaries in ffmpeg-bin/
    if Path(FFMPEG_EXE).exists() and Path(FFPROBE_EXE).exists():
        return

    FFMPEG_DIR.mkdir(parents=True, exist_ok=True)

    ffmpeg_url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    info_msg(
        f"[SETUP] ffmpeg wurde nicht gefunden. Lade ffmpeg von:\n        {ffmpeg_url}",
        f"[SETUP] ffmpeg not found. Downloading from:\n        {ffmpeg_url}",
    )

    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        tmp_path = Path(tmp.name)

    try:
        with urllib.request.urlopen(ffmpeg_url) as resp, tmp_path.open("wb") as f:
            shutil.copyfileobj(resp, f)

        with zipfile.ZipFile(tmp_path, "r") as z:
            members = z.namelist()
            ffmpeg_member = None
            ffprobe_member = None
            for m in members:
                lower = m.replace("\\", "/").lower()
                if lower.endswith("/bin/ffmpeg.exe"):
                    ffmpeg_member = m
                elif lower.endswith("/bin/ffprobe.exe"):
                    ffprobe_member = m

            if not ffmpeg_member or not ffprobe_member:
                raise RuntimeError(
                    tr(
                        "Konnte ffmpeg.exe/ffprobe.exe in der heruntergeladenen ZIP nicht finden.",
                        "Could not find ffmpeg.exe/ffprobe.exe inside the downloaded ZIP archive.",
                    )
                )

            # Extract ffmpeg.exe
            with z.open(ffmpeg_member) as src, Path(FFMPEG_EXE).open("wb") as dst:
                shutil.copyfileobj(src, dst)
            # Extract ffprobe.exe
            with z.open(ffprobe_member) as src, Path(FFPROBE_EXE).open("wb") as dst:
                shutil.copyfileobj(src, dst)

        ok_msg(
            f"[SETUP] ffmpeg installiert nach: {FFMPEG_EXE}",
            f"[SETUP] ffmpeg installed to: {FFMPEG_EXE}",
        )
        ok_msg(
            f"[SETUP] ffprobe installiert nach: {FFPROBE_EXE}",
            f"[SETUP] ffprobe installed to: {FFPROBE_EXE}",
        )
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    if not (Path(FFMPEG_EXE).exists() and Path(FFPROBE_EXE).exists()):
        raise RuntimeError(
            tr(
                "ffmpeg-Setup ist fehlgeschlagen. Bitte manuell prüfen.",
                "ffmpeg setup failed. Please verify manually.",
            )
        )


def ensure_tool(name: str) -> None:
    """
    Ensure that a required tool is available.

    - 'yt-dlp' is checked as a Python package.
    - 'ffmpeg'/'ffprobe' are handled via ensure_ffmpeg_available().
    - All other tools are looked up via PATH.
    """
    if name == "yt-dlp":
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            raise RuntimeError(
                tr(
                    "'yt-dlp' ist in dieser Python-Umgebung nicht installiert. "
                    "Bitte in der 'ytdlp-split'-Umgebung z.B. mit "
                    "'pip install yt-dlp' nachinstallieren.",
                    "'yt-dlp' is not installed in this Python environment. "
                    "Please install it in the 'ytdlp-split' environment, e.g. "
                    "via 'pip install yt-dlp'.",
                )
            )
        return

    if name in ("ffmpeg", "ffprobe"):
        ensure_ffmpeg_available()
        return

    if shutil.which(name) is None:
        raise RuntimeError(
            tr(
                f"'{name}' wurde nicht im PATH gefunden. Bitte installieren und PATH prüfen.",
                f"'{name}' was not found on PATH. Please install it and check your PATH.",
            )
        )


def normalize_youtube_url(u: str, allow_playlists: bool) -> str:
    """
    Remove playlist-related parameters from YouTube URLs if playlists are not
    allowed while keeping other query parameters unchanged.
    """
    try:
        parsed = urlparse(u)
        if parsed.netloc.lower() in {"youtu.be", "www.youtu.be"}:
            # Short URLs rarely contain list=, but handle them anyway.
            qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if not allow_playlists:
                for key in ("list", "playlist", "start_radio", "index"):
                    qs.pop(key, None)
            new_query = urlencode(qs, doseq=True)
            return urlunparse(
                (parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment)
            )

        if "youtube.com" in parsed.netloc.lower():
            qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if not allow_playlists:
                for key in ("list", "playlist", "start_radio", "index"):
                    qs.pop(key, None)
            new_query = urlencode(qs, doseq=True)
            return urlunparse(
                (parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment)
            )
    except Exception:
        pass
    return u


def validate_cookies_from_browser(val: Optional[str]) -> Optional[str]:
    """
    Argument parser helper for --cookies-from-browser, ensuring only supported
    browser prefixes are allowed.
    """
    if not val:
        return None
    if not re.match(r"^(edge|chrome|firefox|brave|vivaldi|opera)(:.+)?$", val, re.IGNORECASE):
        raise argparse.ArgumentTypeError(
            tr(
                "Ungültig für --cookies-from-browser. Erlaubt: edge|chrome|firefox|brave|vivaldi|opera[:PROFILE]",
                "Invalid value for --cookies-from-browser. Allowed: edge|chrome|firefox|brave|vivaldi|opera[:PROFILE]",
            )
        )
    return val


def strip_option(cmd_list: List[str], opt: str) -> List[str]:
    """
    Remove an option from a command list and, if present, the argument that
    immediately follows that option.
    """
    out: List[str] = []
    i = 0
    n = len(cmd_list)
    while i < n:
        if cmd_list[i] == opt:
            # Skip option and potential argument
            i += 2 if i + 1 < n else 1
        else:
            out.append(cmd_list[i])
            i += 1
    return out


# ------------------------- YouTube download -------------------------


def download_audio_mp3(url: str, dl_dir: Path, allow_playlists: bool, keep_infojson: bool) -> Path:
    """
    Download audio as MP3 (192 kb/s) with embedded thumbnail and metadata.

    Additionally:
    - Writes an .info.json file (for uploader/channel information).
    - Sets/overwrites artist/album-artist on the resulting MP3 via stream copy,
      based on uploader/channel.
    """
    ensure_tool("yt-dlp")

    dl_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(dl_dir / "%(title)s.%(ext)s")

    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        ("--yes-playlist" if allow_playlists else "--no-playlist"),
        "-x",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "192K",
        "--embed-thumbnail",
        "--add-metadata",
        "--write-info-json",
        "--newline",
        "--progress-template",
        (
            "download:[%(progress._percent_str)s of %(progress._total_bytes_str)s "
            "at %(progress._speed_str)s ETA %(progress._eta_str)s | "
            "Elapsed %(progress._elapsed_str)s]"
        ),
        "-o",
        outtmpl,
    ]

    # Network/retry configuration
    if getattr(args_global, "force_ipv4", False):
        cmd.append("--force-ipv4")
    cmd += ["--retries", str(getattr(args_global, "retries", "10"))]
    cmd += ["--fragment-retries", str(getattr(args_global, "fragment_retries", "10"))]
    cmd += ["--retry-sleep", str(getattr(args_global, "retry_sleep", "linear=1::5"))]

    # Cookies
    if getattr(args_global, "cookies_from_browser", None):
        cmd += ["--cookies-from-browser", str(args_global.cookies_from_browser)]

    # YouTube extractor workaround: Android client emulation
    if getattr(args_global, "yt_android_client", False):
        cmd += ["--extractor-args", "youtube:player_client=android"]

    cmd.append(url)
    try:
        run_stream(cmd)
    except RuntimeError as e:
        msg = str(e)

        # Category C: cookie-related issues
        cookie_err = (
            ("Failed to decrypt with DPAPI" in msg)
            or ("Could not copy Chrome cookie database" in msg)
            or (("--cookies-from-browser" in " ".join(cmd)) and ("cookies from browser" in msg.lower()))
        )

        # Category A/B: outdated yt-dlp or SABR/HLS issues
        should_update = any(
            key in msg
            for key in [
                "HTTP Error 403",
                "unable to download video data",
                "returned error 403",
                "Some tv client https formats have been skipped",
                "SABR-only",
                "Server-Side Ad Placement",
                "Did not get any data blocks",
                "fragment not found",
                "missing a url",
                "unable to extract",
            ]
        )

        if cookie_err:
            warn_msg(
                "[WARN] Cookies konnten nicht gelesen/kopiert werden. Versuche ohne Cookies erneut …",
                "[WARN] Cookies could not be read/copied. Retrying without cookies …",
            )
            cmd_no_cookies = strip_option(cmd, "--cookies-from-browser")
            cmd_no_cookies = strip_option(cmd_no_cookies, "--cookies")
            run_stream(cmd_no_cookies)

        elif should_update:
            warn_msg(
                "[WARN] yt-dlp meldet einen bekannten Downloadfehler. Führe Update durch und versuche erneut …",
                "[WARN] yt-dlp reported a known download error. Updating yt-dlp and retrying …",
            )

            # Try updating yt-dlp
            subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"], check=False)

            # Second attempt
            try:
                run_stream(cmd)
            except RuntimeError:
                # If the second attempt also fails, proceed and check for a usable MP3 afterwards
                warn_msg(
                    "[WARN] Auch nach Update Fehler – prüfe trotzdem, ob eine MP3 erzeugt wurde …",
                    "[WARN] Error after update as well – still checking whether an MP3 was created …",
                )

        else:
            # Category B: some SABR/HLS errors may still yield a usable MP3
            if ("Did not get any data blocks" in msg) or ("fragment not found" in msg):
                warn_msg(
                    "[WARN] yt-dlp meldete HLS/SABR-Fehler, prüfe trotzdem auf erzeugte MP3 …",
                    "[WARN] yt-dlp reported HLS/SABR-related errors, still checking for a created MP3 …",
                )
            else:
                # All other errors: abort
                raise

    # Pick the most recently modified MP3 in the download directory
    mp3s = sorted(dl_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not mp3s:
        raise RuntimeError(
            tr("Kein MP3 nach Download gefunden.", "No MP3 file found after download.")
        )
    mp3 = mp3s[0]

    # Read uploader from .info.json and set it as artist/album-artist on the MP3
    uploader = read_uploader_from_infojson(mp3)
    if uploader:
        try:
            tag_original_with_uploader(mp3, uploader)
        except Exception as e:
            warn_msg(
                f"[WARN] Konnte Artist-Tag nicht setzen: {e}",
                f"[WARN] Could not set artist tag: {e}",
            )

    # Remove .info.json by default (unless explicitly requested to keep it)
    if not keep_infojson:
        deleted = delete_infojson_for_src(mp3)
        if deleted:
            info_msg(
                f"[INFO] {deleted} .info.json entfernt.",
                f"[INFO] Removed {deleted} .info.json file(s).",
            )

    return mp3


def download_video(
    url: str,
    dl_dir: Path,
    allow_playlists: bool,
    keep_infojson: bool,
) -> Path:
    """
    Download the full video (best available video+audio combination) and
    return the path to the resulting file.

    Behaviour:
    - Uses yt-dlp via `python -m yt_dlp`.
    - Supports optional playlist handling.
    - Optionally keeps or discards .info.json.
    - On typical yt-dlp-related errors, attempts a single yt-dlp update.
    """
    ensure_tool("yt-dlp")

    dl_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(dl_dir / "%(title)s.%(ext)s")

    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        ("--yes-playlist" if allow_playlists else "--no-playlist"),
        "--newline",
        "-f",
        "bv*+ba/b",  # best video + best audio fallback
        "--merge-output-format",
        "mp4",
        "--progress-template",
        (
            "download:[%(progress._percent_str)s of %(progress._total_bytes_str)s "
            "at %(progress._speed_str)s ETA %(progress._eta_str)s | "
            "Elapsed %(progress._elapsed_str)s]"
        ),
        "-o",
        outtmpl,
    ]

    # Network/retry configuration (aligned with audio download)
    if getattr(args_global, "force_ipv4", False):
        cmd.append("--force-ipv4")
    cmd += ["--retries", str(getattr(args_global, "retries", "10"))]
    cmd += ["--fragment-retries", str(getattr(args_global, "fragment_retries", "10"))]
    cmd += ["--retry-sleep", str(getattr(args_global, "retry_sleep", "linear=1::5"))]

    # Cookies
    if getattr(args_global, "cookies_from_browser", None):
        cmd += ["--cookies-from-browser", str(args_global.cookies_from_browser)]

    # YouTube extractor workaround (Android client)
    if getattr(args_global, "yt_android_client", False):
        cmd += ["--extractor-args", "youtube:player_client=android"]

    # Info JSON handling
    if keep_infojson:
        cmd.append("--write-info-json")
    else:
        cmd.append("--no-write-info-json")

    cmd.append(url)

    try:
        run_stream(cmd)
    except RuntimeError as e:
        msg = str(e)
        print(
            tr(
                f"[WARN] Fehler beim Herunterladen des Videos: {msg}",
                f"[WARN] Error while downloading the video: {msg}",
            )
        )
        print(
            tr(
                "[INFO] Versuche Fallback: yt-dlp Update …",
                "[INFO] Attempting fallback: yt-dlp update …",
            )
        )

        # Single yt-dlp update attempt
        subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"], check=False)

        # Second attempt (may still fail and raise)
        run_stream(cmd)

    # Select the most recently modified video file with a suitable extension
    candidates: List[Path] = []
    for ext in ("mp4", "mkv", "webm", "mov"):
        candidates.extend(dl_dir.glob(f"*.{ext}"))

    if not candidates:
        raise RuntimeError(
            tr(
                "Video wurde heruntergeladen, aber keine Datei gefunden.",
                "Video was downloaded, but no resulting file was found.",
            )
        )

    out = max(candidates, key=lambda p: p.stat().st_mtime)

    # Remove .info.json if not requested to keep it
    if not keep_infojson:
        deleted = delete_infojson_for_src(out)
        if deleted:
            print(
                tr(
                    f"[INFO] {deleted} .info.json entfernt.",
                    f"[INFO] Removed {deleted} .info.json file(s).",
                )
            )

    return out


# ------------------------- Chapters & metadata -------------------------


def read_uploader_from_infojson(mp3_path: Path) -> Optional[str]:
    """
    Read uploader/channel information from the corresponding .info.json file
    if present.
    """
    base = mp3_path.with_suffix("")
    candidates = [
        mp3_path.with_suffix(mp3_path.suffix + ".info.json"),
        base.with_suffix(".info.json"),
    ]
    for c in candidates:
        if c.exists():
            try:
                with c.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                uploader = data.get("uploader") or data.get("channel")
                if uploader:
                    return str(uploader)
            except Exception:
                pass
    return None


def infojson_candidates_for_src(src: Path) -> List[Path]:
    """
    Return a list of potential .info.json files related to a given source,
    if they exist.
    """
    cand = [
        src.with_suffix(src.suffix + ".info.json"),  # foo.mp3.info.json
        src.with_suffix(".info.json"),  # foo.info.json
    ]
    return [p for p in cand if p.exists()]


def delete_infojson_for_src(src: Path) -> int:
    """
    Delete all related .info.json files for the specified source.

    Returns the number of deleted files.
    """
    count = 0
    for p in infojson_candidates_for_src(src):
        try:
            if p.exists():
                p.unlink()
                count += 1
        except Exception as e:
            print(
                tr(
                    f"[WARN] Konnte {p.name} nicht löschen: {e}",
                    f"[WARN] Could not delete {p.name}: {e}",
                )
            )
    return count


def ffprobe_json(path: Path) -> Dict:
    """
    Run ffprobe on the given file and return the parsed JSON output including
    chapters, streams and format information.
    """
    cp = subprocess.run(
        [
            str(FFPROBE_EXE),
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_chapters",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",  # ffprobe JSON should not be altered
    )
    return json.loads(cp.stdout)


def get_attached_pic_present(probe: Dict) -> bool:
    """
    Check whether the ffprobe result contains a video stream that is marked
    as an attached picture (cover art).
    """
    for s in probe.get("streams", []):
        if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") == 1:
            return True
    return False


def get_chapters(probe: Dict) -> List[Dict]:
    """
    Extract chapter list from ffprobe JSON output.

    Each chapter is returned as:
      {"start": float, "end": float, "title": Optional[str]}
    """
    chapters: List[Dict] = []
    for ch in probe.get("chapters", []):
        start = float(ch.get("start_time", 0.0))
        end = float(ch.get("end_time", 0.0))
        tags = ch.get("tags") or {}
        title = tags.get("title") or tags.get("TITLE")
        if title:
            title = sanitize_filename(title)
        chapters.append({"start": start, "end": end, "title": title or None})
    return chapters


def sanitize_filename(name: str) -> str:
    """
    Sanitize a string for use as a filesystem-friendly filename:
    - Replace invalid characters.
    - Collapse whitespace.
    - Truncate to a reasonable length.
    """
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > 120:
        name = name[:120].rstrip()
    return name


def fmt_time(sec: float) -> str:
    """
    Format a duration in seconds as h:mm:ss or m:ss, depending on length.
    """
    if sec < 0:
        sec = 0
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


# (start, end, fade_seconds)
TimeRange = Tuple[Optional[float], Optional[float], float]


def parse_hms_to_seconds(token: str) -> float:
    """
    Convert a time token into seconds.

    Supported formats:
    - "90"        -> 90 seconds
    - "1:30"      -> 1 minute 30 seconds
    - "01:02:03"  -> 1 hour, 2 minutes, 3 seconds
    - "90s"       -> 90 seconds
    - "5m"        -> 5 minutes
    - "1h"        -> 1 hour
    """
    token = token.strip()

    # Simple form: 12, 12.5, 90s, 5m, 1h
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([hms])?", token)
    if m:
        val = float(m.group(1))
        unit = m.group(2)
        if unit == "h":
            return val * 3600.0
        elif unit == "m":
            return val * 60.0
        else:
            return val

    # With colons: mm:ss or hh:mm:ss
    parts = token.split(":")
    if len(parts) == 1:
        return float(parts[0])
    elif len(parts) == 2:
        m_, s_ = parts
        return float(m_) * 60.0 + float(s_)
    elif len(parts) == 3:
        h_, m_, s_ = parts
        return float(h_) * 3600.0 + float(m_) * 60.0 + float(s_)
    else:
        raise ValueError(tr(f"Ungueltiges Zeitformat: {token!r}", f"Invalid time format: {token!r}"))


def parse_timecode_spec(spec: str) -> List[Tuple[float, Optional[float], float]]:
    """
    Parse a timecode specification string into a list of ranges with
    optional fade durations.

    Returns a list of tuples: (start, end, fade)
      - start: seconds from file start (>= 0)
      - end  : seconds from file start or None for "until end"
      - fade : fade duration in seconds (>= 0)

    Supported segment formats (semicolon-separated):
      "90"             -> (0, 90, DEFAULT_FADE_SECONDS)
      "1:30"           -> (0, 90, DEFAULT_FADE_SECONDS)
      "1:00-2:30"      -> (60, 150, DEFAULT_FADE_SECONDS)
      "-2:00"          -> (0, 120, DEFAULT_FADE_SECONDS)
      "1:00-"          -> (60, None, DEFAULT_FADE_SECONDS)

    Each of the above can be extended with a fade length using "@":

      "0:30-1:00@0.5"  -> fade = 0.5 seconds
      "1:00-2:00@0"    -> fade disabled (0 seconds)
      "2:00-3:00@1.2"  -> fade = 1.2 seconds

    Multiple ranges:
      "0:30-1:00@0.5; 1:55-2:05@0"
    """
    ranges: List[Tuple[float, Optional[float], float]] = []

    if not spec:
        return ranges

    # Segments separated by ';'
    parts = [p.strip() for p in spec.split(";") if p.strip()]
    for part in parts:
        fade = DEFAULT_FADE_SECONDS

        # Optional fade argument introduced by '@',
        # e.g. "0:30-1:00@0.5" or "-2:00@0".
        if "@" in part:
            range_part, fade_part = part.rsplit("@", 1)
            range_part = range_part.strip()
            fade_part = fade_part.strip()
            if fade_part:
                try:
                    fade = float(fade_part)
                    if fade < 0:
                        fade = 0.0
                except ValueError:
                    # Invalid fade specification -> fallback to default
                    fade = DEFAULT_FADE_SECONDS
        else:
            range_part = part

        range_part = range_part.strip()
        if not range_part:
            continue

        start: float
        end: Optional[float]

        # With '-': start-end, -end, start-
        if "-" in range_part:
            if range_part.startswith("-"):
                # "-2:00" -> 0 to 2:00
                start = 0.0
                end = parse_hms_to_seconds(range_part[1:].strip())
            elif range_part.endswith("-"):
                # "1:00-" -> 1:00 to end
                start = parse_hms_to_seconds(range_part[:-1].strip())
                end = None
            else:
                # "1:00-2:30"
                s_str, e_str = range_part.split("-", 1)
                start = parse_hms_to_seconds(s_str.strip())
                end = parse_hms_to_seconds(e_str.strip())
        else:
            # Single value: "90" -> 0 to 90
            start = 0.0
            end = parse_hms_to_seconds(range_part)

        if end is not None and end <= start:
            # Invalid/negative range: ignore
            print(
                tr(
                    f"[WARN] Ungueltiger Timecode-Range (end <= start) ignoriert: {part!r}",
                    f"[WARN] Invalid timecode range (end <= start) ignored: {part!r}",
                )
            )
            continue

        ranges.append((start, end, fade))

    return ranges


def resolve_sp_in_spec(spec: str, split_points: List[float], total_dur: float) -> str:
    """
    Resolve 'sp' notation used in a timecode spec into concrete second values.

    Supported special segment forms (semicolon-separated):

      "<TC>-sp"  -> range from <TC> to the next split point after <TC>
      "sp-<TC>"  -> range from the previous split point before <TC> to <TC>

    Examples (with split_points [0.0, 60.0, 120.0]):

      "1:00-sp"   -> "60-120"
      "sp-2:00"   -> "60-120"

    All other forms are returned unchanged.
    """
    if not spec or not split_points:
        return spec

    out_parts: List[str] = []
    for raw_part in spec.split(";"):
        part = raw_part.strip()
        if not part:
            continue

        # Preserve optional '@' suffix (e.g. for fade) unchanged
        if "@" in part:
            core, fade = part.rsplit("@", 1)
            core = core.strip()
            fade_suffix = "@" + fade.strip()
        else:
            core = part
            fade_suffix = ""

        core = core.strip()
        new_core = core

        try:
            # "<TC>-sp"  -> from TC to the next split point
            if "-sp" in core and not core.startswith("sp-"):
                left = core.replace("-sp", "").strip()
                start = parse_hms_to_seconds(left)

                # Next split point strictly after start
                candidates = [sp for sp in split_points if sp > start + 1e-6]
                if candidates:
                    end = candidates[0]
                else:
                    # No later split point -> until end of file
                    end = total_dur if total_dur > start else start

                new_core = f"{int(round(start))}-{int(round(end))}"

            # "sp-<TC>"  -> from the previous split point to TC
            elif core.startswith("sp-"):
                right = core[3:].strip()
                end = parse_hms_to_seconds(right)

                candidates = [sp for sp in split_points if sp < end - 1e-6]
                if candidates:
                    start = candidates[-1]
                else:
                    # No earlier split point -> from file start
                    start = 0.0

                new_core = f"{int(round(start))}-{int(round(end))}"

        except Exception:
            # On error, keep the segment unchanged
            new_core = core

        out_parts.append(new_core + fade_suffix)

    return ";".join(out_parts)


def format_seconds_for_name(sec: float) -> str:
    """
    Format a number of seconds in a compact style suitable for filenames,
    for example:
      65s   -> "01m05s"
      3661s -> "01h01m01s"
    """
    sec_int = int(round(max(0.0, sec)))
    m, s = divmod(sec_int, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}h{m:02d}m{s:02d}s"
    elif m > 0:
        return f"{m:02d}m{s:02d}s"
    else:
        return f"{s:02d}s"


# ------------------------- Cover handling -------------------------


def extract_cover_to_tmp(src: Path, tmpdir: Path) -> Optional[Path]:
    """
    Extract the embedded cover image as JPEG or PNG into the specified
    temporary directory. Returns the path to the extracted file or None.
    """
    cover_jpg = tmpdir / "cover.jpg"
    cover_png = tmpdir / "cover.png"
    for p in (cover_jpg, cover_png):
        if p.exists():
            p.unlink()
    # Try JPEG first
    try:
        run(
            [
                str(FFMPEG_EXE),
                "-y",
                "-i",
                str(src),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                str(cover_jpg),
            ]
        )
        if cover_jpg.exists() and cover_jpg.stat().st_size > 0:
            return cover_jpg
    except Exception:
        pass
    # Fallback: PNG
    try:
        run(
            [
                str(FFMPEG_EXE),
                "-y",
                "-i",
                str(src),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                str(cover_png),
            ]
        )
        if cover_png.exists() and cover_png.stat().st_size > 0:
            return cover_png
    except Exception:
        pass
    return None


# ------------------------- Split logic -------------------------


def tag_original_with_uploader(src: Path, uploader: str) -> None:
    """
    Set artist/album-artist on the original MP3 using stream copy (no re-encode).
    """
    tmp = src.with_suffix(".tagtmp.mp3")
    cmd = [
        str(FFMPEG_EXE),
        "-y",
        "-i",
        str(src),
        "-map",
        "0",  # keep all streams including attached_pic
        "-c",
        "copy",
        "-metadata",
        f"artist={uploader}",
        "-metadata",
        f"album_artist={uploader}",
        str(tmp),
    ]
    run(cmd)
    tmp.replace(src)


def split_with_ffmpeg(
    src: Path,
    outdir: Path,
    chapters: List[Dict],
    album: str,
    keep_cover: bool,
    uploader: Optional[str],
) -> List[Path]:
    """
    Create chapter-based segment files via ffmpeg and set metadata on each.

    A simple overall progress bar (ETA/elapsed) is printed for the entire
    source.
    """
    produced: List[Path] = []
    total = len(chapters)
    bar_width = 24
    t0 = time.perf_counter()
    live = LiveLine()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cover = extract_cover_to_tmp(src, tmp) if keep_cover else None

        for i, ch in enumerate(chapters, start=1):
            start = ch["start"]
            end = ch["end"]
            dur = max(0.01, end - start)
            title = ch["title"] or f"Track {i:02d}"
            base = sanitize_filename(title)
            out = outdir / f"{src.stem} - {i:02d} - {base}.mp3"

            # Overall progress across all segments
            filled = int(round(bar_width * i / float(total)))
            bar = "#" * filled + "-" * (bar_width - filled)
            elapsed = time.perf_counter() - t0
            avg = elapsed / i
            eta = avg * (total - i)
            live.update(
                f"[{bar}] {i}/{total}  {title}  |  elapsed {fmt_time(elapsed)}  |  ETA {fmt_time(eta)}"
            )

            cmd = [
                str(FFMPEG_EXE),
                "-y",
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{dur:.3f}",
                "-i",
                str(src),
            ]
            if cover is not None:
                cmd += ["-i", str(cover)]
            cmd += ["-map", "0:a:0"]
            if cover is not None:
                cmd += ["-map", "1:0"]
            cmd += ["-c:a", "libmp3lame", "-b:a", "192k"]
            if cover is not None:
                cmd += [
                    "-c:v",
                    "mjpeg",
                    "-disposition:v:0",
                    "attached_pic",
                    "-metadata:s:v:0",
                    "title=Album cover",
                    "-metadata:s:v:0",
                    "comment=Cover (front)",
                ]
            cmd += [
                "-metadata",
                f"title={title}",
                "-metadata",
                f"track={i}",
                "-metadata",
                f"album={album}",
            ]
            if uploader:
                cmd += [
                    "-metadata",
                    f"artist={uploader}",
                    "-metadata",
                    f"album_artist={uploader}",
                ]
            cmd += [str(out)]

            run(cmd)
            produced.append(out)

    live.done()
    return produced


def get_media_duration_seconds(path: Path) -> float:
    """
    Determine the duration of an audio/video file in seconds via ffprobe.

    Returns 0.0 if the duration cannot be determined.
    """
    try:
        data = ffprobe_json(path)
    except Exception as e:
        warn_msg(
            f"[WARN] Konnte Dauer fuer {path.name} nicht ermitteln: {e}",
            f"[WARN] Could not determine duration for {path.name}: {e}",
        )
        return 0.0

    fmt = data.get("format") or {}
    dur_str = fmt.get("duration")
    if dur_str:
        try:
            return float(dur_str)
        except ValueError:
            pass

    # Fallback: maximum duration across streams
    max_dur = 0.0
    for s in data.get("streams", []):
        d = s.get("duration")
        if d:
            try:
                val = float(d)
                if val > max_dur:
                    max_dur = val
            except ValueError:
                pass
    return max_dur


def apply_timecodes_to_file(
    src: Path,
    spec: str,
    pre_parsed: Optional[List[TimeRange]],
    is_video: bool,
) -> List[Path]:
    """
    Apply timecode ranges to a downloaded file.

    Parameters
    ----------
    src : Path
        Source file (audio or video).
    spec : str
        Original timecode specification string (e.g. "0:30-1:00;1:55-2:05" or with "sp").
    pre_parsed : Optional[List[TimeRange]]
        Pre-parsed ranges (including fade), or None.
    is_video : bool
        Controls processing mode:
        - True: video trim with stream copy
        - False: audio re-encode with optional fade

    Extended notation
    -----------------
    The function additionally supports the "sp" notation:

      "<TC>-sp"  -> range from <TC> to the next split point (chapter start)
      "sp-<TC>"  -> range from the previous split point to <TC>

    Returns
    -------
    List[Path]
        List of generated output files.

    Notes
    -----
    Fade values from the timecode specification are currently applied only
    to audio clips (is_video=False).
    """
    ensure_ffmpeg_available()
    total = get_media_duration_seconds(src)

    # Derive potential split points from chapters (chapter start times)
    split_points: List[float] = []
    try:
        probe = ffprobe_json(src)
        chapters = get_chapters(probe)
        if chapters:
            split_points = sorted({float(ch.get("start", 0.0)) for ch in chapters})
            # Ensure that 0.0 is present as a possible start point
            if split_points and split_points[0] > 0.0:
                split_points.insert(0, 0.0)
    except Exception as e:
        if "sp" in spec:
            warn_msg(
                f"[WARN] Konnte Kapitel/Splitpunkte nicht ermitteln ({e}). 'sp'-Notation wird ignoriert.",
                f"[WARN] Couldn't determine chapters/split points ({e}). 'sp' notation gets ignored.",
            )
        split_points = []

    # Determine ranges (taking "sp" into account)
    ranges: List[TimeRange] = []

    if "sp" in spec:
        if not split_points:
            warn_msg(
                "[WARN] 'sp'-Notation verwendet, aber keine Kapitel/Splitpunkte gefunden – Timecodes werden uebersprungen.",
                "[WARN] 'sp' used, but no chapters/split points found – time codes are skipped.",
            )
            return []
        resolved_spec = resolve_sp_in_spec(spec, split_points, total)
        try:
            ranges = parse_timecode_spec(resolved_spec)
        except Exception as e:
            warn_msg(
                f"[WARN] Konnte Timecode-Spec (nach 'sp'-Aufloesung) nicht parsen: {e}",
                f"[WARN] Couldn't parse timecode-spec (after resolving 'sp'): {e}",
            )
            return []
    elif pre_parsed is not None:
        ranges = pre_parsed
    else:
        try:
            ranges = parse_timecode_spec(spec)
        except Exception as e:
            warn_msg(
                f"[WARN] Konnte Timecode-Spec nicht parsen: {e}",
                f"[WARN] Couldn't parse timecode spec: {e}",
            )
            return []

    # Configuration for filenames (timecodes / fade)
    cfg = (SETTINGS or {}).get("timecode_filename", {}) if SETTINGS is not None else {}
    include_range = bool(cfg.get("include_range", True))
    include_fade = bool(cfg.get("include_fade", True))

    produced: List[Path] = []
    stem = src.stem
    ext = src.suffix  # e.g. ".mp3" or ".mp4"

    for idx, (start, end, fade_raw) in enumerate(ranges, start=1):
        # None -> open range
        s = start if start is not None else 0.0
        e = end if end is not None else total

        if e <= s:
            warn_msg(
                f"[WARN] Timecode-Range #{idx} hat keine positive Dauer: start={s}, end={e}",
                f"[WARN] Timecode range #{idx} has no positive duration: start={s}, end={e}",
            )
            continue

        dur = e - s

        # Normalize fade value
        try:
            fade = float(fade_raw)
        except (TypeError, ValueError):
            fade = DEFAULT_FADE_SECONDS

        if fade < 0:
            fade = 0.0

        if fade > 0:
            # Fade must not exceed half of the segment duration
            max_fade = max(0.0, dur / 2.0 - 0.001)
            if fade > max_fade:
                fade = max_fade
        else:
            fade = 0.0

        # Build suffix for filenames (controlled via settings)
        suffix_parts: List[str] = []

        if include_range:
            start_label = format_seconds_for_name(s)
            end_label = format_seconds_for_name(e)
            suffix_parts.append(f"{start_label}-{end_label}")

        if include_fade and fade > 0.0:
            # e.g. "f2.5s" or "f1s"
            if float(fade).is_integer():
                fade_str = f"{int(fade)}s"
            else:
                fade_str = f"{fade:.1f}s".rstrip("0").rstrip(".")
            suffix_parts.append(f"f{fade_str}")

        if suffix_parts:
            name_suffix = "_" + "_".join(suffix_parts)
        else:
            name_suffix = ""

        out = src.with_name(f"{stem}__tc{idx:02d}{name_suffix}{ext}")

        info_msg(
            f"[TC] Erzeuge Clip {idx}: {s:.1f}s - {e:.1f}s -> {out.name}",
            f"[TC] Creating clip {idx}: {s:.1f}s - {e:.1f}s -> {out.name}",
        )

        # Build ffmpeg command
        cmd: List[str] = [
            str(FFMPEG_EXE),
            "-y",
            "-ss",
            f"{s:.3f}",
            "-to",
            f"{e:.3f}",
            "-i",
            str(src),
        ]

        if is_video:
            # Video: currently only trim using stream copy
            cmd += ["-c", "copy"]
        else:
            # Audio: re-encode with optional fade
            cmd += ["-vn", "-c:a", "libmp3lame", "-b:a", "192k"]

            if fade > 0.0:
                fade_in = fade
                fade_out = fade
                fade_out_start = max(0.0, dur - fade_out)

                afilter = (
                    f"afade=t=in:st=0:d={fade_in:.3f},"
                    f"afade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f}"
                )
                cmd += ["-af", afilter]

        cmd += [str(out)]

        run(cmd)
        produced.append(out)
        ok_msg(
            f"[OK] Timecode-Clip {idx}: {out.name}",
            f"[OK] Timecode clip #{idx}: {out.name}",
        )

    if not produced:
        warn_msg(
            "[WARN] Keine gültigen Timecode-Clips erzeugt.",
            "[WARN] No valid timecode clips were created.",
        )
    return produced


# ------------------------- CUE (optional, basic) -------------------------


def parse_cue(cue_path: Path) -> List[Dict]:
    """
    Minimal .cue parser supporting lines of the form:

        INDEX 01 hh:mm:ss

    Returns a chapter list compatible with get_chapters().
    """
    tracks: List[Tuple[str, float]] = []
    current_title: Optional[str] = None
    with cue_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            mtitle = re.match(r'^\s*TITLE\s+"(.*)"', line, re.I)
            if mtitle:
                current_title = sanitize_filename(mtitle.group(1))
                continue
            mindex = re.match(r"^\s*INDEX\s+01\s+(\d\d):(\d\d):(\d\d)", line, re.I)
            if mindex:
                mm, ss, ff = map(int, mindex.groups())
                start = mm * 60 + ss + ff / 75.0
                tracks.append((current_title or f"Track {len(tracks)+1:02d}", start))

    chapters: List[Dict] = []
    for idx, (t, start) in enumerate(tracks):
        # ffmpeg will clamp the last chapter to the actual file end
        end = tracks[idx + 1][1] if idx + 1 < len(tracks) else start + 1e6
        chapters.append({"start": float(start), "end": float(end), "title": t})
    return chapters


# ------------------------- Orchestration -------------------------


def process_source(
    src: Path,
    split_dir: Path,
    trash_after: bool,
    ask_trash: bool,
    defer_to_batch: bool,
    trash_queue: List[Path],
    keep_infojson: bool,
) -> None:
    """
    Split a single source file based on chapters (or optional .cue) and
    enqueue the source and related metadata files for deletion.
    """
    probe = ffprobe_json(src)
    chapters = get_chapters(probe)
    if not chapters:
        cue = src.with_suffix(".cue")
        if cue.exists():
            chapters = parse_cue(cue)
    if not chapters:
        warn_msg(
            f"[WARN] Keine Kapitel/CUEs gefunden: {src.name}. Überspringe Split.",
            f"[WARN] No chapters/CUEs found: {src.name}. Skipping split.",
        )
        return

    keep_cover = get_attached_pic_present(probe)
    album = src.stem
    split_dir.mkdir(parents=True, exist_ok=True)
    uploader = read_uploader_from_infojson(src)

    produced = split_with_ffmpeg(src, split_dir, chapters, album, keep_cover, uploader)
    ok_msg(
        f"[OK] Gesplittet: {src.name} -> {len(produced)} Dateien",
        f"[OK] Split done: {src.name} -> {len(produced)} files",
    )

    # Remove .info.json by default after it has been read (if any)
    if not keep_infojson:
        deleted = delete_infojson_for_src(src)
        if deleted:
            info_msg(
                f"[INFO] {deleted} .info.json entfernt.",
                f"[INFO] Removed {deleted} .info.json file(s).",
            )

    # Default: always delete originals (no interactive confirmation here).
    # Files are collected in trash_queue, which may be processed immediately
    # or in a batch step at the end.

    # Queue the source file for deletion
    trash_queue.append(src)

    # Queue any corresponding .cue and info.json files
    cue = src.with_suffix(".cue")
    if cue.exists():
        trash_queue.append(cue)

    for j in infojson_candidates_for_src(src):
        trash_queue.append(j)

    # If no batch mode is active, delete immediately
    if not defer_to_batch:
        if send2trash is None:
            warn_msg(
                "[HINWEIS] send2trash nicht installiert. Bitte 'pip install Send2Trash' ausführen.",
                "[NOTE] send2trash not installed. Please run 'pip install Send2Trash'.",
            )
        else:
            for p in trash_queue:
                if p.exists():
                    send2trash(str(p))
            ok_msg(
                    f"[OK] Quelle + Metadaten gelöscht: {src.name}",
                    f"[OK] Source and metadata files moved to recycle bin: {src.name}",
            )
        trash_queue.clear()
    # -------------- end of deletion logic --------------


def read_urls(args: argparse.Namespace) -> List[str]:
    """
    Read URLs from a file (if specified) plus any positional URL arguments.
    Optionally falls back to 'urls.txt' if neither is specified.

    Playlist parameters are removed from YouTube URLs when playlists are not
    allowed.
    """
    urls: List[str] = []
    if args.urls:
        path = Path(args.urls)
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        urls.append(line)
        else:
            warn_msg(
                f"[WARN] URLs-Datei nicht gefunden: {args.urls}",
                f"[WARN] URLs file not found: {args.urls}",
            )
    urls += args.url

    # Fallback: if no URLs were collected and no file was specified,
    # try the default 'urls.txt' in the current working directory.
    if not urls and not args.url and not args.urls:
        default_file = Path("urls.txt")
        if default_file.exists():
            with default_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        urls.append(line)
            info_msg(
                "[INFO] Standard-urls.txt geladen.",
                "[INFO] Loaded default urls.txt.",
            )

    # Normalize URLs if playlists are not allowed
    if not args.playlists:
        urls = [normalize_youtube_url(u, allow_playlists=False) for u in urls]
    return urls


# Global reference to parsed args, used by download helpers
args_global = None  # will be set in main()


def main():
    # Load settings first so that help/description can be localized
    settings = load_settings()
    lang = get_language(settings)

    def lh(de: str, en: str) -> str:
        """
        Localized help/description text helper.
        Returns the German or English string based on the active language.
        """
        return de if lang == "de" else en

    parser = argparse.ArgumentParser(
        prog="ytdlp-split",
        description=lh(
            "YouTube 192 kb/s Download & Kapitel-Splitting",
            "YouTube 192 kb/s Download & Chapter-Splitting",
        ),
    )

    # Positional argument(s)
    parser.add_argument(
        "url",
        nargs="*",
        help=lh(
            "YouTube-URL(s) oder Playlists",
            "YouTube URL(s) or playlists",
        ),
    )

    # Sources
    parser.add_argument(
        "-u",
        "--urls",
        nargs="?",                 # optional argument
        const="urls.txt",          # default file when only -u is provided
        default=None,              # if -u is omitted entirely
        metavar="FILE",
        help=lh(
            "Pfad zu einer Textdatei mit YouTube-URLs/Playlists (eine pro Zeile). "
            "Wird FILE weggelassen, wird standardmäßig 'urls.txt' verwendet.",
            "Path to a text file containing YouTube URLs/playlists (one per line). "
            "If FILE is omitted, 'urls.txt' is used by default.",
        ),
    )

    # Directories
    parser.add_argument(
        "-d",
        "--dl-dir",
        metavar="DIR",
        default=None,
        help=lh(
            "Download-Ordner für Originaldateien. "
            "Standard: audio_dl_dir bzw. video_dl_dir aus settings.json.",
            "Download directory for original files. "
            "Default: audio_dl_dir or video_dl_dir from settings.json.",
        ),
    )
    parser.add_argument(
        "-s",
        "--split-dir",
        metavar="DIR",
        default=None,
        help=lh(
            "Ausgabeordner für gesplittete Dateien. "
            "Standard: split_dir aus settings.json.",
            "Output directory for split files. "
            "Default: split_dir from settings.json.",
        ),
    )

    # Modes (mutually exclusive, lightweight)
    mode_grp = parser.add_mutually_exclusive_group()
    mode_grp.add_argument(
        "-n",
        "--download-only",
        action="store_true",
        help=lh(
            "Nur Download, keine Kapitel-Splits.",
            "Download only, do not split into chapters.",
        ),
    )
    mode_grp.add_argument(
        "-x",
        "--split-existing",
        action="store_true",
        help=lh(
            "Vorhandene MP3-Dateien im Download-Ordner in Kapitel splitten.",
            "Split existing MP3 files in the download directory.",
        ),
    )
    mode_grp.add_argument(
        "--download-video",
        action="store_true",
        help=lh(
            "Vollständiges Video herunterladen statt Audio zu extrahieren (kein Split).",
            "Download the full video instead of extracting audio (no split).",
        ),
    )
    parser.add_argument(
        "--timecodes",
        metavar="SPEC",
        help=lh(
            "Timecode-Bereiche zum Trimmen, z.B. "
            "'1:00-2:30', '90-150', '1:00-;2:00-3:00'. "
            "Mehrere Bereiche werden mit ';' getrennt.",
            "Timecode ranges for trimming, e.g. "
            "'1:00-2:30', '90-150', '1:00-;2:00-3:00'. "
            "Multiple ranges separated by ';'.",
        ),
    )

    # Playlists
    parser.add_argument(
        "-p",
        "--playlists",
        action="store_true",
        help=lh(
            "Playlists erlauben (standardmäßig werden Playlists ignoriert).",
            "Allow playlists (by default, playlist processing is disabled).",
        ),
    )

    # Info.json retention
    parser.add_argument(
        "--keep-infojson",
        action="store_true",
        help=lh(
            "Von yt-dlp erzeugte .info.json-Dateien beibehalten "
            "(Standard: werden nach Verwendung gelöscht).",
            "Keep the .info.json files generated by yt-dlp "
            "(default: they are removed after use).",
        ),
    )

    # Per-source recycle bin options
    trash_grp = parser.add_mutually_exclusive_group()
    trash_grp.add_argument(
        "-c",
        "--trash-confirm",
        action="store_true",
        help=lh(
            "Nach jedem Split pro Quelle fragen, ob die Datei in den Papierkorb verschoben werden soll.",
            "Ask per source whether to move it to the recycle bin after splitting.",
        ),
    )
    trash_grp.add_argument(
        "-r",
        "--trash-after",
        action="store_true",
        help=lh(
            "Quellen nach dem Split automatisch in den Papierkorb verschieben.",
            "Move sources to the recycle bin automatically after splitting.",
        ),
    )

    # Batch variants (override per-source variants)
    batch_grp = parser.add_mutually_exclusive_group()
    batch_grp.add_argument(
        "-C",
        "--trash-confirm-batch",
        action="store_true",
        help=lh(
            "Am Ende einmalig fragen, ob ALLE Quellen/CUEs in den Papierkorb verschoben werden sollen.",
            "At the end, ask once whether ALL sources/CUEs should be moved to the recycle bin.",
        ),
    )
    batch_grp.add_argument(
        "-R",
        "--trash-after-batch",
        action="store_true",
        help=lh(
            "Am Ende ALLE Quellen/CUEs automatisch in den Papierkorb verschieben.",
            "At the end, move ALL sources/CUEs to the recycle bin automatically.",
        ),
    )

    # Network/retries
    parser.add_argument(
        "--force-ipv4",
        action="store_true",
        help=lh(
            "IPv4 für Downloads erzwingen (hilft häufig bei 403-/IPv6-/CDN-Problemen).",
            "Force IPv4 for downloads (often helps with 403/IPv6/CDN issues).",
        ),
    )
    parser.add_argument(
        "--retries",
        default="10",
        metavar="N|infinite",
        help=lh(
            "Anzahl der Wiederholungsversuche bei Fehlern (Standard: 10).",
            "Number of retries on errors (default: 10).",
        ),
    )
    parser.add_argument(
        "--fragment-retries",
        default="10",
        metavar="N|infinite",
        help=lh(
            "Anzahl der Wiederholungsversuche pro Fragment (Standard: 10).",
            "Number of retries per fragment (default: 10).",
        ),
    )
    parser.add_argument(
        "--retry-sleep",
        default="linear=1::5",
        metavar="SPEC",
        help=lh(
            "Backoff-Spezifikation zwischen Wiederholungsversuchen (Standard: linear=1::5).",
            "Backoff specification between retries (default: linear=1::5).",
        ),
    )

    # YouTube workarounds
    parser.add_argument(
        "--yt-android-client",
        action="store_true",
        help=lh(
            "YouTube-Extractor als Android-Client emulieren (kann bei 403/Rate-Limits helfen).",
            "Emulate Android client in YouTube extractor (can help with 403/rate limits).",
        ),
    )

    parser.add_argument(
        "--cookies-from-browser",
        type=validate_cookies_from_browser,
        metavar="BROWSER[:PROFILE]",
        help=lh(
            "Browser-Cookies verwenden, z.B. 'brave:Default', 'chrome:Profile 1', 'edge' (ohne Profil).",
            "Use browser cookies, e.g. 'brave:Default', 'chrome:Profile 1', 'edge' (no profile).",
        ),
    )

    # Load settings (language + path defaults) before parsing args to show hint early
    settings = load_settings()
    global SETTINGS, LANG
    SETTINGS = settings
    LANG = get_language(settings)
    print_language_hint(LANG)

    args = parser.parse_args()
    global args_global
    args_global = args

    paths_cfg = settings.get("paths", {})

    audio_dl_default = (
        Path(paths_cfg.get("audio_dl_dir", os.path.normpath(r"C:\ytdlp-split")))
        .expanduser()
        .resolve()
    )
    video_dl_default = (
        Path(paths_cfg.get("video_dl_dir", os.path.normpath(r"C:\ytdlp-split")))
        .expanduser()
        .resolve()
    )
    split_default = (
        Path(paths_cfg.get("split_dir", os.path.normpath(r"C:\ytdlp-split")))
        .expanduser()
        .resolve()
    )

    # Determine effective download/split paths:
    # - If --dl-dir/--split-dir provided, they take precedence.
    # - Otherwise: use audio_dl_dir/video_dl_dir and split_dir from settings.json.
    if args.dl_dir:
        dl_dir = Path(args.dl_dir).expanduser().resolve()
    else:
        dl_dir = video_dl_default if args.download_video else audio_dl_default

    if args.split_dir:
        split_dir = Path(args.split_dir).expanduser().resolve()
    else:
        split_dir = split_default

    dl_dir.mkdir(parents=True, exist_ok=True)
    split_dir.mkdir(parents=True, exist_ok=True)

    # Timecodes (optional)
    tc_spec: Optional[str] = None
    tc_ranges: Optional[List[TimeRange]] = None
    if args.timecodes:
        tc_spec = args.timecodes.strip()
        try:
            # If no 'sp' notation is present, ranges can be parsed immediately.
            # For 'sp', resolution is deferred until actual split points are known.
            if "sp" not in tc_spec:
                tc_ranges = parse_timecode_spec(tc_spec)
            info_msg(
                f"[INFO] Timecodes aktiviert: {tc_spec}",
                f"[INFO] Timecodes enabled: {tc_spec}",
            )
        except Exception as e:
            warn_msg(
                f"[WARN] Konnte Timecode-Spec nicht parsen: {e}",
                f"[WARN] Could not parse timecode spec: {e}",
            )
            tc_spec = None
            tc_ranges = None

    # Determine sources
    if not args.split_existing:
        urls = read_urls(args)
        if not urls:
            error_msg(
                "Bitte YouTube-URL(s)/Playlist(s) angeben oder --urls verwenden.",
                "Please provide YouTube URL(s)/playlists or use --urls.",
            )
            sys.exit(1)

        info_msg(
            f"[INFO] Lade {len(urls)} URL(s)/Playlist(s) herunter …",
            f"[INFO] Downloading {len(urls)} URL(s)/playlist(s) …",
        )
        downloads: List[Path] = []

        for u in urls:
            u_norm = normalize_youtube_url(u, allow_playlists=args.playlists)

            if args.download_video:
                info_msg(
                    f"[INFO] Lade vollständiges Video: {u_norm}",
                    f"[INFO] Downloading full video: {u_norm}",
                )
                out = download_video(
                    u_norm,
                    dl_dir,
                    allow_playlists=args.playlists,
                    keep_infojson=args.keep_infojson,
                )
                downloads.append(out)
                ok_msg(
                    f"[OK] Video gespeichert: {out.name}",
                    f"[OK] Saved video: {out.name}",
                )

                if tc_spec:
                    info_msg(
                        f"[INFO] Wende Timecodes auf Video an: {tc_spec}",
                        f"[INFO] Applying timecodes to video: {tc_spec}",
                    )
                    apply_timecodes_to_file(out, tc_spec, tc_ranges, is_video=True)

                    # Remove original video after timecode trimming
                    try:
                        out.unlink()
                        info_msg(
                            f"[INFO] Original-Video nach Timecode-Trim entfernt: {out.name}",
                            f"[INFO] Original video removed after timecode trimming: {out.name}",
                        )
                    except Exception as e:
                        warn_msg(
                            f"[WARN] Konnte Original-Video nicht entfernen: {e}",
                            f"[WARN] Could not remove original video: {e}",
                        )

                    deleted = delete_infojson_for_src(out)
                    if deleted:
                        info_msg(
                            f"[INFO] {deleted} .info.json nach Timecode-Trim entfernt.",
                            f"[INFO] Removed {deleted} .info.json file(s) after timecode trimming.",
                        )

            else:
                info_msg(
                    f"[INFO] Lade Audio: {u_norm}",
                    f"[INFO] Downloading audio: {u_norm}",
                )
                mp3 = download_audio_mp3(
                    u_norm,
                    dl_dir,
                    allow_playlists=args.playlists,
                    keep_infojson=args.keep_infojson,
                )
                downloads.append(mp3)
                ok_msg(
                    f"[OK] Geladen: {mp3.name}",
                    f"[OK] Downloaded: {mp3.name}",
                )

                if tc_spec:
                    info_msg(
                        f"[INFO] Wende Timecodes auf Audio an: {tc_spec}",
                        f"[INFO] Applying timecodes to audio: {tc_spec}",
                    )
                    apply_timecodes_to_file(mp3, tc_spec, tc_ranges, is_video=False)

                    # Remove original audio after timecode trimming
                    try:
                        mp3.unlink()
                        info_msg(
                            f"[INFO] Original-Video nach Timecode-Trim entfernt: {out.name}",
                            f"[INFO] Original video removed after timecode trimming: {out.name}",
                        )
                    except Exception as e:
                        warn_msg(
                            f"[WARN] Konnte Original-Audio nicht entfernen: {e}",
                            f"[WARN] Could not remove original audio: {e}",
                        )

                    # Ensure related .info.json files are also removed
                    deleted = delete_infojson_for_src(mp3)
                    if deleted:
                        info_msg(
                            f"[INFO] {deleted} .info.json nach Timecode-Trim entfernt.",
                            f"[INFO] Removed {deleted} .info.json file(s) after timecode trimming.",
                        )

        # If timecodes were used, do not perform chapter splitting afterwards;
        # only download + timecode trimming is performed.
        if tc_spec is not None:
            ok_msg(
                "[FERTIG] Downloads + Timecode-Trimming abgeschlossen (kein Kapitel-Splitting).",
                "[DONE] Downloads + timecode trimming finished (no chapter splitting).",
            )
            return

        if args.download_only or args.download_video:
            ok_msg(
                "[FERTIG] Downloads abgeschlossen.",
                "[DONE] Downloads finished.",
            )
            return

        # Otherwise, use downloaded MP3s as sources for chapter splitting.
        sources = downloads

    else:
        # Split existing MP3s in the download directory
        sources = sorted(dl_dir.glob("*.mp3"))
        if not sources:
            print(
                tr(
                    "Keine MP3s im Download-Ordner gefunden.",
                    "No MP3 files found in the download directory.",
                )
            )
            sys.exit(1)

    # Split phase
    trash_queue: List[Path] = []
    defer_to_batch = args.trash_confirm_batch or args.trash_after_batch

    for src in sources:
        process_source(
            src,
            split_dir,
            trash_after=args.trash_after,
            ask_trash=args.trash_confirm,
            defer_to_batch=defer_to_batch,
            trash_queue=trash_queue,
            keep_infojson=args.keep_infojson,
        )

    # Batch cleanup
    if defer_to_batch and trash_queue:
        if send2trash is None:
            info_msg(
                "[HINWEIS] send2trash nicht installiert. Bitte 'pip install Send2Trash' ausführen, dann erneut versuchen.",
                "[INFO] 'send2trash' is not installed. Please run 'pip install Send2Trash' and try again.",
            )
        else:
            if args.trash_after_batch:
                for p in trash_queue:
                    send2trash(str(p))
                ok_msg(
                        f"[OK] {len(trash_queue)} Datei(en) in den Papierkorb verschoben (Batch-Auto).",
                        f"[OK] Moved {len(trash_queue)} file(s) to the recycle bin (batch auto).",
                )
            else:  # trash_confirm_batch
                prompt = tr(
                    f"Alle {len(trash_queue)} Quell-/CUE-Datei(en) in den Papierkorb schieben? [j/N]: ",
                    f"Move all {len(trash_queue)} source/CUE file(s) to the recycle bin? [y/N]: ",
                )
                ans = input(prompt).strip().lower()
                if ans in ("j", "y"):
                    for p in trash_queue:
                        send2trash(str(p))
                    ok_msg(
                            f"[OK] {len(trash_queue)} Datei(en) in den Papierkorb verschoben (Batch).",
                            f"[OK] Moved {len(trash_queue)} file(s) to the recycle bin (batch).",
                    )
                else:
                    info_msg(
                            "[SKIP] Quellen behalten.",
                            "[SKIP] Keeping source files.",
                    )

    ok_msg(
        "[DONE] Alles erledigt.",
        "[DONE] All tasks completed.",
    )


if __name__ == "__main__":
    main()