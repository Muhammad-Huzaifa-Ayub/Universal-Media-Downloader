#!/usr/bin/env python3
"""
vd_full.py - Robust universal media downloader (videos + images + gifs)

Features:
 - All downloaded videos (any container/format) are saved into VIDEOS_DIR.
 - All downloaded images are saved into IMAGES_DIR.
 - Interactive quality menu (keeps previous behavior).
 - Robust retries and fallbacks:
     * Multiple "player_client" extractor_args tried for YouTube Shorts/player-response issues.
     * curl or requests fallback for direct file downloads.
 - Uses ffprobe/ffmpeg when available to detect containers & fix malformed merges.
 - Detects extension-less outputs and renames them to the correct container.
 - Compact single-line download progress.
 - Summary printed once per successfully finalized file; no duplicate summaries.
 - Creates full verbose yt-dlp run logs only when download fails (for debugging).
 - JSONL and human-readable summary logging.
"""

import os
import re
import sys
import time
import json
import shutil
import logging
import random
import subprocess
from pathlib import Path
from urllib.parse import urlparse, urljoin
from typing import Optional, Tuple, List, Dict

# THIRD-PARTY (ensure installed)
try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from bs4 import BeautifulSoup
except Exception as e:
    print("Missing required packages. Install with:\n  pip install -U yt-dlp requests beautifulsoup4")
    raise

# yt-dlp
try:
    from yt_dlp import YoutubeDL
except Exception:
    print("yt-dlp not installed. Install with: pip install -U yt-dlp")
    raise

# Optional Pillow to get image resolution metadata (not required)
try:
    from PIL import Image
except Exception:
    Image = None

# Basic logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("vd_full")

# ------------------------------------
# Configuration (edit to change paths)
# ------------------------------------
VIDEOS_DIR = Path(r"D:\Huzaifa\Videos\video downloads").resolve()
IMAGES_DIR = Path(r"D:\Huzaifa\Pictures\picture downloads").resolve()
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

SUMMARY_JSONL = Path.cwd() / "vd_downloads.jsonl"
SUMMARY_TXT = Path.cwd() / "vd_downloads.txt"

# environment flags
FORCE_OVERWRITE = os.getenv("VD_FORCE_OVERWRITE", "") in ("1", "true", "True", "YES", "yes")

# UA rotation
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118 Safari/537.36"
]

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif")
VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".ts", ".m3u8", ".mpg", ".mpeg", ".flv", ".3gp")
USEFUL_IMAGE_PATTERNS = ("thumbnail", "thumb", "default", "jpeg", "jpg", "png", "media")

# ------------------------------------
# Helpers: requests session with retries
# ------------------------------------
def requests_session(total_retries=6, backoff_factor=1.0):
    s = requests.Session()
    retries = Retry(total=total_retries, backoff_factor=backoff_factor,
                    status_forcelist=[429, 500, 502, 503, 504], allowed_methods=frozenset(["GET","POST","HEAD","OPTIONS"]))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.headers.update({"User-Agent": random.choice(USER_AGENTS)})
    return s

_shared_session = requests_session()

def get_headers(referer: Optional[str]=None):
    h = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        h["Referer"] = referer
    return h

# ------------------------------------
# Simple text helpers
# ------------------------------------
def safe_filename_from_url(url: str) -> str:
    p = urlparse(url).path
    name = os.path.basename(p) or "file"
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    return name

def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    ext = path.suffix
    ts = time.strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{stem}_{ts}{ext}")

def fmt_size(num_bytes: Optional[int]) -> str:
    if not num_bytes:
        return "Unknown"
    mb = num_bytes / 1024.0 / 1024.0
    return f"{mb:.2f} MB"

def fmt_hms(seconds: Optional[float]) -> str:
    if seconds is None:
        return "Unknown"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s2 = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s2:02d}"
    return f"{m:02d}:{s2:02d}"

# ------------------------------------
# yt-dlp logger adapter (to avoid flooding)
# ------------------------------------
class YTDLPLogger:
    def __init__(self):
        self.warnings = []
        self.errors = []
    def debug(self, msg):
        log.debug("yt-dlp: %s", msg)
    def info(self, msg):
        log.info("yt-dlp: %s", msg)
    def warning(self, msg):
        try:
            m = str(msg).strip()
            if m:
                self.warnings.append(m)
        except Exception:
            pass
        log.debug("yt-dlp warning: %s", msg)
    def error(self, msg):
        try:
            m = str(msg).strip()
            if m:
                self.errors.append(m)
        except Exception:
            pass
        log.debug("yt-dlp error: %s", msg)

# ------------------------------------
# ffprobe probe helper (get duration/res/resolution)
# ------------------------------------
def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None

def probe_with_ffprobe(path: Path) -> Optional[dict]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    cmd = [ffprobe, "-v", "error", "-show_entries", "format:stream", "-print_format", "json", str(path)]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=20)
        data = json.loads(out)
        fmt = data.get("format", {})
        streams = data.get("streams", [])
        duration = float(fmt.get("duration")) if fmt.get("duration") else None
        width = None; height = None; has_audio = False
        for s in streams:
            if s.get("codec_type") == "video" and width is None:
                width = s.get("width")
                height = s.get("height")
            if s.get("codec_type") == "audio":
                has_audio = True
        return {"duration": duration, "width": width, "height": height, "has_audio": has_audio}
    except Exception as e:
        log.debug("ffprobe failed: %s", e)
        return None

# ------------------------------------
# Attempt to detect container by file header (if ffprobe not available)
# ------------------------------------
def detect_container_ext(path: Path) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            hdr = f.read(64)
        if b"ftyp" in hdr:
            return ".mp4"
        if hdr.startswith(b"\x1A\x45\xDF\xA3"):
            return ".webm"  # EBML
        if hdr.startswith(b"ID3"):
            return ".mp3"
        if hdr[:4] == b"RIFF" and b"WAVE" in hdr:
            return ".wav"
        # fallback: look for 'matroska' ascii somewhere
        if b"matroska" in hdr.lower():
            return ".mkv"
    except Exception:
        pass
    return None

# ------------------------------------
# Summary logging + single-print guard
# ------------------------------------
_PRINTED_SUMMARIES = set()

def append_summary_jsonl(entry: dict):
    try:
        SUMMARY_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with open(SUMMARY_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.debug("Failed to append JSONL summary: %s", e)

def append_summary_txt(entry: dict):
    try:
        need_header = not SUMMARY_TXT.exists()
        with open(SUMMARY_TXT, "a", encoding="utf-8") as f:
            if need_header:
                f.write("{:<19}  {:>8}  {:>9}  {:>8}  {:>11}  {:>5}  {}\n".format(
                    "Timestamp", "SizeMB", "Elapsed", "Duration", "Resolution", "Audio", "Path"))
                f.write("-" * 120 + "\n")
            size_mb = (entry.get("size_bytes") or 0) / 1024 / 1024
            elapsed = fmt_hms(entry.get("elapsed_seconds"))
            duration = fmt_hms(entry.get("media_duration_seconds"))
            res = entry.get("resolution") or {}
            res_str = f"{res.get('width') or '?'}x{res.get('height') or '?'}"
            audio = entry.get("has_audio") or "Unknown"
            f.write("{:<19}  {:8.2f}  {:>9}  {:>8}  {:>11}  {:>5}  {}\n".format(
                entry.get("timestamp") or time.strftime("%Y-%m-%d %H:%M:%S"),
                size_mb, elapsed, duration, res_str, audio, entry.get("path")))
            if entry.get("source_url"):
                f.write("    Source: " + entry.get("source_url") + "\n")
    except Exception as e:
        log.debug("Failed to append TXT summary: %s", e)

def print_and_log_summary(file_path: Path, info: Optional[dict]=None, elapsed_seconds: Optional[float]=None, source_url: Optional[str]=None):
    try:
        file_path = file_path.resolve()
    except Exception:
        file_path = Path(str(file_path))
    key = str(file_path)
    if key in _PRINTED_SUMMARIES:
        return
    if not file_path.exists():
        log.debug("print_and_log_summary: file does not exist: %s", file_path)
        return

    # probe
    size = file_path.stat().st_size
    probed = probe_with_ffprobe(file_path)
    if probed:
        duration = probed.get("duration")
        width = probed.get("width")
        height = probed.get("height")
        has_audio = "Yes" if probed.get("has_audio") else "No"
    else:
        duration = None
        width = None
        height = None
        has_audio = "Unknown"
        # If image
        if file_path.suffix.lower() in IMAGE_EXTS and Image is not None:
            try:
                with Image.open(file_path) as im:
                    width, height = im.size
                    has_audio = "No"
                    duration = None
            except Exception:
                pass

    elapsed = elapsed_seconds
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print("\nDownload Summary")
    print("----------------")
    print(f"Saved to : {file_path}")
    print(f"Size      : {fmt_size(size)}")
    print(f"Duration  : {fmt_hms(duration) if duration else 'Unknown'}")
    print(f"Total time taken: {fmt_hms(elapsed)}")
    print(f"Resolution: {height if height else 'Unknown'}p (width {width if width else 'Unknown'})")
    print(f"Audio     : {has_audio}")
    print("----------------\n")

    entry = {
        "timestamp": ts,
        "path": str(file_path),
        "size_bytes": size,
        "media_duration_seconds": duration,
        "resolution": {"width": width, "height": height},
        "has_audio": has_audio,
        "elapsed_seconds": elapsed,
        "source_url": source_url or (info.get("webpage_url") if info else None)
    }
    append_summary_jsonl(entry)
    append_summary_txt(entry)
    _PRINTED_SUMMARIES.add(key)

# ------------------------------------
# Progress hook for yt-dlp (compact single-line)
# ------------------------------------
_DOWNLOAD_STARTS = {}
_DOWNLOAD_ELAPSED = {}

def progress_hook(d: dict):
    try:
        status = d.get("status")
        filename = d.get("filename") or d.get("tmpfilename") or None
        key = filename or d.get("info_dict", {}).get("id") or d.get("url")
        if status == "downloading":
            if key not in _DOWNLOAD_STARTS:
                _DOWNLOAD_STARTS[key] = time.time()
            start = _DOWNLOAD_STARTS.get(key, time.time())
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed", 0) or 0
            eta = d.get("eta", None)
            elapsed = time.time() - start
            if total:
                print(f"\rDownloading: {downloaded/1024/1024:6.2f}/{total/1024/1024:6.2f} MB @ {speed/1024/1024:5.2f} MB/s ETA {fmt_hms(eta)} Elapsed {fmt_hms(elapsed)}", end="", flush=True)
            else:
                pct = d.get("_percent_str") or "?"
                print(f"\rDownloading: {pct} Elapsed {fmt_hms(elapsed)}", end="", flush=True)
        elif status == "finished":
            # yt-dlp signals when a file download finished (may be intermediate)
            fname = d.get("filename") or d.get("tmpfilename")
            start = None
            if key:
                start = _DOWNLOAD_STARTS.pop(key, None)
            elapsed = time.time() - start if start else None
            if fname and elapsed:
                _DOWNLOAD_ELAPSED[str(fname)] = elapsed
                prev = _DOWNLOAD_ELAPSED.get("__session__", 0.0) or 0.0
                _DOWNLOAD_ELAPSED["__session__"] = prev + elapsed
            # avoid printing the default 'Download finished:' which was noisy earlier
    except Exception:
        pass

# ------------------------------------
# Choose quality/menu (interactive)
# ------------------------------------
def choose_quality_menu(info: dict) -> str:
    formats = info.get("formats") or []
    # Filter out storyboards & tiny thumbnails
    def junk(f):
        fid = str(f.get("format_id") or "")
        if "storyboard" in fid or "thumbnail" in fid or f.get("filesize") and f.get("filesize") < 2000:
            return True
        return False

    video_audio = []
    video_only = []
    audio_only = []
    other = []
    idx_map = {}
    idx = 1
    for f in formats:
        if junk(f):
            continue
        has_video = bool(f.get("vcodec") and f.get("vcodec") != "none")
        has_audio = bool(f.get("acodec") and f.get("acodec") != "none")
        label = f.get("format_note") or f.get("format") or (f.get("height") and f"{f.get('height')}p") or f.get("ext")
        size = fmt_size(f.get("filesize") or f.get("filesize_approx"))
        fps = f.get("fps") or "?"
        entry = {"id": f.get("format_id"), "label": label, "size": size, "fps": fps, "has_video": has_video, "has_audio": has_audio, "orig": f}
        if has_video and has_audio:
            video_audio.append(entry)
        elif has_video and not has_audio:
            video_only.append(entry)
        elif has_audio and not has_video:
            audio_only.append(entry)
        else:
            other.append(entry)

    def print_section(title, items):
        nonlocal idx
        if not items:
            return
        print(f"\n{title}:")
        for it in items:
            print(f" {idx:2d}. {it['label']:<12} | Audio: {'Yes' if it['has_audio'] else 'No':3} | {it['size']:<8} | {it['fps']}fps")
            idx_map[idx] = it
            idx += 1

    print("\nAvailable formats:")
    print_section("Video + Audio (recommended)", video_audio)
    print_section("Video-only (will auto-add audio)", video_only)
    print_section("Audio-only (explicit - requires confirmation)", audio_only)
    print_section("Other formats", other)
    print("\nOptions:")
    print(" - Enter the number to choose that format.")
    print(" - Press Enter to select the default (BEST video+audio).")
    choice = input("\nEnter choice (number / Enter): ").strip()
    if not choice:
        return "bestvideo+bestaudio/best"
    try:
        n = int(choice)
    except Exception:
        print("Invalid input; defaulting to bestvideo+bestaudio/best")
        return "bestvideo+bestaudio/best"
    sel = idx_map.get(n)
    if not sel:
        print("Choice not found; defaulting to bestvideo+bestaudio/best")
        return "bestvideo+bestaudio/best"
    # If user selected video-only, offer audio selection
    if sel["has_video"] and not sel["has_audio"]:
        # gather audio-only options
        audio_entries = []
        for f in formats:
            if f.get("acodec") and f.get("acodec") != "none" and not (f.get("vcodec") and f.get("vcodec") != "none"):
                audio_entries.append((f.get("format_id"), f.get("format_note") or f.get("ext"), fmt_size(f.get("filesize") or f.get("filesize_approx"))))
        if audio_entries:
            print("\nMultiple audio tracks available. You may choose one to merge with the selected video.")
            print("Press Enter to use best audio automatically.")
            for i, a in enumerate(audio_entries, start=1):
                print(f" {i:2d}. {a[1]:<12} | {a[2]}")
            pick = input("\nEnter audio number to merge (Enter = best audio): ").strip()
            if not pick:
                return f"{sel['id']}+bestaudio/best"
            try:
                p = int(pick) - 1
                audio_id = audio_entries[p][0]
                return f"{sel['id']}+{audio_id}"
            except Exception:
                print("Invalid audio choice, using best audio.")
                return f"{sel['id']}+bestaudio/best"
        else:
            return f"{sel['id']}+bestaudio/best"
    # If selected audio-only, confirm
    if sel["has_audio"] and not sel["has_video"]:
        confirm = input(f"\nYou selected audio-only: {sel['label']} ({sel['size']}). Download audio only? (y/N): ").strip().lower()
        if confirm in ("y", "yes"):
            return sel["id"]
        print("Canceled audio-only choice; defaulting to bestvideo+bestaudio/best")
        return "bestvideo+bestaudio/best"
    return sel["id"]

# ------------------------------------
# Direct file download (requests streaming)
# ------------------------------------
def direct_stream_download(url: str, out_dir: Path, max_retries: int = 3) -> Optional[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_filename_from_url(url).split("?")[0]
    dest = out_dir / filename
    if dest.exists() and not FORCE_OVERWRITE:
        dest = unique_path(dest)
    headers = get_headers(url)
    session = _shared_session
    attempt = 0
    while attempt < max_retries:
        try:
            with session.get(url, stream=True, timeout=(10, 60), headers=headers) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0) or 0)
                start = time.time()
                downloaded = 0
                chunk_size = 1024 * 1024
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            elapsed = time.time() - start
                            speed = downloaded / (elapsed + 1e-6)
                            print(f"\rDownloading: {downloaded/1024/1024:6.2f}/{total/1024/1024:6.2f} MB @ {speed/1024/1024:5.2f} MB/s Elapsed {fmt_hms(elapsed)}", end="", flush=True)
                elapsed = time.time() - start
                _DOWNLOAD_ELAPSED[str(dest)] = elapsed
                print()
            return dest
        except KeyboardInterrupt:
            print("\nDownload canceled by user.")
            try: dest.unlink()
            except Exception: pass
            return None
        except Exception as e:
            log.debug("direct_stream_download attempt %s error: %s", attempt+1, e)
            attempt += 1
            time.sleep(1 + attempt)
    # as last resort try curl if available
    curl = shutil.which("curl")
    if curl:
        try:
            cmd = [curl, "-L", "-o", str(dest), url]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return dest
        except Exception:
            pass
    return None

# ------------------------------------
# Curl fallback downloader wrapper
# ------------------------------------
def curl_fallback(url: str, out_dir: Path) -> Optional[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / safe_filename_from_url(url)
    curl = shutil.which("curl")
    headers = get_headers(url)
    if curl:
        hargs = []
        for k, v in headers.items():
            hargs.extend(["-H", f"{k}: {v}"])
        cmd = [curl, "-L", "-f", "-o", str(dest), url] + hargs
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return dest
        except Exception as e:
            log.debug("curl fallback failed: %s", e)
    # fallback to requests
    try:
        return direct_stream_download(url, out_dir, max_retries=2)
    except Exception:
        return None

# ------------------------------------
# HTML scan to find media (videos & images)
# ------------------------------------
def scan_html_for_media(page_url: str) -> Tuple[List[str], List[str]]:
    try:
        hdr = get_headers(page_url)
        r = _shared_session.get(page_url, headers=hdr, timeout=12)
        r.raise_for_status()
        html = r.text
        soup = BeautifulSoup(html, "html.parser")
        vids = []
        imgs = []
        # <video> tags
        for v in soup.find_all("video"):
            src = v.get("src")
            if src:
                vids.append(urljoin(page_url, src))
            for s in v.find_all("source"):
                src2 = s.get("src")
                if src2:
                    vids.append(urljoin(page_url, src2))
        # direct links to known media files
        for a in soup.find_all("a", href=True):
            href = urljoin(page_url, a["href"])
            low = href.lower()
            if any(low.endswith(ext) for ext in VIDEO_EXTS) or re.search(r'\.(?:mp4|webm|m3u8|mkv|mov)(?:$|\?)', low):
                vids.append(href)
            if any(low.endswith(ext) for ext in IMAGE_EXTS):
                imgs.append(href)
        # <img> tags and srcset
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if src:
                imgs.append(urljoin(page_url, src))
            srcset = img.get("srcset")
            if srcset:
                parts = [p.strip() for p in srcset.split(",") if p.strip()]
                if parts:
                    for part in parts:
                        first = part.split()[0]
                        imgs.append(urljoin(page_url, first))
        # og:image
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            imgs.append(urljoin(page_url, og.get("content")))
        # JSON LD images
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                j = json.loads(script.string or "{}")
                if isinstance(j, dict) and j.get("image"):
                    if isinstance(j["image"], list):
                        for item in j["image"]:
                            imgs.append(urljoin(page_url, item))
                    else:
                        imgs.append(urljoin(page_url, j["image"]))
            except Exception:
                pass
        # find video file URLs by regex
        found = re.findall(r'https?://[^\s"\']+\.(?:mp4|m3u8|webm|mkv|mov)', html, flags=re.IGNORECASE)
        for f in found:
            vids.append(f)
        # de-duplicate preserving order
        def uniq(seq):
            seen = set()
            out = []
            for x in seq:
                if x and x not in seen:
                    seen.add(x)
                    out.append(x)
            return out
        return uniq(vids), uniq(imgs)
    except Exception as e:
        log.debug("scan_html_for_media failed: %s", e)
        return [], []

# ------------------------------------
# Cookie options helper (cookies.txt)
# ------------------------------------
def get_cookies_opts() -> dict:
    candidates = [Path.cwd() / "cookies.txt", Path.cwd() / "yt_cookies.txt"]
    for c in candidates:
        if c.exists():
            return {"cookiefile": str(c)}
    # also allow env override
    env = os.getenv("YDLP_COOKIEFILE") or os.getenv("YT_COOKIES")
    if env and Path(env).exists():
        return {"cookiefile": env}
    return {}

# ------------------------------------
# Check if URL supported by yt-dlp (probe)
# ------------------------------------
def is_supported_by_ytdlp(url: str, cookies_opts: dict = {}) -> Tuple[bool, Optional[dict]]:
    logger = YTDLPLogger()
    opts = {"quiet": True, "logger": logger}
    opts.update(cookies_opts or {})
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            extractor = info.get("extractor") or info.get("extractor_key") or info.get("ie_key")
            if extractor and "generic" in str(extractor).lower():
                return False, info
            if info.get("formats") or info.get("entries"):
                return True, info
            return False, info
    except Exception as e:
        log.debug("is_supported_by_ytdlp probe error: %s", e)
        return False, None

# ------------------------------------
# Verbose debug runner for yt-dlp (only on error)
# ------------------------------------
def create_verbose_log(url: str, reason: str = None) -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    logfile = Path.cwd() / f"yt_dlp_verbose_{ts}.log"
    cmd = [sys.executable, "-m", "yt_dlp", "-v", "-o", "%(title)s.%(id)s.%(ext)s", url]
    try:
        with open(logfile, "w", encoding="utf-8") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, check=False, timeout=240)
        log.debug("Wrote verbose yt-dlp run to %s (reason: %s)", logfile, reason)
    except Exception as e:
        log.debug("Failed to create verbose log: %s", e)
    return logfile

# ------------------------------------
# Core: download with yt-dlp, robust fallback & multiple player_client tries
# ------------------------------------
def download_video_with_yt_dlp(url: str, out_dir: Path, probe_info: Optional[dict]=None, batch_mode: bool=False) -> Optional[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cookies_opts = get_cookies_opts()
    logger = YTDLPLogger()

    # if direct media (non-youtube) with extension, use direct stream
    ulower = url.lower()
    if any(ulower.endswith(ext) for ext in (".mp4", ".webm", ".mkv", ".mov")) and ("youtube.com" not in ulower and "youtu.be" not in ulower):
        dest = direct_stream_download(url, out_dir=out_dir, max_retries=3)
        if dest:
            elapsed = _DOWNLOAD_ELAPSED.get(str(dest)) or _DOWNLOAD_ELAPSED.get(dest.name)
            print_and_log_summary(dest, {}, elapsed_seconds=elapsed, source_url=url)
            return dest

    # probe if needed
    info = probe_info
    if info is None:
        try:
            with YoutubeDL({"quiet": True, "logger": logger, **cookies_opts}) as ydl_probe:
                info = ydl_probe.extract_info(url, download=False)
        except Exception as e:
            log.debug("probe failed: %s", e)
            info = None

    # choose format if interactive
    fmt = None
    if info and info.get("formats") and not batch_mode:
        fmt = choose_quality_menu(info)
    else:
        fmt = "bestvideo+bestaudio/best"

    user_chosen_format = bool(fmt and fmt != "bestvideo+bestaudio/best" and not batch_mode)

    # out template - ensure downloads primarily go to out_dir
    out_template = "%(title)s.%(id)s.%(ext)s"

    base_opts = {
        "outtmpl": str(out_dir / out_template),
        "noplaylist": True,
        "merge_output_format": "mp4",
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
        "continuedl": True,
        "concurrent_fragment_downloads": 10,
        "http_chunk_size": 5 * 1024 * 1024,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "buffersize": 1024 * 512,
        "nooverwrites": (not FORCE_OVERWRITE) and (not user_chosen_format),
        "overwrites": FORCE_OVERWRITE or user_chosen_format,
        "logger": logger,
    }
    if fmt:
        base_opts["format"] = fmt
    base_opts.update(cookies_opts)

    # enhance headers / spoofing
    try:
        parsed = urlparse(url)
        referer = f"{parsed.scheme}://{parsed.netloc}"
        base_opts.setdefault("http_headers", {})
        for k,v in get_headers(referer).items():
            base_opts["http_headers"].setdefault(k, v)
    except Exception:
        referer = None

    # list of extractor_args player_client variants to try (None tries default)
    player_variants = [
        None,
        {"youtube": ["player_client=android"]},
        {"youtube": ["player_client=android_embed"]},
        {"youtube": ["player_client=web"]},
        {"youtube": ["player_client=safari"]},
        {"youtube": ["player_client=tv"]},
        {"youtube": ["player_client=default"]},
    ]

    last_exception = None
    for variant in player_variants:
        opts = dict(base_opts)
        if variant:
            opts["extractor_args"] = variant
        try:
            log.debug("Trying yt-dlp with extractor_args=%s", variant)
            start = time.time()
            info2 = None
            final_path = None

            with YoutubeDL(opts) as ydl:
                try:
                    info2 = ydl.extract_info(url, download=True)
                except Exception as e:
                    # capture and try next variant
                    last_exception = e
                    log.debug("yt-dlp threw while extracting (variant=%s): %s", variant, e)
                    raise e

                # try prepare_filename to get predicted path
                try:
                    predicted = None
                    try:
                        predicted = ydl.prepare_filename(info2)
                    except Exception:
                        predicted = None
                    if predicted:
                        cand = Path(predicted)
                        if cand.exists():
                            final_path = cand
                        else:
                            # try .mp4 candidate
                            mp4cand = cand.with_suffix(".mp4")
                            if mp4cand.exists():
                                final_path = mp4cand
                except Exception:
                    final_path = None

            elapsed = time.time() - start

            # If final_path not found, attempt to find most-recent candidate in out_dir
            if not final_path or not final_path.exists():
                candidates = []
                try:
                    threshold = time.time() - 900  # last 15 minutes
                    for f in out_dir.iterdir():
                        if not f.is_file():
                            continue
                        # skip tiny files
                        if f.stat().st_size < 512:
                            continue
                        if f.stat().st_mtime >= threshold:
                            candidates.append((f.stat().st_mtime, f))
                    if candidates:
                        candidates.sort(reverse=True)
                        # prefer file that contains video id
                        chosen = None
                        vid_id = (info2.get("id") if info2 else None) or (info.get("id") if info else None)
                        title = (info2.get("title") if info2 else None) or (info.get("title") if info else None)
                        for _, f in candidates:
                            if vid_id and vid_id in f.name:
                                chosen = f
                                break
                            if title and title.split()[0] in f.name:
                                chosen = f
                                break
                        if not chosen:
                            chosen = candidates[0][1]
                        final_path = chosen
                except Exception as e:
                    log.debug("candidate detection failed: %s", e)
                    final_path = None

            if final_path and final_path.exists():
                # If file is extensionless, try detect container
                if not final_path.suffix:
                    ext = detect_container_ext(final_path)
                    if ext:
                        dest = final_path.with_name(final_path.name + ext)
                        if dest.exists() and not FORCE_OVERWRITE:
                            dest = unique_path(dest)
                        try:
                            final_path.rename(dest)
                            final_path = dest
                        except Exception as e:
                            log.debug("rename extless failed: %s", e)
                # Ensure final file is moved to VIDEOS_DIR
                try:
                    if str(VIDEOS_DIR) not in str(final_path.parent):
                        dest = VIDEOS_DIR / final_path.name
                        if dest.exists() and not FORCE_OVERWRITE:
                            dest = unique_path(dest)
                        shutil.move(str(final_path), str(dest))
                        final_path = dest
                except Exception:
                    pass

                # print summary and return
                elapsed_final = _DOWNLOAD_ELAPSED.get("__session__") or _DOWNLOAD_ELAPSED.get(str(final_path)) or elapsed
                print_and_log_summary(final_path, info2 or {}, elapsed_seconds=elapsed_final, source_url=url)
                return final_path

            # If here => no valid final found; try next

            last_exception = RuntimeError("yt-dlp did not produce a final file")
        except Exception as e:
            last_exception = e
            # If known Shorts/player-response pattern, log & try next variant
            msg = str(e).lower()
            if "failed to extract any player response" in msg or "sabr" in msg or "extraction failed" in msg:
                log.debug("Detected player-response / SABR issue; trying next extractor variant. err=%s", e)
                time.sleep(0.4)
                continue
            # If connection reset like errors, re-raise to let top-level handle HTTP fallback
            if any(k in msg for k in ("connectionreset", "connection reset by peer", "read timed out", "timed out")):
                log.debug("Connection reset-like error: %s", e)
                raise
            log.debug("yt-dlp attempt failed with variant %s: %s", variant, e)
            time.sleep(0.5)
            continue

    # exhaustively tried all variants
    log.debug("All extractor variants exhausted; last_exception=%s", last_exception)
    # produce verbose log for debugging
    try:
        verbose_log = create_verbose_log(url, reason=str(last_exception))
        log.info("Created verbose debug log: %s", verbose_log)
    except Exception as e:
        log.debug("Failed to create verbose log: %s", e)
    return None

# ------------------------------------
# Image downloader (with retries and curl fallback)
# ------------------------------------
def is_useful_image(url: str) -> bool:
    low = url.lower()
    if any(p in low for p in USEFUL_IMAGE_PATTERNS):
        return True
    # Accept most images
    return True

def download_image(url: str, out_dir: Path, force: bool=False, max_retries: int=5) -> Optional[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if not force and not is_useful_image(url):
        return None
    dest = out_dir / safe_filename_from_url(url).split("?")[0]
    if dest.exists() and not FORCE_OVERWRITE:
        elapsed = _DOWNLOAD_ELAPSED.get(str(dest)) or None
        print(f"File already exists, skipping: {dest}")
        print_and_log_summary(dest, {}, elapsed_seconds=elapsed, source_url=url)
        return dest
    attempt = 0
    session = _shared_session
    hdrs = get_headers(url)
    while attempt < max_retries:
        try:
            with session.get(url, stream=True, timeout=30, headers=hdrs) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0) or 0)
                start = time.time()
                downloaded = 0
                chunk_size = 16*1024
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            elapsed = time.time()-start
                            speed = downloaded/(elapsed+1e-6)
                            print(f"\rDownloading: {downloaded/1024/1024:6.2f}/{total/1024/1024:6.2f} MB @ {speed/1024/1024:5.2f} MB/s Elapsed {fmt_hms(elapsed)}", end="", flush=True)
                elapsed = time.time()-start
                _DOWNLOAD_ELAPSED[str(dest)] = elapsed
                print()
            print(f"Saved image: {dest}")
            print_and_log_summary(dest, {}, elapsed_seconds=_DOWNLOAD_ELAPSED.get(str(dest)), source_url=url)
            return dest
        except KeyboardInterrupt:
            print("\nImage download canceled by user.")
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
            return None
        except Exception as e:
            log.debug("download_image attempt %s failed: %s", attempt+1, e)
            attempt += 1
            time.sleep(1 + attempt)
    # curl fallback
    fallback = curl_fallback(url, out_dir)
    if fallback:
        print_and_log_summary(fallback, {}, elapsed_seconds=_DOWNLOAD_ELAPSED.get(str(fallback)), source_url=url)
        return fallback
    log.debug("Failed to download image: %s", url)
    return None

# ------------------------------------
# Top-level handler for a given URL
# ------------------------------------
def handle_url(url: str, batch_mode: bool=False):
    url = url.strip()
    if not url:
        return
    print(f"\nProcessing: {url}")
    base = url.split("?")[0].lower()

    # direct image extensions
    if any(base.endswith(ext) for ext in IMAGE_EXTS):
        try:
            download_image(url, IMAGES_DIR, force=True)
            return
        except Exception as e:
            log.debug("direct image extension handling failed: %s", e)

    # HEAD check
    try:
        r = _shared_session.head(url, allow_redirects=True, timeout=10, headers=get_headers(url))
        ctype = r.headers.get("Content-Type", "").lower()
        if ctype.startswith("image/"):
            download_image(url, IMAGES_DIR, force=True)
            return
    except Exception:
        pass

    cookies_opts = get_cookies_opts()
    supported, probe = is_supported_by_ytdlp(url, cookies_opts=cookies_opts)
    if supported:
        # use robust wrapper (yt-dlp then fallbacks)
        try:
            res = download_video_with_yt_dlp(url, VIDEOS_DIR, probe_info=probe, batch_mode=batch_mode)
            if res:
                return
            # fallen through; attempt scan fallback
            log.debug("yt-dlp reported supported but did not yield final file; scanning page")
            vids, imgs = scan_html_for_media(url)
        except Exception as e:
            log.debug("download_video_with_yt_dlp exception: %s", e)
            # try HTTP fallback via curl
            try:
                f = curl_fallback(url, VIDEOS_DIR)
                if f:
                    print_and_log_summary(Path(f), {}, elapsed_seconds=_DOWNLOAD_ELAPSED.get(str(f)), source_url=url)
                    return
            except Exception:
                pass
            vids, imgs = scan_html_for_media(url)
    else:
        vids, imgs = scan_html_for_media(url)

    # handle discovered videos
    if vids:
        for v in vids:
            try:
                lv = v.split("?")[0].lower()
                # direct file
                if any(lv.endswith(ext) for ext in (".mp4", ".webm", ".mkv", ".mov", ".ts")):
                    dest = direct_stream_download(v, out_dir=VIDEOS_DIR, max_retries=2)
                    if dest:
                        print_and_log_summary(dest, {}, elapsed_seconds=_DOWNLOAD_ELAPSED.get(str(dest)), source_url=v)
                        continue
                # m3u8 -> yt-dlp usually handles well
                if ".m3u8" in lv or lv.endswith(".m3u8"):
                    download_video_with_yt_dlp(v, VIDEOS_DIR, probe_info=None, batch_mode=batch_mode)
                    continue
                # otherwise attempt yt-dlp
                sub_supported, sub_info = is_supported_by_ytdlp(v, cookies_opts=cookies_opts)
                if sub_supported:
                    download_video_with_yt_dlp(v, VIDEOS_DIR, probe_info=sub_info, batch_mode=batch_mode)
                else:
                    # try direct fallback for discovered link
                    f = direct_stream_download(v, VIDEOS_DIR, max_retries=2)
                    if f:
                        print_and_log_summary(f, {}, elapsed_seconds=_DOWNLOAD_ELAPSED.get(str(f)), source_url=v)
            except Exception as e:
                log.debug("error handling discovered video link %s: %s", v, e)
    # handle discovered images (gallery)
    if imgs:
        for img in imgs:
            try:
                download_image(img, IMAGES_DIR, force=False)
            except Exception as e:
                log.debug("error downloading discovered image %s: %s", img, e)
    if not vids and not imgs:
        print("No media found on the page.")

# ------------------------------------
# Input parsing helpers
# ------------------------------------
def expand_input_path(s: Optional[str]) -> str:
    if s is None:
        return ""
    s = s.strip()
    if s.lower() == "links":
        # keep your previous default plausible path
        candidate = Path.home() / "links.txt"
        return str(candidate)
    return s

def load_urls_from_input(user_input: str) -> List[str]:
    s = expand_input_path(user_input).strip().strip('"').strip("'")
    if not s:
        return []
    looks_like_path = ("\\" in s) or ("/" in s) or (re.match(r'^[a-zA-Z]:', s) is not None)
    p = Path(s)
    if looks_like_path or p.exists():
        if p.exists() and p.is_file():
            try:
                lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
                return lines
            except Exception:
                return [s]
        if p.exists() and p.is_dir():
            candidate = p / "links.txt"
            if candidate.exists():
                try:
                    lines = [ln.strip() for ln in candidate.read_text(encoding="utf-8").splitlines() if ln.strip()]
                    return lines
                except Exception:
                    return [s]
        return [s]
    parts = re.split(r'[\s,]+', s)
    return [p for p in parts if p]

# ------------------------------------
# Main
# ------------------------------------
def main():
    try:
        print("Universal media downloader (videos + images + gifs) - robust edition")
        print("You may paste multiple URLs separated by spaces/newlines, or enter path to a text file containing URLs.")
        print("Cookie handling: uses cookies.txt if present (explicit).")
        print(f"Videos will be saved to: {VIDEOS_DIR}")
        print(f"Images will be saved to: {IMAGES_DIR}")
        print("Set VD_FORCE_OVERWRITE=1 to force overwrites by default.")
        user_input = input("Enter URLs or path to file: ").strip()
        if not user_input:
            print("No input provided. Exiting.")
            return
        expanded = expand_input_path(user_input)
        p = Path(expanded.strip().strip('"').strip("'"))
        batch_mode = p.exists() and p.is_file()
        urls = load_urls_from_input(expanded)
        # extra split if single field has many
        if len(urls) == 1 and (" " in urls[0] or "\n" in urls[0]):
            parts = re.split(r'[\s,]+', urls[0])
            urls = [x for x in parts if x.strip()]
        if not urls:
            print("No URLs found. Exiting.")
            return
        for idx, u in enumerate(urls, start=1):
            print(f"\n[{idx}/{len(urls)}] Processing: {u}\n")
            try:
                handle_url(u, batch_mode=batch_mode)
            except KeyboardInterrupt:
                print("\nCanceled by user.")
                return
            except Exception as e:
                log.error("Error processing %s: %s", u, e)
                # create verbose log for debugging
                try:
                    v = create_verbose_log(u, reason=str(e))
                    log.info("Verbose debug log saved to %s", v)
                except Exception as ex:
                    log.debug("Failed to produce verbose debug log: %s", ex)
                continue
    except KeyboardInterrupt:
        print("\nCanceled by user.")

if __name__ == "__main__":
    main()
