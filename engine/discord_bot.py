"""
Discord bot audio streaming controller.
Runs an embedded client inside a dedicated background asyncio thread.
Handles queue management and slash commands.
"""

import asyncio
import html
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import warnings
from typing import Callable, Dict, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
import imageio_ffmpeg
import yt_dlp

# Suppress benign aiohttp unclosed connector ResourceWarnings on exit
warnings.filterwarnings("ignore", message=".*unclosed.*", category=ResourceWarning)
warnings.filterwarnings("ignore", message=".*Unclosed.*", category=ResourceWarning)

from engine.security import (
    secure_file_permissions,
    mask_token,
    sanitize_audio_target,
)



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

# YTDL options for fast, resilient audio stream extraction (bypasses datacenter bot blocks)
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "extractaudio": True,
    "audioformat": "opus",
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
}

# Automatically bind cookies.txt if present to authenticate with YouTube (optional fallback)
COOKIE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cookies.txt"))
if os.path.exists(COOKIE_PATH):
    YTDL_OPTIONS["cookiefile"] = COOKIE_PATH

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


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


def resolve_song_info(query_or_url: str) -> Tuple[bool, str, str]:
    """
    Resolve real track title and canonical URL using yt-dlp.
    Can run standalone without needing an active Discord bot gateway session.
    Returns: (success, resolved_title, canonical_url)
    """
    try:
        from engine.security import sanitize_audio_target
        target = normalize_youtube_url(query_or_url)
        is_safe, sanitized_target, _ = sanitize_audio_target(target)
        if not is_safe:
            return False, query_or_url, query_or_url

        if not (sanitized_target.startswith("http://") or sanitized_target.startswith("https://")):
            sanitized_target = f"ytsearch1:{sanitized_target}"

        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            data = ydl.extract_info(sanitized_target, download=False)
            if "entries" in data and data["entries"]:
                data = data["entries"][0]
            title = data.get("title", query_or_url)
            url = data.get("webpage_url") or data.get("url") or sanitized_target
            return True, title, url
    except Exception as e:
        print(f"[DiscordBot] Notice: could not resolve song metadata: {e}")
        return False, query_or_url, query_or_url


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
        return vc

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

        @bot.tree.command(name="join", description="Hubungkan bot Woeyyy ke voice channel tempat kamu berada")
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
                await interaction.followup.send(f"Connected to `#{channel.name}`.")
            except Exception as e:
                print(f"[DiscordBot] Error connecting to voice channel: {e}")
                await interaction.followup.send(f"Failed to connect to voice channel: {e}")

        @bot.tree.command(name="play", description="Putar lagu dari YouTube / YouTube Music atau tambahkan ke antrean")
        @app_commands.describe(query="Judul lagu, link YouTube, atau link YouTube Music")
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
                print(f"[DiscordBot] Error connecting to voice channel: {e}")
                await interaction.followup.send(f"Failed to connect to voice channel: {e}")
                return

            # Send immediate feedback so Discord's "Woeyyy is thinking..." disappears in 0.1s!
            msg_handle = await interaction.followup.send("Searching...")

            self._notify_status("SEARCHING", "Searching for track...")

            requester_name = interaction.user.display_name
            success, msg, is_queued, track = await self._async_enqueue_or_play(query, requester=requester_name)

            if not success:
                await msg_handle.edit(content=f"Error: {msg}")
                return

            title = track.get("title", query)
            dur = track.get("duration_str", "Live")
            uploader = track.get("uploader", "")
            url = track.get("webpage_url", "")

            link_part = f"[{title}]({url})" if url else f"**{title}**"
            uploader_part = f" by **{uploader}**" if uploader else ""
            dur_part = f" (` {dur} `)" if dur else ""

            if is_queued:
                pos = len(self.queue)
                await msg_handle.edit(
                    content=f"Added {link_part}{uploader_part}{dur_part} to the queue at position #{pos}."
                )
            else:
                await msg_handle.edit(
                    content=f"Added {link_part}{uploader_part}{dur_part} to begin playing."
                )

        @cmd_play.autocomplete("query")
        async def play_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> List[app_commands.Choice[str]]:
            if not current or not current.strip():
                return []

            suggestions = await self.get_autocomplete_suggestions(current)
            return [
                app_commands.Choice(name=item["name"], value=item["value"])
                for item in suggestions[:25]
            ]

        @bot.tree.command(name="skip", description="Lewati lagu yang sedang diputar dan putar lagu berikutnya di antrean")
        async def cmd_skip(interaction: discord.Interaction):
            if not self.is_playing and not self.is_paused:
                await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)
                return

            old_title = self.current_title
            next_track = self.queue[0] if self.queue else None
            self.skip()

            if next_track:
                next_title = next_track.get("title", "Next Track")
                next_url = next_track.get("webpage_url", "")
                next_dur = next_track.get("duration_str", "Live")
                next_uploader = next_track.get("uploader", "")

                next_link = f"[{next_title}]({next_url})" if next_url else f"**{next_title}**"
                next_up = f" by **{next_uploader}**" if next_uploader else ""
                next_dur_part = f" (` {next_dur} `)" if next_dur else ""

                await interaction.response.send_message(
                    f"Skipped **{old_title}**.\nNow playing {next_link}{next_up}{next_dur_part}."
                )
            else:
                await interaction.response.send_message(
                    f"Skipped **{old_title}**. The queue is now empty."
                )

        @bot.tree.command(name="queue", description="Lihat daftar antrean lagu yang akan diputar")
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

                cur_link = f"[{c_title}]({c_url})" if c_url else f"**{c_title}**"
                cur_up = f" by **{c_up}**" if c_up else ""
                cur_dur = f" (` {c_dur} `)" if c_dur else ""
                lines.append(f"Now playing: {cur_link}{cur_up}{cur_dur}")

            if self.queue:
                lines.append(f"\nQueue ({len(self.queue)} tracks):")
                for i, t in enumerate(self.queue[:10], start=1):
                    t_title = t.get("title", "Unknown")
                    t_url = t.get("webpage_url", "")
                    t_dur = t.get("duration_str", "Live")
                    t_up = t.get("uploader", "")

                    t_link = f"[{t_title}]({t_url})" if t_url else f"**{t_title}**"
                    t_up_part = f" by **{t_up}**" if t_up else ""
                    t_dur_part = f" (` {t_dur} `)" if t_dur else ""
                    lines.append(f"`{i}.` {t_link}{t_up_part}{t_dur_part}")
                if len(self.queue) > 10:
                    lines.append(f"... and {len(self.queue) - 10} more tracks.")

            await interaction.response.send_message("\n".join(lines))

        @bot.tree.command(name="clear", description="Kosongkan semua antrean lagu yang ada")
        async def cmd_clear(interaction: discord.Interaction):
            count = self.clear_queue()
            await interaction.response.send_message(f"Cleared {count} tracks from the queue.")

        @bot.tree.command(name="pause", description="Pause lagu yang sedang diputar")
        async def cmd_pause(interaction: discord.Interaction):
            if self.voice_client and self.voice_client.is_playing():
                self.voice_client.pause()
                self.is_paused = True
                self._notify_status("PAUSED", self.current_title)
                await interaction.response.send_message("Playback paused.")
            else:
                await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)

        @bot.tree.command(name="resume", description="Lanjutkan lagu yang dijeda")
        async def cmd_resume(interaction: discord.Interaction):
            if self.voice_client and self.voice_client.is_paused():
                self.voice_client.resume()
                self.is_paused = False
                self._notify_status("PLAYING", self.current_title)
                await interaction.response.send_message("Playback resumed.")
            else:
                await interaction.response.send_message("Playback is not paused.", ephemeral=True)

        @bot.tree.command(name="stop", description="Hentikan lagu dan bersihkan antrean")
        async def cmd_stop(interaction: discord.Interaction):
            self.stop_playback()
            await interaction.response.send_message("Playback stopped and queue cleared.")

        @bot.tree.command(name="volume", description="Ubah volume suara bot (0% - 150%)")
        @app_commands.describe(percentage="Persentase volume (contoh: 100)")
        async def cmd_volume(interaction: discord.Interaction, percentage: int):
            vol = max(0, min(150, percentage)) / 100.0
            self.set_volume(vol)
            await interaction.response.send_message(f"Volume set to `{percentage}%`.")

        @bot.tree.command(name="leave", description="Keluarkan bot dari Voice Channel")
        async def cmd_leave(interaction: discord.Interaction):
            guild_vc = getattr(interaction.guild, "voice_client", None) if interaction.guild else None
            if (self.voice_client and self.voice_client.is_connected()) or guild_vc:
                self.leave_voice_channel()
                await interaction.response.send_message("Disconnected from voice channel.")
            else:
                await interaction.response.send_message("Bot is not in a voice channel.", ephemeral=True)

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
            print(f"[DiscordBot] Logged in successfully as {bot_name}")
            self._refresh_voice_channels_internal()
            self._notify_status("ONLINE", bot_name)

            # Sync slash commands (/) across all guilds for instant availability
            try:
                for guild in self.client.guilds:
                    self.client.tree.copy_global_to(guild=guild)
                    await self.client.tree.sync(guild=guild)
                await self.client.tree.sync()
                print("[DiscordBot] Slash commands (/) synced successfully to all servers!")
            except Exception as e:
                print(f"[DiscordBot] Note on syncing slash commands: {e}")

        @self.client.event
        async def on_voice_state_update(member, before, after):
            if member == self.client.user:
                if after.channel is None:
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
                    self._notify_status("VOICE_DISCONNECTED", "Left voice channel")
                else:
                    self.is_in_voice = True
                    self.current_channel_id = after.channel.id
                    self.voice_client = getattr(after.channel.guild, "voice_client", None)
                    self._notify_status("VOICE_CONNECTED", after.channel.name)

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
            self.queue.clear()
            self.current_track = None
            self._notify_status("VOICE_DISCONNECTED", "Left voice channel")
            self._notify_status("QUEUE_UPDATED", "")

        asyncio.run_coroutine_threadsafe(_async_leave(), self._loop)

    async def _async_enqueue_or_play(self, query_or_url: str, requester: str = "Host") -> Tuple[bool, str, bool, Dict[str, any]]:
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
                return False, f"Keamanan: Tautan ditolak ({reason})", False, {}

            if not (sanitized_target.startswith("http://") or sanitized_target.startswith("https://")):
                sanitized_target = f"ytsearch1:{sanitized_target}"

            cache_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cache"))
            os.makedirs(cache_dir, exist_ok=True)

            loop = asyncio.get_event_loop()
            data = None
            direct_url = None
            filepath = None
            http_headers = {}

            if self.is_local:
                # Local Mode: Direct Streaming without full file download & without cookies
                stream_opts = dict(YTDL_OPTIONS)
                stream_opts.pop("cookiefile", None)
                stream_opts["noplaylist"] = True
                try:
                    ytdl_stream = yt_dlp.YoutubeDL(stream_opts)
                    data = await loop.run_in_executor(
                        None, lambda: ytdl_stream.extract_info(sanitized_target, download=False)
                    )
                except Exception as stream_err:
                    print(f"[DiscordBot] Local direct stream extraction notice: {stream_err}")

                if data and "entries" in data:
                    entries = [e for e in data["entries"] if e]
                    data = entries[0] if entries else None

                if data:
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
                dl_opts = dict(YTDL_OPTIONS)
                dl_opts["outtmpl"] = os.path.join(cache_dir, "%(id)s.%(ext)s")
                dl_opts["noplaylist"] = True

                try:
                    ytdl_dl = yt_dlp.YoutubeDL(dl_opts)
                    data = await loop.run_in_executor(
                        None, lambda: ytdl_dl.extract_info(sanitized_target, download=True)
                    )
                except Exception as dl_err:
                    if "cookiefile" in dl_opts:
                        print(f"[DiscordBot] Download with cookies encountered error ({dl_err}), retrying without cookies...")
                        dl_opts.pop("cookiefile", None)
                        ytdl_dl = yt_dlp.YoutubeDL(dl_opts)
                        data = await loop.run_in_executor(
                            None, lambda: ytdl_dl.extract_info(sanitized_target, download=True)
                        )
                    else:
                        raise dl_err

                if data and "entries" in data:
                    entries = [e for e in data["entries"] if e]
                    if not entries:
                        return False, "Track not found.", False, {}
                    data = entries[0]

                if not data:
                    return False, "Track not found.", False, {}

                filepath = ytdl_dl.prepare_filename(data)
                if not os.path.exists(filepath):
                    vid_id = data.get("id", "")
                    for fname in os.listdir(cache_dir):
                        if fname.startswith(vid_id):
                            filepath = os.path.join(cache_dir, fname)
                            break
                direct_url = filepath

            if not data:
                return False, "Track not found.", False, {}

            title = data.get("title", query_or_url)
            uploader = data.get("uploader") or data.get("channel") or data.get("artist") or ""
            sec = data.get("duration", 0) or 0
            dur_str = f"{sec // 60}:{sec % 60:02d}" if sec else "Live"

            track = {
                "filepath": filepath,
                "url": direct_url,
                "title": title,
                "uploader": uploader,
                "duration_sec": sec,
                "duration_str": dur_str,
                "webpage_url": data.get("webpage_url", query_or_url),
                "requester": requester,
                "http_headers": http_headers or data.get("http_headers", {}),
                "is_stream": filepath is None,
                "timestamp": time.time(),
            }

            # Check if playback is currently active
            if self.is_playing or self.is_paused:
                self.queue.append(track)
                self._notify_status("ENQUEUED", track.get("title", query_or_url))
                self._notify_status("QUEUE_UPDATED", "")
                return True, "Added to queue", True, track
            else:
                await self._async_play_track(track)
                return True, "Now playing", False, track

        except Exception as e:
            print(f"[DiscordBot] Failed to enqueue or play: {e}")
            return False, str(e), False, {}

    async def _async_play_track(self, track: Dict[str, any]):
        """Play track on current voice_client (using direct stream URL or cached file)."""
        if not self.voice_client or not self.voice_client.is_connected():
            return

        try:
            ensure_opus_loaded()
            if self.voice_client.is_playing() or self.voice_client.is_paused():
                self.voice_client.stop()

            self.current_track = track
            self.current_title = track["title"]

            ffmpeg_bin = get_ffmpeg_binary()
            audio_src = track.get("filepath") or track.get("url")

            # Dynamic refresh for long-queued direct stream URLs (> 2 hours)
            if track.get("is_stream") and track.get("webpage_url"):
                age = time.time() - track.get("timestamp", 0)
                if not audio_src or age > 7200:
                    try:
                        loop = asyncio.get_event_loop()
                        stream_opts = dict(YTDL_OPTIONS)
                        if self.is_local:
                            stream_opts.pop("cookiefile", None)
                        stream_opts["noplaylist"] = True
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
                    options="-vn",
                )
            else:
                headers = track.get("http_headers") or {}
                user_agent = headers.get("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
                before_opts = (
                    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
                    f' -user_agent "{user_agent}"'
                )
                source = discord.FFmpegPCMAudio(
                    audio_src,
                    executable=ffmpeg_bin,
                    before_options=before_opts,
                    options="-vn",
                )
            transformer = discord.PCMVolumeTransformer(source, volume=self.volume)

            def _after_play(error):
                actual_error = error
                if not actual_error and hasattr(source, "_current_error") and source._current_error:
                    actual_error = source._current_error

                if actual_error:
                    print(f"[DiscordBot] Playback error: {actual_error}")
                    self._notify_status("ERROR", f"Playback error: {actual_error}")

                # If direct stream failed immediately, fallback automatically to download mode
                if actual_error and track.get("is_stream") and not track.get("_retried_as_download"):
                    print(f"[DiscordBot] Stream encountered error, falling back to download for: {track.get('title')}")
                    track["_retried_as_download"] = True
                    if self._loop and self._loop.is_running():
                        async def _fallback_download():
                            try:
                                cache_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cache"))
                                os.makedirs(cache_dir, exist_ok=True)
                                dl_opts = dict(YTDL_OPTIONS)
                                dl_opts["outtmpl"] = os.path.join(cache_dir, "%(id)s.%(ext)s")
                                dl_opts["noplaylist"] = True
                                ytdl_dl = yt_dlp.YoutubeDL(dl_opts)
                                fallback_data = await self._loop.run_in_executor(
                                    None, lambda: ytdl_dl.extract_info(track["webpage_url"], download=True)
                                )
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
                                    await self._async_play_track(track)
                                    return
                            except Exception as dl_err:
                                print(f"[DiscordBot] Fallback download failed: {dl_err}")
                        asyncio.run_coroutine_threadsafe(_fallback_download(), self._loop)
                        return

                # Auto-cleanup cached file after song completes to preserve storage space
                try:
                    cached_f = track.get("filepath")
                    if cached_f and os.path.exists(cached_f):
                        os.remove(cached_f)
                except Exception:
                    pass

                # Check if there are songs waiting in the queue
                if self.queue and self.voice_client and self.voice_client.is_connected():
                    next_song = self.queue.pop(0)
                    self._notify_status("QUEUE_UPDATED", "")
                    if self._loop and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(self._async_play_track(next_song), self._loop)
                else:
                    self.is_playing = False
                    self.is_paused = False
                    self.current_track = None
                    self.current_title = "No audio playing"
                    self._notify_status("PLAYBACK_STOPPED", "")
                    self._notify_status("QUEUE_UPDATED", "")

            self.voice_client.play(transformer, after=_after_play)
            self.is_playing = True
            self.is_paused = False
            self._notify_status("PLAYING", track["title"])
            self._notify_status("QUEUE_UPDATED", "")
        except discord.opus.OpusNotLoaded:
            err_msg = "Opus library not found. Run: sudo apt install -y libopus0 libopus-dev"
            print(f"[DiscordBot] {err_msg}")
            self.is_playing = False
            self.is_paused = False
            self.current_track = None
            self._notify_status("ERROR", err_msg)
        except Exception as e:
            err_msg = str(e) or type(e).__name__
            print(f"[DiscordBot] Failed to start playback: {err_msg} ({type(e).__name__})")
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
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            old_title = self.current_title
            self.voice_client.stop()
            return old_title
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

    def resume(self):
        """Resume paused playback."""
        if self.voice_client and self.voice_client.is_paused():
            self.voice_client.resume()
            self.is_paused = False
            self._notify_status("PLAYING", self.current_title)

    def stop_playback(self):
        """Stop current audio playback and clear queue."""
        self.queue.clear()
        self.current_track = None
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
        self.is_playing = False
        self.is_paused = False
        self.current_title = "No audio playing"
        self._notify_status("PLAYBACK_STOPPED", "")
        self._notify_status("QUEUE_UPDATED", "")

    def set_volume(self, volume: float):
        """Update playback volume (0.0 to 1.5)."""
        self.volume = max(0.0, min(1.5, float(volume)))
        if self.voice_client and hasattr(self.voice_client, "source") and self.voice_client.source:
            try:
                self.voice_client.source.volume = self.volume
            except Exception:
                pass
