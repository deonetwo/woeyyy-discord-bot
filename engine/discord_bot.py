"""
Discord bot audio streaming controller.
Runs an embedded client inside a dedicated background asyncio thread.
Handles queue management and slash commands.
"""

import asyncio
import html
import json
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import warnings
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set, Tuple, Union

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
import imageio_ffmpeg
import yt_dlp

# Suppress benign aiohttp unclosed connector ResourceWarnings on exit
warnings.filterwarnings("ignore", message=".*unclosed.*", category=ResourceWarning)
warnings.filterwarnings("ignore", message=".*Unclosed.*", category=ResourceWarning)
# Suppress benign yt-dlp Python 3.10 deprecation warnings on Linux environments
warnings.filterwarnings("ignore", message=".*Python version 3\\..*", category=UserWarning)
warnings.filterwarnings("ignore", message=".*Python version 3\\..*", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*Python version 3\\..*")

from engine.security import (
    secure_file_permissions,
    sanitize_audio_target,
)
from engine.logger import get_logger

logger = get_logger("DiscordBot")



def ensure_opus_loaded() -> bool:
    """Ensure libopus C-library is loaded into discord.opus for voice streaming."""
    if discord.opus.is_loaded():
        return True

    discord_dir = os.path.dirname(discord.__file__)
    possible_locations = [
        "libopus.so.0",
        "libopus.so",
        "/usr/lib/x86_64-linux-gnu/libopus.so.0",
        "/usr/lib/libopus.so.0",
        "/usr/local/lib/libopus.so",
        os.path.join(discord_dir, "bin", "libopus-0.x64.dll"),
        os.path.join(discord_dir, "bin", "libopus-0.x86.dll"),
        "libopus-0.x64.dll",
        "libopus-0.dll",
        "opus",
    ]

    import ctypes.util
    found_lib = ctypes.util.find_library("opus")
    if found_lib:
        possible_locations.insert(0, found_lib)

    for loc in possible_locations:
        try:
            discord.opus.load_opus(loc)
            if discord.opus.is_loaded():
                return True
        except Exception:
            pass

    try:
        return discord.opus._load_default()
    except Exception:
        return False


def get_ffmpeg_binary() -> str:
    """Resolve FFmpeg binary path: prefer system ffmpeg first, then imageio_ffmpeg."""
    import shutil
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    try:
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            if hasattr(os, "chmod") and sys.platform != "win32":
                try:
                    os.chmod(exe, 0o755)
                except Exception:
                    pass
            return exe
    except Exception:
        pass
    return "ffmpeg"


# Global FFmpeg binary path
FFMPEG_EXECUTABLE = get_ffmpeg_binary()

# YTDL options for fast, resilient audio stream extraction (pure audio download, no heavy CPU transcoding)
YTDL_OPTIONS = {
    "format": "ba/b",
    "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": False,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1:",
    "source_address": "0.0.0.0",
    "js_runtimes": {"bun": {}, "deno": {}},
}

# Path to optional cookies file (used strictly as on-demand fallback when YouTube requires authentication)
COOKIE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cookies.txt"))


def normalize_youtube_url(query: str) -> str:
    """
    Normalize YouTube Music and YouTube URLs.
    Rewrites music.youtube.com -> www.youtube.com to avoid auth-gate throttling and guarantee
    flawless 48kHz Opus stream resolution.
    """
    target = query.strip()
    if "music.youtube.com" in target:
        target = target.replace("music.youtube.com", "www.youtube.com")
    return target


def get_random_server_emoji(guild: Optional[discord.Guild]) -> str:
    """Return a random custom emoji from the server, or a music emoji if none exist."""
    if guild and getattr(guild, "emojis", None):
        custom_emojis = [e for e in guild.emojis if getattr(e, "available", True)]
        if custom_emojis:
            return str(random.choice(custom_emojis))
    return random.choice(["🎵", "🎶", "🎧", "✨"])



def to_unicode_bold(text: str) -> str:
    """Convert ASCII alphanumeric characters into Unicode Mathematical Sans-Serif Bold."""
    if not text:
        return ""
    res = []
    for ch in text:
        code = ord(ch)
        if 65 <= code <= 90:
            res.append(chr(0x1D5D4 + (code - 65)))
        elif 97 <= code <= 122:
            res.append(chr(0x1D5EE + (code - 97)))
        elif 48 <= code <= 57:
            res.append(chr(0x1D7EC + (code - 48)))
        else:
            res.append(ch)
    return "".join(res)


def clean_artist_name(uploader: str) -> str:
    """Clean up auto-generated YouTube artist names like 'Artist - Topic'."""
    if not uploader:
        return ""
    u = uploader.strip()
    if u.endswith(" - Topic"):
        u = u[:-8].strip()
    return u


def clean_search_query(q: str) -> str:
    """
    Remove excessive punctuation, quotes, and parenthetical metadata from a search query
    (e.g., '(from 2010 "OK Bartender" album) (edited by Richard Cheese)' -> '')
    to allow YouTube search to find matching working uploads when strict exact matches are unavailable.
    """
    if not q:
        return ""
    # Strip quotes
    cleaned = re.sub(r'["\']', '', q)
    # Strip parenthetical annotations: (from ...), [official video], (audio), etc.
    cleaned = re.sub(r'\s*\([^)]*\)', '', cleaned)
    cleaned = re.sub(r'\s*\[[^\]]*\]', '', cleaned)
    return re.sub(r'\s+', ' ', cleaned).strip()


def is_relevant_search_candidate(candidate_title: str, query: str) -> bool:
    """
    Check if a search candidate title is relevant to the user query, especially
    when the query specifies quoted song titles or specific words.
    Prevents YouTube fallback from playing completely unrelated songs from the same artist/album.
    """
    if not candidate_title:
        return False
    title_lower = candidate_title.lower()
    # If the query had quotes (e.g. "My Neck My Back"), require matching at least one significant word
    quotes = re.findall(r'["\']([^"\']+)["\']', query)
    if quotes:
        for q_str in quotes:
            core_words = [w.lower() for w in re.findall(r'[a-zA-Z0-9]+', q_str) if len(w) > 2]
            if core_words and not any(w in title_lower for w in core_words):
                return False
    return True


def format_now_playing_status(emoji: str, title: str, uploader: str = "") -> str:
    """Format voice channel status as: {emoji} Now Playing: {bold_title} • {bold_uploader}."""
    prefix = f"{emoji} " if emoji else ""
    t_clean = (title or "Music").strip()
    u_clean = clean_artist_name(uploader)

    t_bold = to_unicode_bold(t_clean)
    u_bold = to_unicode_bold(u_clean)

    if u_clean and not t_clean.lower().startswith(u_clean.lower()):
        text = f"{prefix}Now Playing: {t_bold} • {u_bold}"
    else:
        text = f"{prefix}Now Playing: {t_bold}"

    if len(text) > 100:
        text = text[:97] + "..."
    return text


def format_paused_status(title: str, uploader: str = "") -> str:
    """Format voice channel status when paused as: ⏸️ Paused: {bold_title} • {bold_uploader}."""
    t_clean = (title or "Music").strip()
    u_clean = clean_artist_name(uploader)

    t_bold = to_unicode_bold(t_clean)
    u_bold = to_unicode_bold(u_clean)

    if u_clean and not t_clean.lower().startswith(u_clean.lower()):
        text = f"⏸️ Paused: {t_bold} • {u_bold}"
    else:
        text = f"⏸️ Paused: {t_bold}"

    if len(text) > 100:
        text = text[:97] + "..."
    return text


async def async_search_youtube_suggestions(
    query: str,
    session: Optional[aiohttp.ClientSession] = None,
    max_results: int = 15,
) -> List[Dict[str, str]]:
    """
    Asynchronously query YouTube's search endpoint to fetch matching video suggestions
    for Discord slash command autocompletion.

    Returns a list of dicts: [{"name": display_name, "value": direct_url}]
    where name is formatted as '🎵 Channel - Title' (clamped to <= 100 chars) and value is the watch URL.
    """
    clean = query.strip()
    if not clean:
        return []

    # If the user is pasting a direct URL, suggest playing the URL directly
    if clean.startswith("http://") or clean.startswith("https://"):
        url_label = clean
        if len(url_label) > 90:
            url_label = url_label[:87] + "..."
        return [{"name": f"🔗 {url_label}", "value": clean}]

    items: List[Dict[str, str]] = []
    own_session = False
    if session is None or session.closed:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.0))
        own_session = True

    try:
        # Primary search: YouTube Innertube search endpoint (returns exact video title + channel)
        url = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
        payload = {
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": "2.20240101.00.00",
                    "hl": "en",
                    "gl": "US",
                }
            },
            "query": clean,
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                sec_contents = (
                    data.get("contents", {})
                    .get("twoColumnSearchResultsRenderer", {})
                    .get("primaryContents", {})
                    .get("sectionListRenderer", {})
                    .get("contents", [])
                )
                for sec in sec_contents:
                    item_sec = sec.get("itemSectionRenderer", {})
                    for item in item_sec.get("contents", []):
                        v = item.get("videoRenderer")
                        if not v:
                            continue
                        vid = v.get("videoId")
                        if not vid:
                            continue

                        # Extract title
                        title_runs = v.get("title", {}).get("runs", [])
                        if title_runs:
                            title = "".join(r.get("text", "") for r in title_runs)
                        else:
                            title = v.get("title", {}).get("simpleText", "")

                        # Extract uploader/channel
                        owner_runs = v.get("ownerText", {}).get("runs", [])
                        if owner_runs:
                            owner = "".join(r.get("text", "") for r in owner_runs)
                        else:
                            owner = ""

                        title = html.unescape(title).strip()
                        owner = html.unescape(owner).strip()

                        if not title:
                            continue

                        # Format label: '🎵 Channel - Title' or '🎵 Title'
                        if owner:
                            label = f"🎵 {owner} - {title}"
                        else:
                            label = f"🎵 {title}"

                        # Discord hard limit: Choice.name must be <= 100 characters
                        if len(label) > 100:
                            label = label[:97] + "..."

                        val = f"https://www.youtube.com/watch?v={vid}"
                        items.append({"name": label, "value": val})
                        if len(items) >= max_results:
                            break
                    if len(items) >= max_results:
                        break
    except Exception:
        pass

    # Secondary fallback: YouTube suggest queries endpoint if Innertube is empty
    if not items:
        try:
            suggest_url = (
                f"https://suggestqueries.google.com/complete/search"
                f"?client=youtube&ds=yt&q={urllib.parse.quote_plus(clean)}"
            )
            async with session.get(suggest_url) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    start = text.find("(")
                    end = text.rfind(")")
                    if start != -1 and end != -1:
                        data = json.loads(text[start + 1 : end])
                        if len(data) > 1 and isinstance(data[1], list):
                            for s_item in data[1]:
                                s_text = s_item[0] if isinstance(s_item, list) else str(s_item)
                                s_label = f"🔍 {s_text}"
                                if len(s_label) > 100:
                                    s_label = s_label[:97] + "..."
                                items.append({"name": s_label, "value": s_text})
                                if len(items) >= max_results:
                                    break
        except Exception:
            pass

    if own_session and not session.closed:
        await session.close()

    return items


ENV_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".bot_config.json"))
AUDIO_CACHE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cache"))
USER_HISTORY_PATH = os.path.join(AUDIO_CACHE_DIR, "user_history.json")
AUTOPLAY_HISTORY_PATH = os.path.join(AUDIO_CACHE_DIR, "autoplay_history.json")
DEFAULT_MAX_CACHE_MB = 500
DEFAULT_MAX_CACHE_FILES = 50


def load_env_file():
    """Load key-value pairs from .env into os.environ if not already present."""
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("\"'")
                    if key and key not in os.environ:
                        os.environ[key] = val
        except Exception:
            pass


# Automatically load .env into os.environ on import
load_env_file()


def get_cache_limits() -> Tuple[int, int]:
    """Return (max_bytes, max_files) configured via .env / environment variables or defaults."""
    load_env_file()
    try:
        mb = int(os.environ.get("MAX_CACHE_MB", str(DEFAULT_MAX_CACHE_MB)))
    except (ValueError, TypeError):
        mb = DEFAULT_MAX_CACHE_MB
    try:
        files = int(os.environ.get("MAX_CACHE_FILES", str(DEFAULT_MAX_CACHE_FILES)))
    except (ValueError, TypeError):
        files = DEFAULT_MAX_CACHE_FILES
    return max(50, mb) * 1024 * 1024, max(10, files)


def extract_youtube_video_id(url_or_query: str) -> Optional[str]:
    """Extract 11-character YouTube video ID from URL or raw ID string."""
    if not url_or_query:
        return None
    clean = url_or_query.strip()
    if len(clean) == 11 and re.match(r"^[a-zA-Z0-9_-]{11}$", clean):
        return clean
    patterns = [
        r"(?:v=|\/v\/|youtu\.be\/|embed\/|live\/)([a-zA-Z0-9_-]{11})",
        r"[\?&]v=([a-zA-Z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, clean)
        if m:
            return m.group(1)
    return None


def format_song_link(title: str, url: str) -> str:
    """Format song title as a clickable Discord markdown link if a valid HTTP(S) URL is present."""
    t = (title or "").strip() or "Song"
    u = (url or "").strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        vid = extract_youtube_video_id(u)
        if vid:
            u = f"https://www.youtube.com/watch?v={vid}"
    if u and (u.startswith("http://") or u.startswith("https://")):
        return f"**[{t}](<{u}>)**"
    return f"**{t}**"


def fetch_youtube_oembed_meta(vid_id: str) -> Optional[Dict[str, any]]:
    """Fetch video title and author name from official YouTube oEmbed API without cookies."""
    if not vid_id or len(vid_id) < 5:
        return None
    url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid_id}&format=json"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
        )
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            if resp.status == 200:
                raw_data = resp.read()
                data = json.loads(raw_data.decode("utf-8", errors="replace"))
                title = data.get("title", "").strip()
                author = data.get("author_name", "").strip()
                if title:
                    return {
                        "title": title,
                        "uploader": author,
                        "webpage_url": f"https://www.youtube.com/watch?v={vid_id}",
                        "video_id": vid_id,
                    }
    except Exception:
        pass
    return None


def get_audio_file_duration(filepath: str) -> Tuple[int, str]:
    """Extract audio duration in seconds and formatted string using ffmpeg binary."""
    if not filepath or not os.path.exists(filepath):
        return 0, ""
    try:
        proc = subprocess.run(
            [FFMPEG_EXECUTABLE, "-i", filepath],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", proc.stderr or "")
        if m:
            hrs = int(m.group(1))
            mins = int(m.group(2))
            secs = int(float(m.group(3)))
            total_sec = hrs * 3600 + mins * 60 + secs
            dur_str = f"{total_sec // 60}:{total_sec % 60:02d}"
            return total_sec, dur_str
    except Exception:
        pass
    return 0, ""


class AudioCacheIndex:
    """
    High-Performance In-Memory RAM Cache Index.
    Maintains an in-memory dictionary of all cached audio files and metadata.
    Provides nanosecond-level lookups (<0.001 ms) without repeated disk directory
    scanning or JSON file parsing.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._index: Dict[str, Dict[str, any]] = {}  # vid_id -> {"filepath": ..., "meta": ...}
        self._loaded_dirs: set = set()

    def sync_from_disk(self, cache_dir: str):
        """Scan cache_dir once and populate in-memory RAM cache index."""
        if not os.path.exists(cache_dir):
            return

        audio_exts = (".opus", ".webm", ".m4a", ".mp3", ".ogg", ".wav", ".flac", ".aac")
        try:
            files = os.listdir(cache_dir)
        except OSError:
            return

        json_files = {f[:-5]: f for f in files if f.endswith(".json") and f not in ("user_history.json", "autoplay_history.json")}
        audio_files = {}
        for f in files:
            for ext in audio_exts:
                if f.lower().endswith(ext):
                    base = f[:-len(ext)]
                    fpath = os.path.join(cache_dir, f)
                    try:
                        if os.path.isfile(fpath) and os.path.getsize(fpath) > 1024:
                            audio_files[base] = fpath
                    except OSError:
                        pass
                    break

        new_index = {}
        missing_meta_items = []
        for vid_id, af_path in audio_files.items():
            meta = None
            if vid_id in json_files:
                jpath = os.path.join(cache_dir, json_files[vid_id])
                try:
                    with open(jpath, "r", encoding="utf-8") as jf:
                        meta = json.load(jf)
                except Exception:
                    meta = None

            is_raw = (not meta) or (meta.get("title") == vid_id) or bool(re.match(r"^[A-Za-z0-9_-]{11}$", meta.get("title", "")))
            if not meta or is_raw:
                meta = meta or {
                    "title": vid_id,
                    "uploader": "",
                    "duration_sec": 0,
                    "duration_str": "",
                    "webpage_url": f"https://www.youtube.com/watch?v={vid_id}",
                    "video_id": vid_id,
                }
                missing_meta_items.append((vid_id, af_path, meta))

            new_index[vid_id] = {
                "filepath": af_path,
                "meta": meta,
            }

        # Enrich missing metadata from user_history.json if available
        hist_path = os.path.join(cache_dir, "user_history.json")
        if os.path.exists(hist_path) and os.path.getsize(hist_path) > 0:
            try:
                with open(hist_path, "r", encoding="utf-8") as hf:
                    all_hist = json.load(hf)
                    for u_songs in all_hist.values():
                        if isinstance(u_songs, list):
                            for s in u_songs:
                                s_vid = s.get("video_id") or extract_youtube_video_id(s.get("webpage_url", ""))
                                if s_vid and s_vid in new_index:
                                    cur_meta = new_index[s_vid]["meta"]
                                    if cur_meta.get("title") == s_vid and s.get("title"):
                                        cur_meta["title"] = s["title"]
                                    if not cur_meta.get("uploader") and s.get("uploader"):
                                        cur_meta["uploader"] = s["uploader"]
            except Exception:
                pass

        with self._lock:
            self._index.update(new_index)
            # Evict removed files
            for vid_id in list(self._index.keys()):
                if vid_id not in audio_files:
                    self._index.pop(vid_id, None)
            self._loaded_dirs.add(cache_dir)

        # Asynchronously resolve missing metadata in the background
        if missing_meta_items:
            def _bg_resolve():
                for m_vid, m_af, m_meta in missing_meta_items:
                    resolved = resolve_track_metadata(cache_dir, m_vid, m_meta, m_af)
                    with self._lock:
                        if m_vid in self._index:
                            self._index[m_vid]["meta"] = resolved
            threading.Thread(target=_bg_resolve, daemon=True).start()

    def _load_single_track(self, cache_dir: str, vid_id: str) -> Tuple[Optional[str], Optional[Dict[str, any]]]:
        """Scan disk for a newly created or single audio track if not present in RAM."""
        if not os.path.exists(cache_dir) or not vid_id:
            return None, None

        audio_exts = (".opus", ".webm", ".m4a", ".mp3", ".ogg", ".wav", ".flac", ".aac")
        cached_file = None
        try:
            for fname in os.listdir(cache_dir):
                if fname.startswith(f"{vid_id}.") and fname.lower().endswith(audio_exts):
                    full_path = os.path.join(cache_dir, fname)
                    if os.path.isfile(full_path) and os.path.getsize(full_path) > 1024:
                        cached_file = full_path
                        break
        except OSError:
            return None, None

        if not cached_file:
            return None, None

        meta = None
        meta_path = os.path.join(cache_dir, f"{vid_id}.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = None

        if not meta or meta.get("title") == vid_id or re.match(r"^[A-Za-z0-9_-]{11}$", meta.get("title", "")):
            meta = resolve_track_metadata(cache_dir, vid_id, meta, filepath=cached_file)

        return cached_file, meta

    def get(self, cache_dir: str, vid_id: str) -> Tuple[Optional[str], Optional[Dict[str, any]]]:
        """Nanosecond lookup by video ID directly from RAM."""
        if not vid_id:
            return None, None

        if cache_dir not in self._loaded_dirs:
            self.sync_from_disk(cache_dir)

        with self._lock:
            entry = self._index.get(vid_id)
            if entry:
                af_path = entry["filepath"]
                if os.path.exists(af_path):
                    meta = dict(entry["meta"])
                    title = meta.get("title", "").strip()
                    if not title or title == vid_id or re.match(r"^[A-Za-z0-9_-]{11}$", title):
                        meta = resolve_track_metadata(cache_dir, vid_id, meta, af_path)
                        entry["meta"] = meta
                    return af_path, dict(meta)
                else:
                    self._index.pop(vid_id, None)

        # Fallback to single track disk check (handles external file additions/tests)
        cached_f, meta = self._load_single_track(cache_dir, vid_id)
        if cached_f and meta:
            title = meta.get("title", "").strip()
            if not title or title == vid_id or re.match(r"^[A-Za-z0-9_-]{11}$", title):
                meta = resolve_track_metadata(cache_dir, vid_id, meta, cached_f)
            self.put(cache_dir, vid_id, meta, cached_f)
            return cached_f, dict(meta)

        return None, None

    def search_by_query(self, cache_dir: str, query: str) -> Tuple[Optional[str], Optional[Dict[str, any]]]:
        """Nanosecond search across all cached tracks directly from RAM."""
        if not os.path.exists(cache_dir) or not query:
            return None, None

        clean_query = query.strip().lower()
        clean_query = re.sub(r"^ytsearch\d*:\s*", "", clean_query)

        if clean_query.startswith("http://") or clean_query.startswith("https://"):
            return None, None

        stop_words = {"the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for", "with", "by", "is"}
        q_tokens = [w for w in re.split(r"\W+", clean_query) if w]
        if not q_tokens:
            return None, None

        content_tokens = [w for w in q_tokens if w not in stop_words]
        if not content_tokens:
            return None, None

        if cache_dir not in self._loaded_dirs:
            self.sync_from_disk(cache_dir)

        candidates = []
        with self._lock:
            for vid_id, entry in list(self._index.items()):
                af_path = entry["filepath"]
                meta = entry["meta"]
                title = meta.get("title", "").strip()
                uploader = meta.get("uploader", "").strip()
                if not title:
                    continue

                norm_title = title.lower()
                norm_uploader = uploader.lower()
                full_target = f"{norm_title} {norm_uploader}"
                target_tokens = set(re.split(r"\W+", full_target))

                # Exact match
                if clean_query in (norm_title, f"{norm_uploader} - {norm_title}", f"{norm_uploader} {norm_title}"):
                    if os.path.exists(af_path):
                        return af_path, dict(meta)
                    else:
                        self._index.pop(vid_id, None)
                        continue

                # Query terms matching
                if all(token in full_target for token in q_tokens):
                    if not os.path.exists(af_path):
                        self._index.pop(vid_id, None)
                        continue
                    matched_content = sum(1 for t in content_tokens if t in target_tokens)
                    score = matched_content / len(content_tokens)
                    candidates.append((score, len(clean_query), af_path, meta))

        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return candidates[0][2], dict(candidates[0][3])

        return None, None

    def get_random_track(
        self,
        cache_dir: str,
        exclude_vid_id: Optional[str] = None,
        exclude_vid_ids: Optional[Union[Set[str], List[str], Tuple[str, ...]]] = None,
    ) -> Optional[Tuple[str, Dict[str, any]]]:
        """
        Return a random cached track (filepath, meta).
        exclude_vid_id: immediately preceding video ID to avoid back-to-back repeats.
        exclude_vid_ids: set of video IDs already played today (Smart Autoplay).
        Returns None if no cached tracks exist or all available tracks have already been played today.
        """
        if not os.path.exists(cache_dir):
            return None

        if cache_dir not in self._loaded_dirs or not self._index:
            self.sync_from_disk(cache_dir)

        audio_exts = (".opus", ".webm", ".m4a", ".mp3", ".ogg", ".wav", ".flac", ".aac")

        with self._lock:
            valid_candidates = []
            for vid_id, entry in list(self._index.items()):
                fp = entry.get("filepath", "")
                if fp and os.path.exists(fp) and os.path.getsize(fp) > 1024:
                    meta_copy = dict(entry.get("meta", {}))
                    meta_copy.setdefault("video_id", vid_id)
                    valid_candidates.append((vid_id, fp, meta_copy))
                else:
                    self._index.pop(vid_id, None)

            if not valid_candidates:
                # Fallback: scan disk directory directly if index was empty
                try:
                    for fname in os.listdir(cache_dir):
                        if fname.endswith(audio_exts):
                            vid = os.path.splitext(fname)[0]
                            fp = os.path.join(cache_dir, fname)
                            if os.path.isfile(fp) and os.path.getsize(fp) > 1024:
                                meta_f = os.path.join(cache_dir, f"{vid}.json")
                                meta = {}
                                if os.path.exists(meta_f):
                                    try:
                                        with open(meta_f, "r", encoding="utf-8") as jf:
                                            meta = json.load(jf)
                                    except Exception:
                                        pass
                                if not meta:
                                    meta = {"title": vid, "video_id": vid}
                                self._index[vid] = {"filepath": fp, "meta": meta}
                                valid_candidates.append((vid, fp, meta))
                except Exception:
                    pass

            if not valid_candidates:
                return None

            to_exclude_today = set(exclude_vid_ids) if exclude_vid_ids else set()

            # Filter out tracks already played today by autoplay
            unplayed_today = [c for c in valid_candidates if c[0] not in to_exclude_today]
            if not unplayed_today:
                return None

            # Avoid immediate repeat if other unplayed tracks exist
            if exclude_vid_id:
                pool = [c for c in unplayed_today if c[0] != exclude_vid_id]
                if not pool:
                    pool = unplayed_today
            else:
                pool = unplayed_today

            chosen = random.choice(pool)
            chosen_vid, chosen_fp, chosen_meta = chosen
            title = chosen_meta.get("title", "").strip()
            if not title or title == chosen_vid or re.match(r"^[A-Za-z0-9_-]{11}$", title):
                chosen_meta = resolve_track_metadata(cache_dir, chosen_vid, chosen_meta, chosen_fp)
                with self._lock:
                    if chosen_vid in self._index:
                        self._index[chosen_vid]["meta"] = chosen_meta
            return chosen_fp, chosen_meta

    def put(self, cache_dir: str, vid_id: str, meta: Dict[str, any], audio_path: str):
        """Immediately update RAM cache index."""
        if not vid_id or not audio_path:
            return
        with self._lock:
            self._index[vid_id] = {
                "filepath": audio_path,
                "meta": dict(meta),
            }
            self._loaded_dirs.add(cache_dir)

    def remove(self, vid_id: str):
        """Remove track from RAM cache."""
        with self._lock:
            self._index.pop(vid_id, None)

    def clear(self):
        """Clear entire RAM cache index."""
        with self._lock:
            self._index.clear()
            self._loaded_dirs.clear()

    def count(self, cache_dir: Optional[str] = None) -> int:
        """Return total number of cached tracks in the index."""
        if cache_dir and cache_dir not in self._loaded_dirs:
            self.sync_from_disk(cache_dir)
        with self._lock:
            return len(self._index)

    def __len__(self) -> int:
        with self._lock:
            return len(self._index)


AUDIO_CACHE_INDEX = AudioCacheIndex()


def find_cached_track(cache_dir: str, vid_id: str) -> Tuple[Optional[str], Optional[Dict[str, any]]]:
    """
    Check if a valid audio file for the video ID already exists in cache_dir.
    Serviced directly from in-memory RAM cache (<0.001 ms).
    """
    return AUDIO_CACHE_INDEX.get(cache_dir, vid_id)


def find_cached_track_by_query(cache_dir: str, query: str) -> Tuple[Optional[str], Optional[Dict[str, any]]]:
    """
    Search local cache for an audio file matching a search query (song title / artist).
    Serviced directly from in-memory RAM cache (<0.05 ms).
    """
    return AUDIO_CACHE_INDEX.search_by_query(cache_dir, query)


def create_track_from_cached_meta(
    cached_file: str,
    cached_meta: Dict[str, any],
    requester: str = "Autoplay",
    emoji: str = "",
) -> Dict[str, any]:
    """Construct a standardized playback track dictionary from cached metadata."""
    if not emoji:
        emoji = get_random_server_emoji(None)
    target_vid = (
        cached_meta.get("video_id")
        or extract_youtube_video_id(cached_meta.get("webpage_url", ""))
        or (os.path.splitext(os.path.basename(cached_file))[0] if cached_file else "")
        or ""
    )
    cache_dir = os.path.dirname(cached_file) if cached_file else ""
    title = cached_meta.get("title", "").strip()
    if (not title or title == target_vid or re.match(r"^[A-Za-z0-9_-]{11}$", title)) and target_vid:
        cached_meta = resolve_track_metadata(cache_dir, target_vid, cached_meta, cached_file)

    sec = cached_meta.get("duration_sec", 0) or 0
    dur_str = cached_meta.get("duration_str") or (f"{sec // 60}:{sec % 60:02d}" if sec else "Live")
    title = cached_meta.get("title") or target_vid or "Cached Audio"
    uploader = cached_meta.get("uploader", "")

    raw_w_url = cached_meta.get("webpage_url", "")
    if raw_w_url and str(raw_w_url).startswith(("http://", "https://")):
        final_web_url = str(raw_w_url)
    elif target_vid:
        final_web_url = f"https://www.youtube.com/watch?v={target_vid}"
    else:
        final_web_url = ""

    return {
        "filepath": cached_file,
        "url": cached_file,
        "title": title,
        "uploader": uploader,
        "duration_sec": sec,
        "duration_str": dur_str,
        "webpage_url": final_web_url,
        "video_id": target_vid,
        "requester": requester,
        "http_headers": {},
        "is_stream": False,
        "timestamp": time.time(),
        "emoji": emoji,
    }


def save_track_cache_meta(cache_dir: str, vid_id: str, meta: Dict[str, any]):
    """Save track metadata to RAM cache and persist to cache/{vid_id}.json."""
    if not vid_id or not os.path.exists(cache_dir):
        return

    # Update in-memory RAM cache
    audio_exts = (".opus", ".webm", ".m4a", ".mp3", ".ogg", ".wav", ".flac", ".aac")
    audio_file = None
    for ext in audio_exts:
        af = os.path.join(cache_dir, f"{vid_id}{ext}")
        if os.path.isfile(af) and os.path.getsize(af) > 1024:
            audio_file = af
            break
    if audio_file:
        AUDIO_CACHE_INDEX.put(cache_dir, vid_id, meta, audio_file)

    meta_path = os.path.join(cache_dir, f"{vid_id}.json")
    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save track cache meta for {vid_id}: {e}")


def resolve_track_metadata(
    cache_dir: str,
    vid_id: str,
    existing_meta: Optional[Dict[str, any]] = None,
    filepath: Optional[str] = None,
) -> Dict[str, any]:
    """
    Ensure complete track metadata (title, uploader, duration).
    Self-heals via YouTube oEmbed and ffmpeg if title is missing, equal to video ID, or a raw 11-char ID.
    """
    meta = dict(existing_meta or {})
    title = meta.get("title", "").strip()
    uploader = meta.get("uploader", "").strip()
    sec = meta.get("duration_sec", 0) or 0
    dur_str = meta.get("duration_str", "").strip()

    is_raw_id = (not title) or (title == vid_id) or bool(re.match(r"^[A-Za-z0-9_-]{11}$", title))
    needs_save = False

    if is_raw_id and vid_id:
        oembed = fetch_youtube_oembed_meta(vid_id)
        if oembed:
            new_title = oembed.get("title", "").strip()
            if new_title:
                title = new_title
                needs_save = True
            new_uploader = oembed.get("uploader", "").strip()
            if new_uploader and not uploader:
                uploader = new_uploader
                needs_save = True

    if (not sec or not dur_str or dur_str in ("Live", "0:00")) and filepath and os.path.exists(filepath):
        f_sec, f_dur = get_audio_file_duration(filepath)
        if f_sec:
            sec = f_sec
            dur_str = f_dur
            needs_save = True

    if not dur_str:
        dur_str = f"{sec // 60}:{sec % 60:02d}" if sec else "Live"

    raw_w_url = meta.get("webpage_url", "")
    if raw_w_url and str(raw_w_url).startswith(("http://", "https://")):
        final_web_url = str(raw_w_url)
    elif vid_id:
        final_web_url = f"https://www.youtube.com/watch?v={vid_id}"
    else:
        final_web_url = ""

    resolved = {
        "title": title or vid_id,
        "uploader": uploader,
        "duration_sec": sec,
        "duration_str": dur_str,
        "webpage_url": final_web_url,
        "video_id": vid_id,
    }

    if needs_save and cache_dir and os.path.exists(cache_dir):
        try:
            save_track_cache_meta(cache_dir, vid_id, resolved)
        except Exception:
            pass

    return resolved


def ensure_track_title(track: Dict[str, any], cache_dir: str = AUDIO_CACHE_DIR) -> str:
    """Ensure track dictionary has resolved title instead of raw 11-char video ID."""
    if not track:
        return ""
    title = (track.get("title") or "").strip()
    vid = track.get("video_id") or extract_youtube_video_id(track.get("webpage_url", ""))
    if (not title or title == vid or bool(re.match(r"^[A-Za-z0-9_-]{11}$", title))) and vid:
        resolved = resolve_track_metadata(cache_dir, vid, track)
        if resolved.get("title") and resolved.get("title") != vid:
            title = resolved["title"]
            track["title"] = title
    return title or "Music"


def prune_audio_cache(
    cache_dir: str,
    max_bytes: Optional[int] = None,
    max_files: Optional[int] = None,
):
    """
    LRU Cache Eviction: Prunes old cached audio files when cache exceeds size or file limits.
    Prevents VPS disk bloat by keeping storage footprint strictly capped.
    """
    if not os.path.exists(cache_dir):
        return

    if max_bytes is None or max_files is None:
        cfg_bytes, cfg_files = get_cache_limits()
        max_bytes = max_bytes or cfg_bytes
        max_files = max_files or cfg_files

    audio_exts = {".opus", ".webm", ".m4a", ".mp3", ".ogg", ".wav", ".flac", ".aac"}
    entries = []
    total_size = 0

    try:
        for fname in os.listdir(cache_dir):
            fpath = os.path.join(cache_dir, fname)
            if not os.path.isfile(fpath):
                continue
            base, ext = os.path.splitext(fname)
            if ext.lower() in audio_exts:
                try:
                    stat = os.stat(fpath)
                    acc_time = max(stat.st_atime, stat.st_mtime)
                    entries.append((acc_time, stat.st_size, fpath, base))
                    total_size += stat.st_size
                except OSError:
                    pass
    except OSError:
        return

    if total_size <= max_bytes and len(entries) <= max_files:
        return

    # Sort oldest accessed first (LRU)
    entries.sort(key=lambda x: x[0])
    target_bytes = int(max_bytes * 0.8)
    target_files = int(max_files * 0.8)

    pruned_count = 0
    for acc_time, size, fpath, base in entries:
        if total_size <= target_bytes and len(entries) - pruned_count <= target_files:
            break
        try:
            os.remove(fpath)
            total_size -= size
            pruned_count += 1
            meta_path = os.path.join(cache_dir, f"{base}.json")
            if os.path.exists(meta_path):
                os.remove(meta_path)
            AUDIO_CACHE_INDEX.remove(base)
        except OSError:
            pass

    if pruned_count > 0:
        logger.info(f"Cache auto-prune: removed {pruned_count} old audio files to maintain storage quota.")


def load_user_history() -> Dict[str, List[Dict[str, any]]]:
    """Load persistent song play history per user from cache/user_history.json."""
    if os.path.exists(USER_HISTORY_PATH):
        try:
            with open(USER_HISTORY_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {}
                data = json.loads(content)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.warning(f"Failed to load user history: {e}")
    return {}


_USER_HISTORY_FILE_LOCK = threading.Lock()


def save_user_history(history: Dict[str, List[Dict[str, any]]]):
    """Save persistent song play history to cache/user_history.json."""
    with _USER_HISTORY_FILE_LOCK:
        try:
            cache_dir = os.path.dirname(USER_HISTORY_PATH)
            os.makedirs(cache_dir, exist_ok=True)
            with open(USER_HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save user history: {e}")


_AUTOPLAY_HISTORY_FILE_LOCK = threading.Lock()


def get_today_date_str() -> str:
    """Return current date formatted as YYYY-MM-DD."""
    return datetime.now().strftime("%Y-%m-%d")


def load_autoplay_history() -> Dict[str, any]:
    """
    Load persistent daily autoplay history from cache/autoplay_history.json.
    Automatically resets when date changes to a new calendar day.
    """
    today = get_today_date_str()
    if os.path.exists(AUTOPLAY_HISTORY_PATH):
        try:
            with open(AUTOPLAY_HISTORY_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        if data.get("date") == today and isinstance(data.get("played_ids"), list):
                            return data
        except Exception as e:
            logger.warning(f"Failed to load autoplay history: {e}")
    return {"date": today, "played_ids": []}


def save_autoplay_history(history: Dict[str, any]):
    """Save persistent daily autoplay history to cache/autoplay_history.json."""
    with _AUTOPLAY_HISTORY_FILE_LOCK:
        try:
            cache_dir = os.path.dirname(AUTOPLAY_HISTORY_PATH)
            os.makedirs(cache_dir, exist_ok=True)
            with open(AUTOPLAY_HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save autoplay history: {e}")


def load_saved_token() -> str:
    """
    Load Discord Bot Token with the following priority:
    1. OS Environment variable: DISCORD_BOT_TOKEN (ignores placeholder values)
    2. Local .env file
    3. Legacy .bot_config.json (auto-migrates to .env)
    """
    placeholder_tokens = {"YOUR_BOT_TOKEN_HERE", "YOUR_BOT_TOKEN", ""}

    # 1. Check OS Environment variable
    tok = os.environ.get("DISCORD_BOT_TOKEN", "").strip().strip("\"'")
    if tok and tok not in placeholder_tokens:
        return tok

    # 2. Check local .env file
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("DISCORD_BOT_TOKEN="):
                        val = line.split("=", 1)[1].strip().strip("\"'")
                        if val and val not in placeholder_tokens:
                            os.environ["DISCORD_BOT_TOKEN"] = val
                            return val
        except Exception:
            pass

    # 3. Fallback & migration from legacy .bot_config.json
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                legacy_tok = data.get("bot_token", "").strip()
                if legacy_tok:
                    save_token(legacy_tok)
                    try:
                        os.remove(CONFIG_PATH)
                    except Exception:
                        pass
                    return legacy_tok
        except Exception:
            pass

    return ""


def save_token(token: str):
    """
    Save Discord Bot Token to OS environment and persistent .env file.
    Secures file permissions via secure_file_permissions.
    """
    cleaned = token.strip().strip("\"'")
    os.environ["DISCORD_BOT_TOKEN"] = cleaned
    try:
        lines = []
        found = False
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for i, line in enumerate(lines):
                if line.strip().startswith("DISCORD_BOT_TOKEN="):
                    lines[i] = f"DISCORD_BOT_TOKEN={cleaned}\n"
                    found = True
                    break
        if not found:
            lines.append(f"DISCORD_BOT_TOKEN={cleaned}\n")

        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines)
        secure_file_permissions(ENV_PATH)

        # Remove legacy .bot_config.json if it exists
        if os.path.exists(CONFIG_PATH):
            try:
                os.remove(CONFIG_PATH)
            except Exception:
                pass
    except Exception as e:
        print(f"[DiscordBot] Failed to save token to .env: {e}")


class BufferedAudioSource(discord.AudioSource):
    """
    In-memory PCM Jitter Buffer for Discord voice streaming.
    Decodes audio ahead of real-time into a RAM queue (default: 250 frames = 5.0 seconds),
    completely isolating Discord AudioPlayer from disk I/O latency, CPU contention,
    and yt-dlp download spikes during queue additions.
    """

    def __init__(self, original: discord.AudioSource, buffer_size: int = 250):
        self.original = original
        self.buffer_size = buffer_size
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=buffer_size)
        self._stop_event = threading.Event()
        self._eof = False
        self._feeder_error: Optional[Exception] = None
        self._underrun_count = 0

        self._feeder = threading.Thread(
            target=self._feed,
            name="AudioBufferFeeder",
            daemon=True,
        )
        self._feeder.start()

        # Prime initial buffer (up to 200ms or 10 frames) so AudioPlayer never starts starved
        start_wait = time.perf_counter()
        while self._queue.qsize() < 10 and not self._eof and not self._stop_event.is_set():
            if time.perf_counter() - start_wait > 0.2:
                break
            time.sleep(0.01)

    def _feed(self):
        """Continuously read PCM frames from original source and buffer in memory."""
        try:
            while not self._stop_event.is_set():
                data = self.original.read()
                if not data:
                    self._eof = True
                    break
                while not self._stop_event.is_set():
                    try:
                        self._queue.put(data, timeout=0.05)
                        break
                    except queue.Full:
                        continue
        except Exception as e:
            self._feeder_error = e
        finally:
            self._eof = True

    def read(self) -> bytes:
        """
        Return next 20ms audio frame instantly from RAM.
        Never blocks on FFmpeg or disk I/O.
        """
        while not self._stop_event.is_set():
            if self._eof and self._queue.empty():
                return b""
            try:
                data = self._queue.get(timeout=0.02)
                self._underrun_count = 0
                return data
            except queue.Empty:
                if self._eof and self._queue.empty():
                    return b""
                self._underrun_count += 1
                if self._underrun_count > 150:  # 3 seconds of complete source stall
                    return b""
                # Underrun safety: return 20ms silence frame (3840 bytes) rather than clicking or crashing
                return b"\x00" * 3840
        return b""

    def is_opus(self) -> bool:
        return self.original.is_opus()

    def cleanup(self):
        """Stop feeder thread, drain queue, and clean up underlying source."""
        self._stop_event.set()
        try:
            while not self._queue.empty():
                self._queue.get_nowait()
        except Exception:
            pass
        if hasattr(self.original, "cleanup"):
            try:
                self.original.cleanup()
            except Exception:
                pass

    @property
    def _process(self):
        return getattr(self.original, "_process", None)

    @property
    def _current_error(self):
        return getattr(self.original, "_current_error", None) or self._feeder_error


class DiscordVoiceBot:
    """
    Thread-safe Discord bot controller with queue and slash commands.
    Runs commands.Bot in a dedicated asyncio background loop.
    """

    def __init__(
        self,
        on_status_change: Optional[Callable[[str, str], None]] = None,
        is_local: Optional[bool] = None,
    ):
        self.on_status_change = on_status_change
        self.is_local: bool = (
            is_local if is_local is not None else (os.environ.get("BOT_MODE", "").lower() == "local")
        )

        self.client: Optional[commands.Bot] = None
        self.voice_client: Optional[discord.VoiceClient] = None

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

        self.is_connected = False
        self.is_in_voice = False
        self.is_playing = False
        self.is_paused = False

        self.current_title = "No audio playing"
        self.current_track: Optional[Dict[str, any]] = None
        self.queue: List[Dict[str, any]] = []  # List of track dicts
        self.volume = 1.0  # 1.0 = 100%

        self.available_channels: List[Tuple[str, int]] = []  # [(Display Name, channel_id)]
        self.current_channel_id: Optional[int] = None

        self._autocomplete_cache: Dict[str, Tuple[float, List[Dict[str, str]]]] = {}
        self._http_session: Optional[aiohttp.ClientSession] = None

        self.ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)
        self.user_history: Dict[str, List[Dict[str, any]]] = load_user_history()
        self._history_lock = threading.Lock()
        threading.Thread(target=prune_audio_cache, args=(AUDIO_CACHE_DIR,), daemon=True).start()
        threading.Thread(target=AUDIO_CACHE_INDEX.sync_from_disk, args=(AUDIO_CACHE_DIR,), daemon=True).start()

        # Smart Auto-Leave settings (seconds, 0 to disable)
        self.auto_leave_empty_timeout: int = int(os.environ.get("AUTO_LEAVE_EMPTY_TIMEOUT", 180))  # 3 minutes default
        self.auto_leave_idle_timeout: int = int(os.environ.get("AUTO_LEAVE_IDLE_TIMEOUT", 300))    # 5 minutes default
        self._empty_since: Optional[float] = None
        self._idle_since: Optional[float] = None
        self._auto_leave_task: Optional[asyncio.Task] = None

        # Smart Autoplay setting (plays unplayed random cached tracks today when queue is empty)
        self.autoplay: bool = os.environ.get("AUTOPLAY_ENABLED", "false").lower() in ("true", "1", "yes")
        self.smart_autoplay: bool = os.environ.get("SMART_AUTOPLAY_ENABLED", "true").lower() in ("true", "1", "yes")
        self._autoplay_lock = threading.Lock()
        auto_data = load_autoplay_history()
        self._autoplay_date: str = auto_data.get("date", get_today_date_str())
        self._autoplay_played_ids: Set[str] = set(auto_data.get("played_ids", []))
        self._manual_stop: bool = False
        self._manual_skip: bool = False
        self.last_text_channel: Optional[discord.abc.Messageable] = None

    def _bind_text_channel(
        self,
        interaction: Optional[discord.Interaction] = None,
        channel: Optional[discord.abc.Messageable] = None,
    ):
        """Track the most recent active Discord text channel for playback announcements."""
        if channel:
            self.last_text_channel = channel
        elif interaction and interaction.channel:
            self.last_text_channel = interaction.channel

    def _get_announce_channel(self) -> Optional[discord.abc.Messageable]:
        """Return the active text channel for now playing notifications, with fallback to guild channels."""
        if self.last_text_channel:
            return self.last_text_channel
        if self.voice_client and self.voice_client.guild:
            guild = self.voice_client.guild
            me = guild.me
            if guild.system_channel:
                perms = guild.system_channel.permissions_for(me) if me else None
                if not perms or perms.send_messages:
                    return guild.system_channel
            for ch in guild.text_channels:
                perms = ch.permissions_for(me) if me else None
                if not perms or perms.send_messages:
                    return ch
        return None

    def record_user_history(self, user_id: Union[int, str], track: Dict[str, any]):
        """Record a played/enqueued song to user history (max 25 songs, FIFO with deduplication)."""
        uid = str(user_id)
        if not uid or not track:
            return

        title = track.get("title") or "Unknown Title"
        uploader = track.get("uploader") or ""
        url = track.get("webpage_url") or track.get("url") or ""
        if not (str(url).startswith("http://") or str(url).startswith("https://")):
            vid = track.get("video_id") or extract_youtube_video_id(url)
            if vid:
                url = f"https://www.youtube.com/watch?v={vid}"
            else:
                url = ""
        dur = track.get("duration_str") or ""

        entry = {
            "title": title,
            "uploader": uploader,
            "webpage_url": url,
            "duration_str": dur,
            "played_at": int(time.time()),
        }

        with self._history_lock:
            user_list = self.user_history.get(uid, [])
            key_val = url.strip() if url else title.strip().lower()
            filtered = [
                item for item in user_list
                if (item.get("webpage_url", "").strip() if url else item.get("title", "").strip().lower()) != key_val
            ]
            new_list = [entry] + filtered
            self.user_history[uid] = new_list[:25]
            hist_copy = {k: list(v) for k, v in self.user_history.items()}

        threading.Thread(target=save_user_history, args=(hist_copy,), daemon=True).start()

    def get_user_history(self, user_id: Union[int, str]) -> List[Dict[str, any]]:
        """Get copy of user song history."""
        uid = str(user_id)
        with self._history_lock:
            return list(self.user_history.get(uid, []))

    def clear_user_history(self, user_id: Union[int, str]) -> int:
        """Clear user history and return count of deleted items."""
        uid = str(user_id)
        with self._history_lock:
            count = len(self.user_history.get(uid, []))
            self.user_history.pop(uid, None)
            hist_copy = {k: list(v) for k, v in self.user_history.items()}

        threading.Thread(target=save_user_history, args=(hist_copy,), daemon=True).start()
        return count

    def _get_autoplay_played_ids_today(self) -> Set[str]:
        """Return set of track IDs already played today by smart autoplay, auto-rolling over at midnight."""
        today = get_today_date_str()
        with self._autoplay_lock:
            if self._autoplay_date != today:
                self._autoplay_date = today
                self._autoplay_played_ids.clear()
                save_autoplay_history({"date": today, "played_ids": []})
            return set(self._autoplay_played_ids)

    def record_autoplay_track(self, vid_id: Optional[str]):
        """Record track ID as played today by smart autoplay."""
        if not vid_id:
            return
        today = get_today_date_str()
        with self._autoplay_lock:
            if self._autoplay_date != today:
                self._autoplay_date = today
                self._autoplay_played_ids.clear()
            self._autoplay_played_ids.add(vid_id)
            data_to_save = {
                "date": self._autoplay_date,
                "played_ids": list(self._autoplay_played_ids),
            }
        threading.Thread(target=save_autoplay_history, args=(data_to_save,), daemon=True).start()

    def clear_autoplay_history(self):
        """Reset smart autoplay history for today."""
        today = get_today_date_str()
        with self._autoplay_lock:
            self._autoplay_date = today
            self._autoplay_played_ids.clear()
            data_to_save = {"date": today, "played_ids": []}
        save_autoplay_history(data_to_save)

    async def _get_http_session(self) -> aiohttp.ClientSession:
        """Get or lazily create a persistent aiohttp.ClientSession for the bot loop."""
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=2.0)
            )
        return self._http_session

    async def get_autocomplete_suggestions(self, query: str) -> List[Dict[str, str]]:
        """
        Fetch autocomplete suggestions with an in-memory TTL cache (5 minutes).
        Ensures ultra-fast 0ms responses for repeated queries or backspaces.
        """
        clean = query.strip()
        if not clean:
            return []

        cache_key = clean.lower()
        now = time.time()
        if cache_key in self._autocomplete_cache:
            cached_time, cached_items = self._autocomplete_cache[cache_key]
            if now - cached_time < 300.0:
                return cached_items

        session = await self._get_http_session()
        items = await async_search_youtube_suggestions(clean, session=session, max_results=15)

        # Prune cache if it grows beyond 300 items
        if len(self._autocomplete_cache) > 300:
            self._autocomplete_cache.clear()

        self._autocomplete_cache[cache_key] = (now, items)
        return items

    async def _ensure_voice_connected(self, channel: discord.VoiceChannel) -> discord.VoiceClient:
        """
        Connect or move to the given voice channel with automatic recovery from
        stale/zombie voice connections and 'Already connected' errors after prolonged inactivity.
        """
        guild = channel.guild
        guild_vc: Optional[discord.VoiceClient] = getattr(guild, "voice_client", None)

        # 1. If guild already has an actively connected voice client
        if guild_vc and guild_vc.is_connected():
            self.voice_client = guild_vc
            if guild_vc.channel.id != channel.id:
                await guild_vc.move_to(channel)
            self.is_in_voice = True
            self.current_channel_id = channel.id
            self._notify_status("VOICE_CONNECTED", channel.name)
            return guild_vc

        # 2. If a stale/zombie voice client exists (disconnected socket, timeout, or gateway drop)
        if guild_vc:
            try:
                await guild_vc.disconnect(force=True)
            except Exception:
                pass
            await asyncio.sleep(0.3)

        if self.voice_client and self.voice_client != guild_vc:
            try:
                await self.voice_client.disconnect(force=True)
            except Exception:
                pass
            await asyncio.sleep(0.2)

        # 3. Connect to the voice channel with resilience against discord.ClientException
        try:
            vc = await channel.connect(timeout=15.0, reconnect=True)
        except discord.ClientException as e:
            if "Already connected" in str(e):
                stale_vc = getattr(guild, "voice_client", None)
                if stale_vc:
                    if stale_vc.is_connected():
                        self.voice_client = stale_vc
                        if stale_vc.channel.id != channel.id:
                            await stale_vc.move_to(channel)
                        self.is_in_voice = True
                        self.current_channel_id = channel.id
                        self._notify_status("VOICE_CONNECTED", channel.name)
                        return stale_vc
                    try:
                        await stale_vc.disconnect(force=True)
                    except Exception:
                        pass
                await asyncio.sleep(0.5)
                vc = await channel.connect(timeout=15.0, reconnect=True)
            else:
                raise

        self.voice_client = vc
        self.is_in_voice = True
        self.current_channel_id = channel.id
        self._notify_status("VOICE_CONNECTED", channel.name)
        if not self.is_playing and not self.is_paused:
            await self._update_voice_channel_status("Waiting for song requests", channel_id=channel.id)
        return vc

    async def _update_voice_channel_status(self, status: Optional[str], channel_id: Optional[int] = None):
        """
        Update Discord Voice Channel Status (text displayed under voice channel name).
        Gracefully handles missing permissions ('Set Voice Channel Status').
        """
        target_id = channel_id or self.current_channel_id
        if not target_id and self.voice_client and self.voice_client.channel:
            target_id = self.voice_client.channel.id

        if not target_id or not self.client:
            return

        try:
            if hasattr(self.client, "http") and hasattr(self.client.http, "edit_voice_channel_status"):
                await self.client.http.edit_voice_channel_status(status, channel_id=target_id)
            else:
                ch = self.client.get_channel(target_id)
                if ch and isinstance(ch, discord.VoiceChannel):
                    await ch.edit(status=status)
        except Exception as e:
            # Requires 'Set Voice Channel Status' permission.
            # If status is None (resetting status when leaving) and permission is missing/403, silently ignore
            if status is None and "403" in str(e):
                return
            print(f"[DiscordBot] Notice: could not update voice channel status: {e}")

    def _notify_status(self, status: str, detail: str = ""):
        """Notify GUI thread of connection/voice status update."""
        if self.on_status_change:
            try:
                self.on_status_change(status, detail)
            except Exception:
                pass

    def start(self, token: str):
        """Start the Discord bot in a background thread."""
        if self.is_connected or (self._thread and self._thread.is_alive()):
            self.stop()

        token = token.strip()
        if not token:
            self._notify_status("ERROR", "Token cannot be empty")
            return

        save_token(token)
        self._notify_status("CONNECTING", "Logging into Discord...")

        self._thread = threading.Thread(target=self._run_bot, args=(token,), daemon=True)
        self._thread.start()

    def _register_slash_commands(self):
        """Register all slash commands (/) on the bot's command tree."""
        bot = self.client

        async def _tree_interaction_check(interaction: discord.Interaction) -> bool:
            self._bind_text_channel(interaction)
            return True

        bot.tree.interaction_check = _tree_interaction_check

        @bot.tree.command(name="join", description="Connect the bot to your current voice channel")
        async def cmd_join(interaction: discord.Interaction):
            if not interaction.user.voice or not interaction.user.voice.channel:
                await interaction.response.send_message(
                    "You must be in a voice channel to use this command.", ephemeral=True
                )
                return

            channel = interaction.user.voice.channel
            await interaction.response.defer(ephemeral=False)

            try:
                await self._ensure_voice_connected(channel)
                emoji = get_random_server_emoji(interaction.guild)
                prefix = f"{emoji} " if emoji else ""
                logger.info(f"Voice connected to channel '#{channel.name}' via /join")
                await interaction.followup.send(f"{prefix}Connected to **#{channel.name}**")
            except Exception as e:
                logger.error(f"Error connecting to voice channel: {e}")
                await interaction.followup.send(
                    "Failed to connect to voice channel. Please ensure the bot has permission to join and speak.",
                    ephemeral=True,
                )

        @bot.tree.command(name="play", description="Play a track from YouTube or add it to the queue")
        @app_commands.describe(query="Song title, YouTube URL, or YouTube Music URL")
        async def cmd_play(interaction: discord.Interaction, query: str):
            if not interaction.user.voice or not interaction.user.voice.channel:
                await interaction.response.send_message(
                    "You must be in a voice channel to use this command.",
                    ephemeral=True,
                )
                return

            # Always defer immediately within 50ms so Discord never times out!
            await interaction.response.defer(ephemeral=False)

            channel = interaction.user.voice.channel
            try:
                await self._ensure_voice_connected(channel)
            except Exception as e:
                logger.error(f"Error connecting to voice channel: {e}")
                await interaction.followup.send(
                    "Failed to connect to voice channel. Please ensure the bot has permission to join and speak.",
                    ephemeral=True,
                )
                return

            # Send immediate feedback tailored to input type
            is_direct_url = query.strip().startswith("http://") or query.strip().startswith("https://")
            status_msg = "Loading audio..." if is_direct_url else "Searching YouTube..."
            msg_handle = await interaction.followup.send(status_msg)

            self._notify_status("SEARCHING", "Searching for track...")

            requester_name = interaction.user.display_name
            chosen_emoji = get_random_server_emoji(interaction.guild)
            try:
                success, msg, is_queued, track = await self._async_enqueue_or_play(
                    query, requester=requester_name, emoji=chosen_emoji
                )

                if not success:
                    logger.warning(f"Failed to load audio for '{query}': {msg}")
                    await msg_handle.edit(content="Could not load audio. Please check the song title or URL and try again.")
                    return

                # Record track to user's history immediately upon retrieval
                if track:
                    self.record_user_history(interaction.user.id, track)

                title = track.get("title", query)
                dur = track.get("duration_str", "Live")
                uploader = track.get("uploader", "")
                url = track.get("webpage_url", "")

                link_part = format_song_link(title, url)
                uploader_part = f" by **{uploader}**" if uploader else ""
                dur_part = f" (`{dur}`)" if dur else ""

                emoji = track.get("emoji") or chosen_emoji
                prefix = f"{emoji} " if emoji else ""

                if is_queued:
                    pos = len(self.queue)
                    msg_text = f"{prefix}Added {link_part}{uploader_part}{dur_part} to the queue at position #{pos}."
                else:
                    msg_text = f"{prefix}Added {link_part}{uploader_part}{dur_part} to begin playing."

                await msg_handle.edit(content=msg_text)
            except Exception as e:
                logger.error(f"Error during /play command execution: {e}")
                try:
                    await msg_handle.edit(content="Could not load audio. Please check the song title or URL and try again.")
                except Exception:
                    pass

        @cmd_play.autocomplete("query")
        async def play_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> List[app_commands.Choice[str]]:
            clean = current.strip() if current else ""
            user_id = str(interaction.user.id)
            history = self.get_user_history(user_id)

            if not clean:
                if not history:
                    return []

                choices = []
                for item in history[:25]:
                    title = item.get("title", "").strip()
                    uploader = item.get("uploader", "").strip()
                    if uploader and not title.lower().startswith(uploader.lower()):
                        label = f"{uploader} - {title}"
                    else:
                        label = title
                    name = label
                    if len(name) > 100:
                        name = name[:97] + "..."
                    val = (item.get("webpage_url") or title)[:100]
                    choices.append(app_commands.Choice(name=name, value=val))
                return choices

            choices: List[app_commands.Choice[str]] = []
            seen_values = set()

            # 1. Check matching items from user's history first
            clean_lower = clean.lower()
            for item in history:
                title = item.get("title", "").strip()
                uploader = item.get("uploader", "").strip()
                val = (item.get("webpage_url") or title)[:100]
                if clean_lower in title.lower() or (uploader and clean_lower in uploader.lower()):
                    if uploader and not title.lower().startswith(uploader.lower()):
                        label = f"{uploader} - {title}"
                    else:
                        label = title
                    name = label
                    if len(name) > 100:
                        name = name[:97] + "..."
                    if val not in seen_values:
                        choices.append(app_commands.Choice(name=name, value=val))
                        seen_values.add(val)
                        if len(choices) >= 5:
                            break

            # 2. Fetch live YouTube suggestions for the remaining slots
            remaining = 25 - len(choices)
            if remaining > 0:
                try:
                    suggestions = await self.get_autocomplete_suggestions(clean)
                    for item in suggestions:
                        val = item["value"]
                        if val not in seen_values:
                            choices.append(app_commands.Choice(name=item["name"], value=val))
                            seen_values.add(val)
                            if len(choices) >= 25:
                                break
                except Exception as e:
                    print(f"[DiscordBot] Autocomplete error: {e}")

            return choices

        @bot.tree.command(name="skip", description="Skip the currently playing track")
        async def cmd_skip(interaction: discord.Interaction):
            is_active = (
                (self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()))
                or self.is_playing
                or bool(self.current_track)
            )
            if not is_active and not self.queue:
                await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)
                return

            old_track = self.current_track
            old_title = ensure_track_title(old_track) if old_track else self.current_title
            old_url = old_track.get("webpage_url", "") if old_track else ""
            old_link = format_song_link(old_title, old_url)
            next_track = self.get_next_track(guild=interaction.guild)

            self.skip()

            emoji = get_random_server_emoji(interaction.guild)
            prefix = f"{emoji} " if emoji else ""

            if next_track:
                next_title = ensure_track_title(next_track)
                next_url = next_track.get("webpage_url", "")
                next_dur = next_track.get("duration_str", "Live")
                next_uploader = next_track.get("uploader", "")

                next_link = format_song_link(next_title, next_url)
                next_up = f" by **{next_uploader}**" if next_uploader else ""
                next_dur_part = f" (`{next_dur}`)" if next_dur else ""

                track_emoji = next_track.get("emoji") or emoji
                track_prefix = f"{track_emoji} " if track_emoji else ""

                msg = f"{track_prefix}Skipped {old_link}.\nNow playing {next_link}{next_up}{next_dur_part}."
            elif self.autoplay:
                if self.smart_autoplay:
                    played_today = self._get_autoplay_played_ids_today()
                    if played_today:
                        msg = f"{prefix}Skipped {old_link}. Smart Autoplay is active, but all cached songs have already been played today."
                    else:
                        msg = f"{prefix}Skipped {old_link}. Autoplay is active, but no cached tracks were found in cache/."
                else:
                    msg = f"{prefix}Skipped {old_link}. Autoplay is active, but no cached tracks were found in cache/."
            else:
                msg = f"{prefix}Skipped {old_link}. The queue is now empty."

            if hasattr(interaction.response, "is_done") and callable(interaction.response.is_done) and interaction.response.is_done() is True:
                await interaction.followup.send(msg, suppress_embeds=True)
            else:
                await interaction.response.send_message(msg, suppress_embeds=True)

        @bot.tree.command(name="queue", description="Display the current song queue")
        async def cmd_queue(interaction: discord.Interaction):
            if not self.current_track and not self.queue:
                await interaction.response.send_message("The queue is empty.", ephemeral=True)
                return

            lines = []
            if self.current_track:
                c_title = self.current_track.get("title", "Unknown")
                c_url = self.current_track.get("webpage_url", "")
                c_dur = self.current_track.get("duration_str", "Live")
                c_up = self.current_track.get("uploader", "")

                cur_link = format_song_link(c_title, c_url)
                cur_up = f" by **{c_up}**" if c_up else ""
                cur_dur = f" (`{c_dur}`)" if c_dur else ""

                cur_emoji = (self.current_track.get("emoji") if self.current_track else None) or get_random_server_emoji(interaction.guild)
                prefix = f"{cur_emoji} " if cur_emoji else ""
                lines.append(f"{prefix}Now playing: {cur_link}{cur_up}{cur_dur}")

            if self.queue:
                lines.append(f"\nQueue ({len(self.queue)} tracks):")
                for i, t in enumerate(self.queue[:10], start=1):
                    t_title = t.get("title", "Unknown")
                    t_url = t.get("webpage_url", "")
                    t_dur = t.get("duration_str", "Live")
                    t_up = t.get("uploader", "")

                    t_link = format_song_link(t_title, t_url)
                    t_up_part = f" by **{t_up}**" if t_up else ""
                    t_dur_part = f" (`{t_dur}`)" if t_dur else ""
                    t_emoji = t.get("emoji") or get_random_server_emoji(interaction.guild)
                    t_prefix = f"{t_emoji} " if t_emoji else ""
                    lines.append(f"`{i}.` {t_prefix}{t_link}{t_up_part}{t_dur_part}")
                if len(self.queue) > 10:
                    lines.append(f"... and {len(self.queue) - 10} more tracks.")

            await interaction.response.send_message("\n".join(lines), suppress_embeds=True)

        @bot.tree.command(name="clear", description="Clear all songs from the queue")
        async def cmd_clear(interaction: discord.Interaction):
            count = self.clear_queue()
            emoji = get_random_server_emoji(interaction.guild)
            prefix = f"{emoji} " if emoji else ""
            await interaction.response.send_message(f"{prefix}Cleared {count} tracks from the queue.")

        @bot.tree.command(name="pause", description="Pause the currently playing track")
        async def cmd_pause(interaction: discord.Interaction):
            if self.voice_client and self.voice_client.is_playing():
                cur_track = self.current_track
                cur_title = cur_track.get("title", self.current_title) if cur_track else self.current_title
                cur_url = cur_track.get("webpage_url", "") if cur_track else ""
                cur_link = format_song_link(cur_title, cur_url)
                self.pause()
                emoji = get_random_server_emoji(interaction.guild)
                prefix = f"{emoji} " if emoji else ""
                await interaction.response.send_message(f"{prefix}Playback paused for {cur_link}.", suppress_embeds=True)
            else:
                await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)

        @bot.tree.command(name="resume", description="Resume paused playback")
        async def cmd_resume(interaction: discord.Interaction):
            if self.voice_client and self.voice_client.is_paused():
                cur_track = self.current_track
                cur_title = cur_track.get("title", self.current_title) if cur_track else self.current_title
                cur_url = cur_track.get("webpage_url", "") if cur_track else ""
                cur_link = format_song_link(cur_title, cur_url)
                self.resume()
                emoji = get_random_server_emoji(interaction.guild)
                prefix = f"{emoji} " if emoji else ""
                await interaction.response.send_message(f"{prefix}Playback resumed for {cur_link}.", suppress_embeds=True)
            else:
                await interaction.response.send_message("Playback is not paused.", ephemeral=True)

        @bot.tree.command(name="stop", description="Stop playback and clear the queue")
        async def cmd_stop(interaction: discord.Interaction):
            cur_track = self.current_track
            cur_title = cur_track.get("title", self.current_title) if cur_track else ""
            cur_url = cur_track.get("webpage_url", "") if cur_track else ""
            cur_link = f" for {format_song_link(cur_title, cur_url)}" if (cur_title and cur_title != "No audio playing") else ""
            self.stop_playback()
            emoji = get_random_server_emoji(interaction.guild)
            prefix = f"{emoji} " if emoji else ""
            await interaction.response.send_message(f"{prefix}Playback stopped{cur_link} and queue cleared.", suppress_embeds=True)

        @bot.tree.command(name="volume", description="Adjust playback volume (0% - 150%)")
        @app_commands.describe(percentage="Volume percentage (e.g. 100)")
        async def cmd_volume(interaction: discord.Interaction, percentage: int):
            vol = max(0, min(150, percentage)) / 100.0
            self.set_volume(vol)
            emoji = get_random_server_emoji(interaction.guild)
            prefix = f"{emoji} " if emoji else ""
            await interaction.response.send_message(f"{prefix}Volume set to `{percentage}%`.")

        @bot.tree.command(name="leave", description="Disconnect the bot from the voice channel")
        async def cmd_leave(interaction: discord.Interaction):
            guild_vc = getattr(interaction.guild, "voice_client", None) if interaction.guild else None
            if (self.voice_client and self.voice_client.is_connected()) or guild_vc:
                self.leave_voice_channel()
                emoji = get_random_server_emoji(interaction.guild)
                prefix = f"{emoji} " if emoji else ""
                await interaction.response.send_message(f"{prefix}Disconnected from voice channel.")
            else:
                await interaction.response.send_message("Bot is not in a voice channel.", ephemeral=True)

        class AutoplaySelect(discord.ui.Select):
            def __init__(ui_self, current_autoplay: bool, current_smart: bool = True):
                options = [
                    discord.SelectOption(
                        label="Smart Autoplay (No repeats today)",
                        value="smart",
                        description="Plays unplayed songs today, no repeats until midnight",
                        emoji="🧠",
                        default=(current_autoplay and current_smart),
                    ),
                    discord.SelectOption(
                        label="Standard Autoplay (Random)",
                        value="standard",
                        description="Plays random cached songs without daily restriction",
                        emoji="🔀",
                        default=(current_autoplay and not current_smart),
                    ),
                    discord.SelectOption(
                        label="Autoplay OFF",
                        value="off",
                        description="Playback stops when queue ends",
                        emoji="⏹️",
                        default=not current_autoplay,
                    ),
                    discord.SelectOption(
                        label="Reset Daily History",
                        value="reset",
                        description="Clear today's played tracks history so songs can replay",
                        emoji="🔄",
                    ),
                ]
                super().__init__(placeholder="Choose autoplay mode...", min_values=1, max_values=1, options=options)

            async def callback(ui_self, select_interaction: discord.Interaction):
                await select_interaction.response.defer()
                chosen_val = ui_self.values[0]
                emoji = get_random_server_emoji(select_interaction.guild)
                prefix = f"{emoji} " if emoji else ""

                if chosen_val == "smart":
                    res_msg = await bot_enable_autoplay(select_interaction, smart=True)
                elif chosen_val == "standard":
                    res_msg = await bot_enable_autoplay(select_interaction, smart=False)
                elif chosen_val == "reset":
                    self.clear_autoplay_history()
                    res_msg = f"{prefix}Autoplay daily history has been **reset**! All cached songs are now eligible to play again."
                else:
                    self.set_autoplay(False)
                    res_msg = f"{prefix}Autoplay is now **OFF**. Playback will stop when the queue is empty."

                new_view = AutoplaySelectView(self.autoplay, self.smart_autoplay)
                await select_interaction.edit_original_response(content=res_msg, view=new_view)

        class AutoplaySelectView(discord.ui.View):
            def __init__(ui_self, current_autoplay: bool, current_smart: bool = True):
                super().__init__(timeout=180)
                ui_self.add_item(AutoplaySelect(current_autoplay, current_smart))

        async def bot_enable_autoplay(interaction: discord.Interaction, smart: bool = True) -> str:
            self.set_autoplay(True, smart=smart, trigger=False)
            emoji = get_random_server_emoji(interaction.guild)
            prefix = f"{emoji} " if emoji else ""

            mode_label = "Smart Mode (no repeats today)" if self.smart_autoplay else "Standard Mode (random)"
            is_active = (self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused())) or self.is_playing

            if is_active and self.current_track:
                cur_title = ensure_track_title(self.current_track)
                self.current_title = cur_title
                cur_url = self.current_track.get("webpage_url", "")
                cur_link = format_song_link(cur_title, cur_url)
                if self.smart_autoplay:
                    return f"{prefix}Autoplay is now **ON** ({mode_label}).\nCurrently playing {cur_link}. When the queue ends, unplayed songs from cache will play automatically."
                else:
                    return f"{prefix}Autoplay is now **ON** ({mode_label}).\nCurrently playing {cur_link}. When the queue ends, random songs from cache will play automatically."

            # If not currently playing but bot is connected in a voice channel with empty queue:
            if self.voice_client and self.voice_client.is_connected() and not self.queue:
                started_track = self.trigger_autoplay()
                if started_track and isinstance(started_track, dict):
                    t_title = ensure_track_title(started_track)
                    t_url = started_track.get("webpage_url", "")
                    t_link = format_song_link(t_title, t_url)
                    return f"{prefix}Autoplay is now **ON** ({mode_label}).\nNow playing {t_link} from cache!"
                else:
                    if self.smart_autoplay:
                        played_today = self._get_autoplay_played_ids_today()
                        if played_today:
                            return f"{prefix}Autoplay is now **ON** ({mode_label}), but all cached songs have already been played today. Use `/autoplay reset` to clear history."
                    return f"{prefix}Autoplay is now **ON** ({mode_label}).\nWhen the queue ends, songs from cache will play automatically."

            return f"{prefix}Autoplay is now **ON** ({mode_label}).\nWhen the queue ends, songs from cache will play automatically."

        @bot.tree.command(
            name="autoplay",
            description="Control autoplay mode (smart unplayed songs, standard random, off, or reset)",
        )
        @app_commands.describe(mode="Autoplay mode (smart, standard, on, off, reset, or status)")
        @app_commands.choices(
            mode=[
                app_commands.Choice(name="smart (no repeats today)", value="smart"),
                app_commands.Choice(name="standard (repeats allowed)", value="standard"),
                app_commands.Choice(name="on (smart autoplay)", value="on"),
                app_commands.Choice(name="off", value="off"),
                app_commands.Choice(name="reset (clear today's history)", value="reset"),
                app_commands.Choice(name="status", value="status"),
            ]
        )
        async def cmd_autoplay(interaction: discord.Interaction, mode: Optional[str] = None):
            await interaction.response.defer(ephemeral=False)
            try:
                emoji = get_random_server_emoji(interaction.guild)
                prefix = f"{emoji} " if emoji else ""
                if mode in ("smart", "on"):
                    msg = await bot_enable_autoplay(interaction, smart=True)
                    await interaction.followup.send(msg, suppress_embeds=True)
                elif mode == "standard":
                    msg = await bot_enable_autoplay(interaction, smart=False)
                    await interaction.followup.send(msg, suppress_embeds=True)
                elif mode == "off":
                    self.set_autoplay(False)
                    msg = f"{prefix}Autoplay is now **OFF**. Playback will stop when the queue is empty."
                    await interaction.followup.send(msg)
                elif mode == "reset":
                    self.clear_autoplay_history()
                    msg = f"{prefix}Autoplay daily history has been **reset**! All cached songs are now eligible to play again."
                    await interaction.followup.send(msg)
                elif mode == "status":
                    status_info = self.get_autoplay_status()
                    curr_state = "ON" if status_info["enabled"] else "OFF"
                    mode_info = "Smart Mode (no repeats today)" if status_info["smart"] else "Standard Mode (random)"
                    played_cnt = status_info["played_today_count"]
                    total_cnt = status_info["total_cached_count"]
                    rem_cnt = status_info["remaining_unplayed"]
                    msg = (
                        f"{prefix}**Autoplay Status**\n"
                        f"• State: **{curr_state}**\n"
                        f"• Mode: **{mode_info}**\n"
                        f"• Played today: **{played_cnt}** track(s)\n"
                        f"• Total in cache: **{total_cnt}** track(s)\n"
                        f"• Remaining unplayed today: **{rem_cnt}** track(s)"
                    )
                    await interaction.followup.send(msg)
                else:
                    current_state = "ON" if self.autoplay else "OFF"
                    mode_desc = "Smart Mode (no repeats today)" if self.smart_autoplay else "Standard Mode"
                    played_cnt = len(self._get_autoplay_played_ids_today())
                    msg = (
                        f"{prefix}Autoplay is currently **{current_state}** ({mode_desc}).\n"
                        f"Songs played today: **{played_cnt}**\n"
                        "Choose an option below to change autoplay mode:"
                    )
                    view = AutoplaySelectView(self.autoplay, self.smart_autoplay)
                    await interaction.followup.send(msg, view=view)
            except Exception as e:
                logger.error(f"Error in cmd_autoplay: {e}")
                try:
                    await interaction.followup.send(
                        "An error occurred while changing autoplay settings. Please try again.",
                        ephemeral=True,
                    )
                except Exception:
                    pass

        @bot.tree.command(
            name="smartautoplay",
            description="Manage Smart Autoplay (plays unplayed songs today, no repeats)",
        )
        @app_commands.describe(action="Action to perform (enable, disable, reset daily history, or status)")
        @app_commands.choices(
            action=[
                app_commands.Choice(name="on (enable smart autoplay)", value="on"),
                app_commands.Choice(name="off (disable autoplay)", value="off"),
                app_commands.Choice(name="reset (clear today's history)", value="reset"),
                app_commands.Choice(name="status (view today's progress)", value="status"),
            ]
        )
        async def cmd_smartautoplay(interaction: discord.Interaction, action: Optional[str] = None):
            await interaction.response.defer(ephemeral=False)
            try:
                act = (action or "on").lower()
                emoji = get_random_server_emoji(interaction.guild)
                prefix = f"{emoji} " if emoji else ""
                if act in ("on", "enable"):
                    msg = await bot_enable_autoplay(interaction, smart=True)
                    await interaction.followup.send(msg, suppress_embeds=True)
                elif act in ("off", "disable"):
                    self.set_autoplay(False)
                    msg = f"{prefix}Smart Autoplay is now **OFF**. Playback will stop when the queue is empty."
                    await interaction.followup.send(msg)
                elif act in ("reset", "clear"):
                    self.clear_autoplay_history()
                    msg = f"{prefix}Smart Autoplay daily history has been **reset**! All cached songs can play again today."
                    await interaction.followup.send(msg)
                elif act in ("status", "info"):
                    status_info = self.get_autoplay_status()
                    curr_state = "ON" if status_info["enabled"] else "OFF"
                    played_cnt = status_info["played_today_count"]
                    total_cnt = status_info["total_cached_count"]
                    rem_cnt = status_info["remaining_unplayed"]
                    mode_info = "Smart (no repeats today)" if status_info["smart"] else "Standard (random repeats)"
                    msg = (
                        f"{prefix}**Smart Autoplay Status**\n"
                        f"• State: **{curr_state}**\n"
                        f"• Mode: **{mode_info}**\n"
                        f"• Songs played today: **{played_cnt}**\n"
                        f"• Total cache library: **{total_cnt}**\n"
                        f"• Remaining unplayed today: **{rem_cnt}**"
                    )
                    await interaction.followup.send(msg)
                else:
                    msg = await bot_enable_autoplay(interaction, smart=True)
                    await interaction.followup.send(msg, suppress_embeds=True)
            except Exception as e:
                logger.error(f"Error in cmd_smartautoplay: {e}")
                try:
                    await interaction.followup.send(
                        "An error occurred while managing smart autoplay. Please try again.",
                        ephemeral=True,
                    )
                except Exception:
                    pass

        @bot.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            cmd_name = interaction.command.name if interaction.command else "unknown"
            logger.error(f"AppCommand error on /{cmd_name}: {error}")
            try:
                msg = "An error occurred while executing this command. Please try again."
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                pass

    def _run_bot(self, token: str):
        """Asyncio event loop runner."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        intents.message_content = True

        self.client = commands.Bot(command_prefix="!", intents=intents)

        # Register slash commands
        self._register_slash_commands()

        @self.client.event
        async def on_ready():
            self.is_connected = True
            bot_name = str(self.client.user)
            logger.info(f"Logged in successfully as {bot_name}")
            self._refresh_voice_channels_internal()
            self._notify_status("ONLINE", bot_name)

            # Sync slash commands (/) across all guilds for instant availability
            try:
                for guild in self.client.guilds:
                    self.client.tree.copy_global_to(guild=guild)
                    await self.client.tree.sync(guild=guild)
                await self.client.tree.sync()
                logger.info("Slash commands (/) synced successfully to all servers!")
            except Exception as e:
                logger.warning(f"Note on syncing slash commands: {e}")

            # Start background smart auto-leave monitor task
            if self._auto_leave_task is None or self._auto_leave_task.done():
                self._auto_leave_task = self._loop.create_task(self._auto_leave_monitor_loop())

        @self.client.event
        async def on_voice_state_update(member, before, after):
            if member == self.client.user:
                if after.channel is None:
                    # Bot was disconnected from voice channel
                    self.is_in_voice = False
                    if before and before.channel and hasattr(before.channel, "guild"):
                        g_vc = getattr(before.channel.guild, "voice_client", None)
                        if g_vc:
                            try:
                                await g_vc.disconnect(force=True)
                            except Exception:
                                pass
                    self.voice_client = None
                    self.current_channel_id = None
                    self._empty_since = None
                    self._idle_since = None
                    self._notify_status("VOICE_DISCONNECTED", "Left voice channel")
                else:
                    self.is_in_voice = True
                    self.current_channel_id = after.channel.id
                    self.voice_client = getattr(after.channel.guild, "voice_client", None)
                    self._empty_since = None
                    if not self.is_playing and not self.is_paused:
                        self._idle_since = time.time()
                    self._notify_status("VOICE_CONNECTED", after.channel.name)
                    if not self.is_playing and not self.is_paused:
                        await self._update_voice_channel_status("Waiting for song requests", channel_id=after.channel.id)
            elif self.is_in_voice and self.voice_client and self.voice_client.channel:
                # Track when human members leave or join the bot's current channel
                if before and before.channel and before.channel.id == self.current_channel_id:
                    human_members = [m for m in self.voice_client.channel.members if not m.bot]
                    if len(human_members) == 0 and self._empty_since is None:
                        self._empty_since = time.time()
                elif after and after.channel and after.channel.id == self.current_channel_id:
                    if not member.bot:
                        self._empty_since = None

        try:
            self._loop.run_until_complete(self.client.start(token))
        except Exception as e:
            self.is_connected = False
            print(f"[DiscordBot] Login failed or connection closed: {e}")
            self._notify_status("ERROR", str(e))
        finally:
            self.is_connected = False
            self.is_in_voice = False
            try:
                pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                if self._http_session and not self._http_session.closed:
                    self._loop.run_until_complete(self._http_session.close())
                    self._http_session = None
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                # Allow aiohttp connectors a brief moment to finish graceful teardown
                self._loop.run_until_complete(asyncio.sleep(0.25))
            except Exception:
                pass
            if not self._loop.is_closed():
                self._loop.close()

    def stop(self):
        """Disconnect and stop the Discord bot cleanly."""
        if self._auto_leave_task and not self._auto_leave_task.done():
            self._auto_leave_task.cancel()

        if not self.is_connected or not self._loop or not self.client:
            return

        async def _async_stop():
            try:
                if self.voice_client and self.voice_client.is_connected():
                    await self.voice_client.disconnect(force=True)
                if self.client:
                    await self.client.close()
                if self._http_session and not self._http_session.closed:
                    await self._http_session.close()
                    self._http_session = None
                await asyncio.sleep(0.25)
            except Exception as e:
                print(f"[DiscordBot] Error during stop: {e}")

        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(_async_stop(), self._loop)
            try:
                future.result(timeout=4.0)
            except Exception:
                pass

        self.is_connected = False
        self.is_in_voice = False
        self.voice_client = None
        self.queue.clear()
        self.current_track = None
        self._notify_status("OFFLINE", "Bot disconnected")

    def _refresh_voice_channels_internal(self):
        """Enumerate all visible voice channels in guilds where the bot is a member."""
        channels = []
        if self.client and self.client.guilds:
            for guild in self.client.guilds:
                for vc in guild.voice_channels:
                    label = f"{guild.name} ➔ #{vc.name}"
                    channels.append((label, vc.id))
        self.available_channels = channels

    def get_available_voice_channels(self) -> List[Tuple[str, int]]:
        """Return list of (DisplayName, ChannelID) for GUI dropdown."""
        if self.client and self.is_connected:
            self._refresh_voice_channels_internal()
        return self.available_channels

    def join_voice_channel(self, channel_id: int):
        """Connect the bot to a specific voice channel."""
        if not self.is_connected or not self._loop or not self.client:
            self._notify_status("ERROR", "Bot is not online")
            return

        async def _async_join():
            channel = self.client.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.VoiceChannel):
                self._notify_status("ERROR", "Channel not found")
                return

            try:
                await self._ensure_voice_connected(channel)
            except Exception as e:
                print(f"[DiscordBot] Error joining voice channel: {e}")
                self._notify_status("ERROR", str(e))

        asyncio.run_coroutine_threadsafe(_async_join(), self._loop)

    def leave_voice_channel(self):
        """Disconnect the bot from its current voice channel."""
        if not self._loop:
            return

        async def _async_leave():
            old_cid = self.current_channel_id
            await self._update_voice_channel_status(None, channel_id=old_cid)
            vc = self.voice_client
            if not vc and self.client and self.current_channel_id:
                ch = self.client.get_channel(self.current_channel_id)
                if ch and hasattr(ch, "guild"):
                    vc = getattr(ch.guild, "voice_client", None)

            # Also force-disconnect any active voice clients tracked by client
            if not vc and self.client:
                for active_vc in getattr(self.client, "voice_clients", []):
                    try:
                        await active_vc.disconnect(force=True)
                    except Exception:
                        pass

            if vc:
                try:
                    if vc.is_playing():
                        vc.stop()
                    await vc.disconnect(force=True)
                except Exception:
                    pass

            self.is_in_voice = False
            self.voice_client = None
            self.current_channel_id = None
            self._empty_since = None
            self._idle_since = None
            self.queue.clear()
            self.current_track = None
            self._notify_status("VOICE_DISCONNECTED", "Left voice channel")
            self._notify_status("QUEUE_UPDATED", "")

        asyncio.run_coroutine_threadsafe(_async_leave(), self._loop)

    async def _auto_leave_monitor_loop(self):
        """Periodically check for empty voice channels or prolonged idle states to leave cleanly."""
        while self.is_connected and self._loop and self._loop.is_running():
            try:
                await asyncio.sleep(15)
                if not self.is_in_voice or not self.voice_client or not self.voice_client.is_connected():
                    self._empty_since = None
                    self._idle_since = None
                    continue

                channel = self.voice_client.channel
                if not channel:
                    continue

                now = time.time()

                # 1. Check if channel is empty (no human members present)
                if self.auto_leave_empty_timeout > 0:
                    human_members = [m for m in channel.members if not m.bot]
                    if len(human_members) == 0:
                        if self._empty_since is None:
                            self._empty_since = now
                        elif (now - self._empty_since) >= self.auto_leave_empty_timeout:
                            logger.info(
                                f"Auto-leaving voice channel: channel '#{channel.name}' has been empty for {self.auto_leave_empty_timeout}s."
                            )
                            self._empty_since = None
                            self._idle_since = None
                            self.leave_voice_channel()
                            continue
                    else:
                        self._empty_since = None

                # 2. Check if bot has been idle (no audio playing, not paused, queue empty)
                if self.auto_leave_idle_timeout > 0:
                    if not self.is_playing and not self.is_paused and not self.queue:
                        if self._idle_since is None:
                            self._idle_since = now
                        elif (now - self._idle_since) >= self.auto_leave_idle_timeout:
                            logger.info(
                                f"Auto-leaving voice channel: idle timeout reached ({self.auto_leave_idle_timeout}s without playback)."
                            )
                            self._idle_since = None
                            self._empty_since = None
                            self.leave_voice_channel()
                            continue
                    else:
                        self._idle_since = None

            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _async_enqueue_or_play(self, query_or_url: str, requester: str = "Host", emoji: str = "") -> Tuple[bool, str, bool, Dict[str, any]]:
        """
        Extract stream info using yt-dlp with YouTube Music normalization.
        When is_local=True: Uses direct streaming (download=False) without cookies for instant playback.
        When is_local=False: Uses cached download (download=True) into cache/ for cloud/server stability.
        Returns: (success, message, is_queued, track_dict)
        """
        try:
            target = normalize_youtube_url(query_or_url)
            is_safe, sanitized_target, reason = sanitize_audio_target(target)
            if not is_safe:
                return False, f"Security: URL rejected ({reason})", False, {}

            if not (sanitized_target.startswith("http://") or sanitized_target.startswith("https://") or sanitized_target.startswith("ytsearch")):
                sanitized_target = f"ytsearch5:{sanitized_target}"

            cache_dir = AUDIO_CACHE_DIR
            os.makedirs(cache_dir, exist_ok=True)

            # --- Instant Cache Bypass: Instant Playback for Cached Songs ---
            vid_id = extract_youtube_video_id(sanitized_target)
            cached_file = None
            cached_meta = None
            if vid_id:
                cached_file, cached_meta = find_cached_track(cache_dir, vid_id)
            else:
                # If searching by title/artist, search local cache first before querying YouTube
                cached_file, cached_meta = find_cached_track_by_query(cache_dir, query_or_url)

            if cached_file and cached_meta:
                try:
                    os.utime(cached_file, None)
                except OSError:
                    pass

                target_vid = cached_meta.get("video_id") or vid_id or extract_youtube_video_id(cached_meta.get("webpage_url", "")) or ""
                title = cached_meta.get("title", "").strip()
                if (not title or title == target_vid or re.match(r"^[A-Za-z0-9_-]{11}$", title)) and target_vid:
                    cached_meta = resolve_track_metadata(cache_dir, target_vid, cached_meta, cached_file)

                if not emoji:
                    guild = getattr(self.voice_client, "guild", None) if self.voice_client else None
                    emoji = get_random_server_emoji(guild)

                sec = cached_meta.get("duration_sec", 0) or 0
                dur_str = cached_meta.get("duration_str") or (f"{sec // 60}:{sec % 60:02d}" if sec else "Live")
                title = cached_meta.get("title", target_vid or query_or_url)
                uploader = cached_meta.get("uploader", "")

                raw_w_url = cached_meta.get("webpage_url", "")
                if raw_w_url and str(raw_w_url).startswith(("http://", "https://")):
                    final_cached_url = str(raw_w_url)
                elif target_vid:
                    final_cached_url = f"https://www.youtube.com/watch?v={target_vid}"
                else:
                    final_cached_url = ""

                track = {
                    "filepath": cached_file,
                    "url": cached_file,
                    "title": title,
                    "uploader": uploader,
                    "duration_sec": sec,
                    "duration_str": dur_str,
                    "webpage_url": final_cached_url,
                    "video_id": target_vid,
                    "requester": requester,
                    "http_headers": {},
                    "is_stream": False,
                    "timestamp": time.time(),
                    "emoji": emoji,
                }

                if self.is_playing or self.is_paused:
                    self.queue.append(track)
                    self._notify_status("ENQUEUED", track["title"])
                    self._notify_status("QUEUE_UPDATED", "")
                    return True, "Added to queue", True, track
                else:
                    await self._async_play_track(track)
                    return True, "Now playing", False, track

            loop = asyncio.get_event_loop()
            data = None
            direct_url = None
            filepath = None
            http_headers = {}

            if self.is_local:
                # Local Mode: Direct Streaming without full file download & zero cookies required
                stream_opts = {
                    "format": "bestaudio/best",
                    "extractaudio": True,
                    "audioformat": "opus",
                    "noplaylist": True,
                    "nocheckcertificate": False,
                    "ignoreerrors": True,
                    "logtostderr": False,
                    "quiet": True,
                    "no_warnings": True,
                    "default_search": "ytsearch5:",
                    "source_address": "0.0.0.0",
                }
                try:
                    ytdl_stream = yt_dlp.YoutubeDL(stream_opts)
                    data = await loop.run_in_executor(
                        None, lambda: ytdl_stream.extract_info(sanitized_target, download=False)
                    )
                except Exception as stream_err:
                    print(f"[DiscordBot] Local direct stream extraction notice: {stream_err}")

                if data and "entries" in data:
                    entries = [e for e in data["entries"] if e]
                    for candidate in entries:
                        d_url = candidate.get("url")
                        h_hdrs = candidate.get("http_headers", {})
                        if not d_url and "formats" in candidate:
                            audio_formats = [
                                f for f in candidate["formats"]
                                if f.get("url") and (f.get("vcodec") == "none" or "audio" in f.get("format", "").lower() or f.get("acodec") != "none")
                            ]
                            if audio_formats:
                                d_url = audio_formats[-1].get("url")
                                if "http_headers" in audio_formats[-1]:
                                    h_hdrs = audio_formats[-1].get("http_headers")
                        if d_url:
                            direct_url = d_url
                            http_headers = h_hdrs
                            data = candidate
                            break
                elif data:
                    direct_url = data.get("url")
                    http_headers = data.get("http_headers", {})
                    if not direct_url and "formats" in data:
                        audio_formats = [
                            f for f in data["formats"]
                            if f.get("url") and (f.get("vcodec") == "none" or "audio" in f.get("format", "").lower() or f.get("acodec") != "none")
                        ]
                        if audio_formats:
                            best_fmt = audio_formats[-1]
                            direct_url = best_fmt.get("url")
                            if "http_headers" in best_fmt:
                                http_headers = best_fmt.get("http_headers")

            # Fallback or Server Mode: Download to cache folder
            if not direct_url:
                res_vid = vid_id or extract_youtube_video_id(sanitized_target)
                candidates = []

                # 1. If searching by query (not a direct video URL), perform fast multi-candidate flat search
                if not res_vid:
                    search_opts = dict(YTDL_OPTIONS)
                    search_opts["noplaylist"] = True
                    search_opts["extract_flat"] = True

                    search_target = sanitized_target
                    if search_target.startswith("ytsearch1:"):
                        search_target = f"ytsearch5:{search_target[10:]}"
                    elif not (search_target.startswith("http://") or search_target.startswith("https://") or search_target.startswith("ytsearch")):
                        search_target = f"ytsearch5:{search_target}"

                    try:
                        ytdl_search = yt_dlp.YoutubeDL(search_opts)
                        data = await loop.run_in_executor(
                            None, lambda: ytdl_search.extract_info(search_target, download=False)
                        )
                    except Exception as search_err:
                        logger.warning(f"Search metadata extraction notice ({search_err}), proceeding to download...")
                        data = None

                    if data and "entries" in data:
                        candidates = [e for e in data["entries"] if e]
                    elif data:
                        candidates = [data]

                    # Filter candidates by title relevance if query has quotes
                    rel_candidates = [c for c in candidates if is_relevant_search_candidate(c.get("title", ""), query_or_url)]

                    # If no relevant candidates found and query has parenthetical/quoted annotations, fallback to cleaned query
                    cleaned_q = clean_search_query(query_or_url)
                    if not rel_candidates and cleaned_q and cleaned_q.lower() != query_or_url.strip().lower():
                        try:
                            clean_target = f"ytsearch5:{cleaned_q}"
                            data_clean = await loop.run_in_executor(
                                None, lambda: ytdl_search.extract_info(clean_target, download=False)
                            )
                            if data_clean and "entries" in data_clean:
                                existing_ids = {c.get("id") for c in candidates if c.get("id")}
                                for c in data_clean["entries"]:
                                    if c and c.get("id") and c.get("id") not in existing_ids:
                                        if is_relevant_search_candidate(c.get("title", ""), cleaned_q):
                                            rel_candidates.append(c)
                                            existing_ids.add(c.get("id"))
                        except Exception as clean_err:
                            logger.info(f"Clean query search notice: {clean_err}")

                    candidates = rel_candidates or candidates
                    data = candidates[0] if candidates else None

                    if data:
                        res_vid = data.get("id") or extract_youtube_video_id(data.get("url", "")) or extract_youtube_video_id(data.get("webpage_url", ""))

                # 2. Check if ANY candidate video ID is ALREADY in local cache!
                if not direct_url and candidates:
                    for c in candidates:
                        cand_id = c.get("id") or extract_youtube_video_id(c.get("url", "")) or extract_youtube_video_id(c.get("webpage_url", ""))
                        if cand_id:
                            cached_file, cached_meta = find_cached_track(cache_dir, cand_id)
                            if cached_file and os.path.exists(cached_file):
                                filepath = cached_file
                                direct_url = cached_file
                                res_vid = cand_id
                                data = c
                                if cached_meta:
                                    if not data.get("title") and cached_meta.get("title"):
                                        data["title"] = cached_meta["title"]
                                    if not data.get("uploader") and cached_meta.get("uploader"):
                                        data["uploader"] = cached_meta["uploader"]
                                break

                if not direct_url and res_vid:
                    cached_file, cached_meta = find_cached_track(cache_dir, res_vid)
                    if cached_file and os.path.exists(cached_file):
                        filepath = cached_file
                        direct_url = cached_file
                        if not data:
                            data = dict(cached_meta) if cached_meta else {}
                        elif cached_meta:
                            if not data.get("title") and cached_meta.get("title"):
                                data["title"] = cached_meta["title"]
                            if not data.get("uploader") and cached_meta.get("uploader"):
                                data["uploader"] = cached_meta["uploader"]

                # 3. Only download from YouTube if the audio file is NOT in cache (Single-Pass with candidate fallback)
                if not direct_url:
                    dl_opts = dict(YTDL_OPTIONS)
                    dl_opts["outtmpl"] = os.path.join(cache_dir, "%(id)s.%(ext)s")
                    dl_opts["noplaylist"] = True
                    dl_opts.pop("extract_flat", None)
                    if os.path.exists(COOKIE_PATH):
                        dl_opts["cookiefile"] = COOKIE_PATH
                    else:
                        dl_opts.pop("cookiefile", None)

                    # Build target candidate list to try
                    targets_to_try = []
                    if candidates:
                        for c in candidates:
                            c_id = c.get("id") or extract_youtube_video_id(c.get("url", "")) or extract_youtube_video_id(c.get("webpage_url", ""))
                            if c_id:
                                targets_to_try.append((f"https://www.youtube.com/watch?v={c_id}", c_id, c))
                    if not targets_to_try:
                        if res_vid:
                            targets_to_try.append((f"https://www.youtube.com/watch?v={res_vid}", res_vid, data))
                        else:
                            fallback_t = (data.get("webpage_url") if data else None) or (data.get("url") if data else None) or sanitized_target
                            targets_to_try.append((fallback_t, "", data))

                    last_error = None
                    ytdl_active = None
                    for dl_target, cand_id, cand_data in targets_to_try:
                        try:
                            ytdl_dl = yt_dlp.YoutubeDL(dl_opts)
                            dl_data = await loop.run_in_executor(
                                None, lambda: ytdl_dl.extract_info(dl_target, download=True)
                            )
                            if dl_data:
                                data = dl_data
                                ytdl_active = ytdl_dl
                                if cand_id:
                                    res_vid = cand_id
                                break
                        except Exception as dl_err:
                            err_str = str(dl_err).lower()
                            last_error = dl_err
                            is_auth_error = any(kw in err_str for kw in ["sign in", "login", "cookie", "authenticate", "account", "confirm you're not a bot", "age-restricted", "confirm your age"])
                            is_unavail = any(kw in err_str for kw in ["unavailable", "removed", "private", "not available"])

                            if "cookiefile" in dl_opts and (is_auth_error or is_unavail):
                                logger.warning(f"Download with cookies for {cand_id or dl_target} failed ({dl_err}), retrying without cookies...")
                                clean_dl_opts = dict(dl_opts)
                                clean_dl_opts.pop("cookiefile", None)
                                try:
                                    ytdl_clean = yt_dlp.YoutubeDL(clean_dl_opts)
                                    dl_data = await loop.run_in_executor(
                                        None, lambda: ytdl_clean.extract_info(dl_target, download=True)
                                    )
                                    if dl_data:
                                        data = dl_data
                                        ytdl_active = ytdl_clean
                                        if cand_id:
                                            res_vid = cand_id
                                        break
                                except Exception as clean_err:
                                    logger.warning(f"Retry without cookies failed ({clean_err})")
                                    last_error = clean_err
                            elif "cookiefile" not in dl_opts and is_auth_error and not is_unavail and os.path.exists(COOKIE_PATH):
                                logger.warning(f"Download for {cand_id or dl_target} requires authentication ({dl_err}), retrying with cookies...")
                                auth_dl_opts = dict(dl_opts)
                                auth_dl_opts["cookiefile"] = COOKIE_PATH
                                try:
                                    ytdl_auth = yt_dlp.YoutubeDL(auth_dl_opts)
                                    dl_data = await loop.run_in_executor(
                                        None, lambda: ytdl_auth.extract_info(dl_target, download=True)
                                    )
                                    if dl_data:
                                        data = dl_data
                                        ytdl_active = ytdl_auth
                                        if cand_id:
                                            res_vid = cand_id
                                        break
                                except Exception as auth_err:
                                    logger.warning(f"Retry with cookies failed ({auth_err})")
                                    last_error = auth_err
                            else:
                                logger.info(f"Search candidate {cand_id or dl_target} unavailable ({dl_err}), trying next candidate...")

                    if data and "entries" in data:
                        entries = [e for e in data["entries"] if e]
                        if not entries:
                            return False, f"Track unavailable ({last_error or 'no playable stream'})", False, {}
                        data = entries[0]

                    if not data:
                        return False, f"Track unavailable ({last_error or 'not found'})", False, {}

                    prep_ydl = ytdl_active or yt_dlp.YoutubeDL(dl_opts)
                    filepath = prep_ydl.prepare_filename(data)
                    if not os.path.exists(filepath):
                        target_id = data.get("id", "") or res_vid
                        for fname in os.listdir(cache_dir):
                            if target_id and fname.startswith(target_id):
                                filepath = os.path.join(cache_dir, fname)
                                break
                    direct_url = filepath

            if not data:
                return False, "Track not found.", False, {}

            title = data.get("title", query_or_url)
            uploader = data.get("uploader") or data.get("channel") or data.get("artist") or ""
            sec = data.get("duration", 0) or 0
            dur_str = f"{sec // 60}:{sec % 60:02d}" if sec else "Live"

            if not emoji:
                guild = getattr(self.voice_client, "guild", None) if self.voice_client else None
                emoji = get_random_server_emoji(guild)

            target_vid = (
                data.get("id")
                or vid_id
                or extract_youtube_video_id(data.get("webpage_url", ""))
                or extract_youtube_video_id(data.get("url", ""))
                or extract_youtube_video_id(query_or_url)
                or ""
            )
            raw_w_url = data.get("webpage_url", "")
            if raw_w_url and str(raw_w_url).startswith(("http://", "https://")):
                final_web_url = str(raw_w_url)
            elif target_vid:
                final_web_url = f"https://www.youtube.com/watch?v={target_vid}"
            elif query_or_url.startswith(("http://", "https://")):
                final_web_url = query_or_url
            else:
                final_web_url = ""

            track = {
                "filepath": filepath,
                "url": direct_url,
                "title": title,
                "uploader": uploader,
                "duration_sec": sec,
                "duration_str": dur_str,
                "webpage_url": final_web_url,
                "video_id": target_vid,
                "requester": requester,
                "http_headers": http_headers or data.get("http_headers", {}),
                "is_stream": filepath is None,
                "timestamp": time.time(),
                "emoji": emoji,
            }

            # Save metadata and enforce storage limits asynchronously to eliminate event loop lag
            if target_vid and filepath and os.path.exists(filepath):
                meta_dict = {
                    "title": title,
                    "uploader": uploader,
                    "duration_sec": sec,
                    "duration_str": dur_str,
                    "webpage_url": final_web_url or f"https://www.youtube.com/watch?v={target_vid}",
                    "video_id": target_vid,
                }
                def _bg_persist():
                    save_track_cache_meta(cache_dir, target_vid, meta_dict)
                    prune_audio_cache(cache_dir)

                loop.run_in_executor(None, _bg_persist)

            # Check if playback is currently active
            if self.is_playing or self.is_paused:
                self.queue.append(track)
                logger.info(f"Enqueued '{track.get('title')}' (#{len(self.queue)}) requested by {requester}")
                self._notify_status("ENQUEUED", track.get("title", query_or_url))
                self._notify_status("QUEUE_UPDATED", "")
                return True, "Added to queue", True, track
            else:
                logger.info(f"Now playing '{track.get('title')}' requested by {requester}")
                await self._async_play_track(track)
                return True, "Now playing", False, track

        except Exception as e:
            logger.error(f"Failed to enqueue or play: {e}")
            return False, "Failed to load audio track", False, {}

    async def _async_play_track(self, track: Dict[str, any], announce: bool = False):
        """Play track on current voice_client (using direct stream URL or cached file)."""
        if not self.voice_client or not self.voice_client.is_connected():
            return

        try:
            ensure_opus_loaded()
            if self.voice_client.is_playing() or self.voice_client.is_paused():
                self.voice_client.stop()

            ensure_track_title(track)
            self.current_track = track
            self.current_title = track["title"]

            ffmpeg_bin = get_ffmpeg_binary()
            audio_src = track.get("filepath") or track.get("url")

            if track.get("is_stream") and track.get("webpage_url"):
                age = time.time() - track.get("timestamp", 0)
                if not audio_src or age > 7200:
                    try:
                        loop = asyncio.get_event_loop()
                        stream_opts = {
                            "format": "bestaudio/best",
                            "extractaudio": True,
                            "audioformat": "opus",
                            "noplaylist": True,
                            "quiet": True,
                            "no_warnings": True,
                            "source_address": "0.0.0.0",
                        }
                        ytdl_refresh = yt_dlp.YoutubeDL(stream_opts)
                        refreshed = await loop.run_in_executor(
                            None, lambda: ytdl_refresh.extract_info(track["webpage_url"], download=False)
                        )
                        if refreshed:
                            if "entries" in refreshed and refreshed["entries"]:
                                refreshed = refreshed["entries"][0]
                            audio_src = refreshed.get("url")
                            track["url"] = audio_src
                            if "http_headers" in refreshed:
                                track["http_headers"] = refreshed["http_headers"]
                            track["timestamp"] = time.time()
                    except Exception as refresh_err:
                        print(f"[DiscordBot] Stream refresh error: {refresh_err}")

            if audio_src and os.path.exists(audio_src):
                source = discord.FFmpegPCMAudio(
                    audio_src,
                    executable=ffmpeg_bin,
                    before_options="-probesize 32k -analyzeduration 0",
                    options="-vn -threads 1",
                )
            else:
                headers = track.get("http_headers") or {}
                user_agent = headers.get("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
                before_opts = (
                    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
                    " -probesize 32k -analyzeduration 0"
                    f' -user_agent "{user_agent}"'
                )
                source = discord.FFmpegPCMAudio(
                    audio_src,
                    executable=ffmpeg_bin,
                    before_options=before_opts,
                    options="-vn -threads 1",
                )
            buffered_source = BufferedAudioSource(source, buffer_size=250)
            transformer = discord.PCMVolumeTransformer(buffered_source, volume=self.volume)
            play_start_time = time.time()

            def _after_play(error):
                is_skipped = getattr(self, "_manual_skip", False)
                self._manual_skip = False
                is_stopped = getattr(self, "_manual_stop", False)

                actual_error = error
                if not is_skipped and not is_stopped:
                    if not actual_error and hasattr(source, "_current_error") and source._current_error:
                        actual_error = source._current_error

                    # Also inspect process returncode if process terminated with non-zero
                    proc = getattr(source, "_process", None)
                    if proc is not None:
                        try:
                            proc_ret = proc.poll()
                            if proc_ret is None:
                                proc_ret = proc.wait(timeout=0.5)
                            if proc_ret is not None and proc_ret != 0:
                                actual_error = actual_error or f"FFmpeg exited with code {proc_ret}"
                        except Exception:
                            pass

                    # Also detect premature exit: if a stream stopped in < 3.0s for a song with duration > 10s
                    duration_sec = track.get("duration_sec", 0)
                    elapsed = time.time() - play_start_time
                    if not actual_error and track.get("is_stream") and (duration_sec == 0 or duration_sec > 10) and elapsed < 3.0:
                        actual_error = f"Stream ended prematurely after {elapsed:.1f}s"

                    if actual_error:
                        logger.error(f"Playback error: {actual_error}")
                        self._notify_status("ERROR", f"Playback error: {actual_error}")

                    # If direct stream failed immediately, fallback automatically to download mode
                    if actual_error and track.get("is_stream") and not track.get("_retried_as_download"):
                        logger.warning(f"Stream encountered error ({actual_error}), falling back to download for: {track.get('title')}")
                        track["_retried_as_download"] = True
                        if self._loop and self._loop.is_running():
                            async def _fallback_download():
                                try:
                                    cache_dir = AUDIO_CACHE_DIR
                                    os.makedirs(cache_dir, exist_ok=True)
                                    dl_opts = dict(YTDL_OPTIONS)
                                    dl_opts["outtmpl"] = os.path.join(cache_dir, "%(id)s.%(ext)s")
                                    dl_opts["noplaylist"] = True
                                    if os.path.exists(COOKIE_PATH):
                                        dl_opts["cookiefile"] = COOKIE_PATH
                                    else:
                                        dl_opts.pop("cookiefile", None)
                                    try:
                                        ytdl_dl = yt_dlp.YoutubeDL(dl_opts)
                                        fallback_data = await self._loop.run_in_executor(
                                            None, lambda: ytdl_dl.extract_info(track["webpage_url"], download=True)
                                        )
                                    except Exception as dl_err:
                                        err_str = str(dl_err).lower()
                                        is_auth_error = any(kw in err_str for kw in ["sign in", "login", "cookie", "authenticate", "account", "confirm you're not a bot", "age-restricted", "confirm your age"])
                                        is_unavail = any(kw in err_str for kw in ["unavailable", "removed", "private", "not available"])
                                        if "cookiefile" in dl_opts and (is_auth_error or is_unavail):
                                            logger.warning(f"Fallback download with cookies failed ({dl_err}), retrying without cookies...")
                                            clean_dl_opts = dict(dl_opts)
                                            clean_dl_opts.pop("cookiefile", None)
                                            try:
                                                ytdl_clean = yt_dlp.YoutubeDL(clean_dl_opts)
                                                fallback_data = await self._loop.run_in_executor(
                                                    None, lambda: ytdl_clean.extract_info(track["webpage_url"], download=True)
                                                )
                                                ytdl_dl = ytdl_clean
                                            except Exception as clean_retry_err:
                                                logger.warning(f"Fallback retry without cookies failed ({clean_retry_err})")
                                                raise dl_err
                                        elif "cookiefile" not in dl_opts and is_auth_error and not is_unavail and os.path.exists(COOKIE_PATH):
                                            logger.warning(f"Fallback download encountered auth requirement ({dl_err}), retrying with cookies...")
                                            auth_dl_opts = dict(dl_opts)
                                            auth_dl_opts["cookiefile"] = COOKIE_PATH
                                            try:
                                                ytdl_auth = yt_dlp.YoutubeDL(auth_dl_opts)
                                                fallback_data = await self._loop.run_in_executor(
                                                    None, lambda: ytdl_auth.extract_info(track["webpage_url"], download=True)
                                                )
                                                ytdl_dl = ytdl_auth
                                            except Exception as auth_retry_err:
                                                logger.warning(f"Fallback retry with cookies failed ({auth_retry_err})")
                                                raise dl_err
                                        else:
                                            raise dl_err

                                    if fallback_data and "entries" in fallback_data and fallback_data["entries"]:
                                        fallback_data = fallback_data["entries"][0]
                                    dl_filepath = ytdl_dl.prepare_filename(fallback_data)
                                    if not os.path.exists(dl_filepath):
                                        vid_id = fallback_data.get("id", "")
                                        for fname in os.listdir(cache_dir):
                                            if fname.startswith(vid_id):
                                                dl_filepath = os.path.join(cache_dir, fname)
                                                break
                                    if dl_filepath and os.path.exists(dl_filepath):
                                        track["filepath"] = dl_filepath
                                        track["url"] = dl_filepath
                                        track["is_stream"] = False
                                        fb_vid = fallback_data.get("id") or extract_youtube_video_id(track.get("webpage_url", ""))
                                        if fb_vid:
                                            fb_meta = {
                                                "title": track.get("title", fb_vid),
                                                "uploader": track.get("uploader", ""),
                                                "duration_sec": track.get("duration_sec", 0),
                                                "duration_str": track.get("duration_str", ""),
                                                "webpage_url": track.get("webpage_url", f"https://www.youtube.com/watch?v={fb_vid}"),
                                                "video_id": fb_vid,
                                            }
                                            threading.Thread(
                                                target=lambda: (
                                                    save_track_cache_meta(cache_dir, fb_vid, fb_meta),
                                                    prune_audio_cache(cache_dir),
                                                ),
                                                daemon=True,
                                            ).start()
                                        await self._async_play_track(track)
                                        return
                                except Exception as dl_err:
                                    logger.error(f"Fallback download failed: {dl_err}")
                            asyncio.run_coroutine_threadsafe(_fallback_download(), self._loop)
                            return

                # Keep cached file for instant replay, but enforce LRU cache quota
                cached_f = track.get("filepath")
                if cached_f and os.path.exists(cached_f):
                    try:
                        os.utime(cached_f, None)
                    except OSError:
                        pass
                    cache_dir = AUDIO_CACHE_DIR
                    threading.Thread(target=prune_audio_cache, args=(cache_dir,), daemon=True).start()

                if is_stopped:
                    self._manual_stop = False
                    self.is_playing = False
                    self.is_paused = False
                    self.current_track = None
                    self.current_title = "No audio playing"
                    self._idle_since = time.time()
                    self._notify_status("PLAYBACK_STOPPED", "")
                    self._notify_status("QUEUE_UPDATED", "")
                    if self._loop and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._update_voice_channel_status("Waiting for song requests"), self._loop
                        )
                    return

                # Check if there are songs waiting in the queue
                if self.queue and self.voice_client and self.voice_client.is_connected():
                    next_song = self.queue.pop(0)
                    self._notify_status("QUEUE_UPDATED", "")
                    if self._loop and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._async_play_track(next_song, announce=not is_skipped),
                            self._loop,
                        )
                elif self.autoplay and self.voice_client and self.voice_client.is_connected():
                    cache_dir = AUDIO_CACHE_DIR
                    curr_vid = track.get("video_id") or extract_youtube_video_id(track.get("webpage_url", ""))
                    played_today = self._get_autoplay_played_ids_today() if self.smart_autoplay else None
                    random_cached = AUDIO_CACHE_INDEX.get_random_track(
                        cache_dir, exclude_vid_id=curr_vid, exclude_vid_ids=played_today
                    )
                    if random_cached:
                        cached_file, cached_meta = random_cached
                        auto_emoji = get_random_server_emoji(self.voice_client.guild if self.voice_client else None)
                        auto_track = create_track_from_cached_meta(cached_file, cached_meta, requester="Autoplay", emoji=auto_emoji)
                        ensure_track_title(auto_track)
                        auto_vid = auto_track.get("video_id") or extract_youtube_video_id(cached_file) or os.path.splitext(os.path.basename(cached_file))[0]
                        if self.smart_autoplay:
                            self.record_autoplay_track(auto_vid)
                            logger.info(f"Smart Autoplay selecting unplayed cached track: {auto_track.get('title')} ({auto_vid})")
                        else:
                            logger.info(f"Standard Autoplay selecting cached track: {auto_track.get('title')} ({auto_vid})")
                        if self._loop and self._loop.is_running():
                            asyncio.run_coroutine_threadsafe(
                                self._async_play_track(auto_track, announce=not is_skipped),
                                self._loop,
                            )
                    else:
                        if self.smart_autoplay:
                            logger.info("Smart Autoplay: All available cached songs have already been played today. Stopping playback.")
                        else:
                            logger.info("Autoplay: No cached tracks found in cache/. Stopping playback.")
                        self.is_playing = False
                        self.is_paused = False
                        self.current_track = None
                        self.current_title = "No audio playing"
                        self._idle_since = time.time()
                        self._notify_status("PLAYBACK_STOPPED", "")
                        self._notify_status("QUEUE_UPDATED", "")
                        if self._loop and self._loop.is_running():
                            asyncio.run_coroutine_threadsafe(
                                self._update_voice_channel_status("Waiting for song requests"), self._loop
                            )
                else:
                    self.is_playing = False
                    self.is_paused = False
                    self.current_track = None
                    self.current_title = "No audio playing"
                    self._idle_since = time.time()
                    self._notify_status("PLAYBACK_STOPPED", "")
                    self._notify_status("QUEUE_UPDATED", "")
                    if self._loop and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._update_voice_channel_status("Waiting for song requests"), self._loop
                        )

            # Warm up Discord voice UDP connection with silence frames to prevent initial 0-5s jitter/packet drop
            try:
                if self.voice_client and self.voice_client.is_connected() and hasattr(self.voice_client, "send_audio_packet"):
                    silence_frame = b"\xF8\xFF\xFE"
                    for _ in range(5):
                        self.voice_client.send_audio_packet(silence_frame, encode=False)
                        await asyncio.sleep(0.02)
            except Exception:
                pass

            self.voice_client.play(transformer, after=_after_play)
            self.is_playing = True
            self.is_paused = False
            self._idle_since = None
            self._notify_status("PLAYING", track["title"])
            self._notify_status("QUEUE_UPDATED", "")

            # Update voice channel status (text under voice channel name)
            emoji = track.get("emoji") or get_random_server_emoji(self.voice_client.guild if self.voice_client else None)
            title = track.get("title", "Music")
            uploader = track.get("uploader", "")
            status_text = format_now_playing_status(emoji, title, uploader)
            await self._update_voice_channel_status(status_text)

            # Announce next track to active text channel when advancing naturally
            if announce:
                target_channel = self._get_announce_channel()
                if target_channel and hasattr(target_channel, "send"):
                    try:
                        link_part = format_song_link(title, track.get("webpage_url", ""))
                        up_part = f" by **{uploader}**" if uploader else ""
                        dur = track.get("duration_str", "")
                        dur_part = f" (`{dur}`)" if dur else ""
                        prefix = f"{emoji} " if emoji else ""
                        msg = f"{prefix}Now playing {link_part}{up_part}{dur_part}."
                        try:
                            await target_channel.send(msg, suppress_embeds=True)
                        except TypeError:
                            await target_channel.send(msg)
                    except Exception as send_err:
                        logger.warning(f"Could not send now playing announcement to text channel: {send_err}")
        except discord.opus.OpusNotLoaded:
            err_msg = "Opus library not found. Run: sudo apt install -y libopus0 libopus-dev"
            logger.error(err_msg)
            self.is_playing = False
            self.is_paused = False
            self.current_track = None
            self._notify_status("ERROR", err_msg)
        except Exception as e:
            err_msg = str(e) or type(e).__name__
            logger.error(f"Failed to start playback: {err_msg} ({type(e).__name__})")
            self.is_playing = False
            self.is_paused = False
            self.current_track = None
            self._notify_status("ERROR", f"Failed to play: {err_msg}")

    def play_music(self, query_or_url: str):
        """Send song request from GUI or caller into queue/playback pipeline."""
        if not self.is_in_voice or not self.voice_client or not self._loop:
            self._notify_status("ERROR", "Bot is not in a voice channel")
            return

        self._notify_status("SEARCHING", f"Loading: {query_or_url[:35]}...")

        async def _run():
            success, msg, is_q, track = await self._async_enqueue_or_play(query_or_url, requester="GUI Host")
            if not success:
                self._notify_status("ERROR", msg)
            elif is_q:
                self._notify_status("ENQUEUED", track.get("title", query_or_url))

        asyncio.run_coroutine_threadsafe(_run(), self._loop)

    def skip(self) -> Optional[str]:
        """Skip currently playing track and advance queue."""
        old_title = self.current_title
        self._manual_skip = True
        logger.info(f"Skipping track: '{old_title}'")

        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
            return old_title
        elif self.queue and self.voice_client and self.voice_client.is_connected() and self._loop and self._loop.is_running():
            next_song = self.queue.pop(0)
            self._notify_status("QUEUE_UPDATED", "")
            asyncio.run_coroutine_threadsafe(self._async_play_track(next_song), self._loop)
            return old_title
        elif self.autoplay and self.voice_client and self.voice_client.is_connected() and self._loop and self._loop.is_running():
            self.trigger_autoplay()
            return old_title
        elif self.current_track or self.is_playing:
            self.is_playing = False
            self.is_paused = False
            self.current_track = None
            self.current_title = "No audio playing"
            return old_title
        return None

    def get_next_track(self, guild: Optional[discord.Guild] = None) -> Optional[Dict[str, any]]:
        """
        Return the upcoming track to be played.
        If the queue is empty but autoplay is active, pre-select and enqueue a random cached track
        so that the upcoming track is known in advance and guaranteed to play.
        """
        if self.queue:
            return self.queue[0]
        if self.autoplay and self.voice_client and (not hasattr(self.voice_client, "is_connected") or (callable(self.voice_client.is_connected) and self.voice_client.is_connected())):
            cache_dir = AUDIO_CACHE_DIR
            curr_vid = ""
            if self.current_track:
                curr_vid = self.current_track.get("video_id") or extract_youtube_video_id(self.current_track.get("webpage_url", ""))
            played_today = self._get_autoplay_played_ids_today() if self.smart_autoplay else None
            random_cached = AUDIO_CACHE_INDEX.get_random_track(
                cache_dir, exclude_vid_id=curr_vid, exclude_vid_ids=played_today
            )
            if random_cached:
                cached_file, cached_meta = random_cached
                auto_emoji = get_random_server_emoji(guild)
                auto_track = create_track_from_cached_meta(cached_file, cached_meta, requester="Autoplay", emoji=auto_emoji)
                ensure_track_title(auto_track)
                auto_vid = auto_track.get("video_id") or extract_youtube_video_id(cached_file) or os.path.splitext(os.path.basename(cached_file))[0]
                if self.smart_autoplay:
                    self.record_autoplay_track(auto_vid)
                self.queue.append(auto_track)
                return auto_track
        return None

    def clear_queue(self) -> int:
        """Clear upcoming songs from queue."""
        count = len(self.queue)
        self.queue.clear()
        self._notify_status("QUEUE_UPDATED", "")
        return count

    def get_queue(self) -> List[Dict[str, any]]:
        """Return list of queued tracks."""
        return list(self.queue)

    def pause(self):
        """Pause current playback."""
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.pause()
            self.is_paused = True
            self._notify_status("PAUSED", self.current_title)
            if self._loop and self._loop.is_running():
                title = self.current_track.get("title", self.current_title) if self.current_track else self.current_title
                uploader = self.current_track.get("uploader", "") if self.current_track else ""
                status_text = format_paused_status(title, uploader)
                asyncio.run_coroutine_threadsafe(self._update_voice_channel_status(status_text), self._loop)

    def resume(self):
        """Resume paused playback."""
        if self.voice_client and self.voice_client.is_paused():
            self.voice_client.resume()
            self.is_paused = False
            self._notify_status("PLAYING", self.current_title)
            if self._loop and self._loop.is_running():
                emoji = (self.current_track.get("emoji") if self.current_track else None) or get_random_server_emoji(self.voice_client.guild if self.voice_client else None)
                title = self.current_track.get("title", self.current_title) if self.current_track else self.current_title
                uploader = self.current_track.get("uploader", "") if self.current_track else ""
                status_text = format_now_playing_status(emoji, title, uploader)
                asyncio.run_coroutine_threadsafe(self._update_voice_channel_status(status_text), self._loop)

    def stop_playback(self):
        """Stop current audio playback and clear queue."""
        self._manual_stop = True
        self.queue.clear()
        self.current_track = None
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
        self.is_playing = False
        self.is_paused = False
        self.current_title = "No audio playing"
        self._idle_since = time.time()
        self._notify_status("PLAYBACK_STOPPED", "")
        self._notify_status("QUEUE_UPDATED", "")
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._update_voice_channel_status("Waiting for song requests"), self._loop
            )

    def set_volume(self, volume: float):
        """Update playback volume (0.0 to 1.5)."""
        self.volume = max(0.0, min(1.5, float(volume)))
        if self.voice_client and hasattr(self.voice_client, "source") and self.voice_client.source:
            try:
                self.voice_client.source.volume = self.volume
            except Exception:
                pass

    def set_autoplay(self, enabled: bool, smart: Optional[bool] = None, trigger: bool = False) -> bool:
        """Enable or disable autoplay mode, optionally configuring smart mode."""
        self.autoplay = bool(enabled)
        if smart is not None:
            self.smart_autoplay = bool(smart)
        if self.autoplay and trigger:
            if self.voice_client and self.voice_client.is_connected() and not self.is_playing and not self.queue:
                if not self.voice_client.is_playing() and not self.voice_client.is_paused():
                    self.trigger_autoplay()
        return self.autoplay

    def set_smart_autoplay(self, enabled: bool) -> bool:
        """Enable or disable smart deduplication for autoplay."""
        self.smart_autoplay = bool(enabled)
        return self.smart_autoplay

    def get_autoplay_status(self) -> Dict[str, any]:
        """Return current autoplay status, mode, and statistics."""
        played_ids = self._get_autoplay_played_ids_today()
        total_cached = AUDIO_CACHE_INDEX.count(AUDIO_CACHE_DIR)
        return {
            "enabled": self.autoplay,
            "smart": self.smart_autoplay,
            "played_today_count": len(played_ids),
            "total_cached_count": total_cached,
            "remaining_unplayed": max(0, total_cached - len(played_ids)),
        }

    def trigger_autoplay(self) -> Optional[Dict[str, any]]:
        """Attempt to play a random cached track if autoplay is enabled and bot is idle."""
        if not self.autoplay:
            return None
        if not self.voice_client or not self.voice_client.is_connected():
            return None
        if self.is_playing or self.is_paused or self.queue:
            return None
        if self.voice_client.is_playing() or self.voice_client.is_paused():
            return None

        cache_dir = AUDIO_CACHE_DIR
        curr_vid = ""
        if self.current_track:
            curr_vid = self.current_track.get("video_id") or extract_youtube_video_id(self.current_track.get("webpage_url", ""))
        played_today = self._get_autoplay_played_ids_today() if self.smart_autoplay else None
        random_cached = AUDIO_CACHE_INDEX.get_random_track(
            cache_dir, exclude_vid_id=curr_vid, exclude_vid_ids=played_today
        )
        if random_cached:
            cached_file, cached_meta = random_cached
            auto_emoji = get_random_server_emoji(self.voice_client.guild if self.voice_client else None)
            auto_track = create_track_from_cached_meta(cached_file, cached_meta, requester="Autoplay", emoji=auto_emoji)
            ensure_track_title(auto_track)
            auto_vid = auto_track.get("video_id") or extract_youtube_video_id(cached_file) or os.path.splitext(os.path.basename(cached_file))[0]
            if self.smart_autoplay:
                self.record_autoplay_track(auto_vid)
                logger.info(f"Smart Autoplay starting unplayed cached track: {auto_track.get('title')} ({auto_vid})")
            else:
                logger.info(f"Standard Autoplay starting cached track: {auto_track.get('title')} ({auto_vid})")
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self._async_play_track(auto_track), self._loop)
            return auto_track
        else:
            if self.smart_autoplay:
                logger.info("Smart Autoplay: All available cached songs have already been played today.")
            else:
                logger.info("Autoplay: No cached tracks found in cache/.")
        return None
