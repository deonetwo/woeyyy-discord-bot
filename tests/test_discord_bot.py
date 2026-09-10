"""
Unit tests for Woeyyy Discord Bot.
Verifies bot controller initialization, configuration storage,
FFmpeg binary presence, and thread lifecycle.
"""

import asyncio
import json
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engine.discord_bot import (
    AudioCacheIndex,
    BufferedAudioSource,
    DiscordVoiceBot,
    FFMPEG_EXECUTABLE,
    find_cached_track_by_query,
    load_saved_token,
    save_token,
)


class TestDiscordVoiceBot(unittest.TestCase):

    def test_ffmpeg_binary_exists(self):
        """Verify imageio-ffmpeg bundled binary exists and is executable."""
        self.assertTrue(os.path.exists(FFMPEG_EXECUTABLE))
        if sys.platform == "win32":
            self.assertTrue(FFMPEG_EXECUTABLE.endswith(".exe"))

    def test_token_save_and_load(self):
        """Verify token persistence in .env and DISCORD_BOT_TOKEN."""
        orig_token = load_saved_token()
        try:
            dummy_token = "TEST_DISCORD_TOKEN_12345"
            save_token(dummy_token)
            loaded = load_saved_token()
            self.assertEqual(dummy_token, loaded)
        finally:
            if orig_token:
                save_token(orig_token)


    def test_bot_controller_init(self):
        """Verify DiscordVoiceBot initial state and parameters."""
        bot = DiscordVoiceBot()
        self.assertFalse(bot.is_connected)
        self.assertFalse(bot.is_in_voice)
        self.assertFalse(bot.is_playing)
        self.assertEqual(bot.volume, 1.0)
        self.assertEqual(len(bot.available_channels), 0)

    def test_bot_volume_clamping(self):
        """Verify volume control adheres to [0.0, 1.5] bounds."""
        bot = DiscordVoiceBot()
        bot.set_volume(2.0)
        self.assertEqual(bot.volume, 1.5)
        bot.set_volume(-0.5)
        self.assertEqual(bot.volume, 0.0)
    def test_youtube_music_normalization(self):
        """Verify music.youtube.com URLs are rewritten to www.youtube.com."""
        from engine.discord_bot import normalize_youtube_url
        ym_url = "https://music.youtube.com/watch?v=ODqzYeSICCs&list=RDAMVM"
        expected = "https://www.youtube.com/watch?v=ODqzYeSICCs&list=RDAMVM"
        self.assertEqual(normalize_youtube_url(ym_url), expected)

        # Standard YouTube remains unchanged
        std_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        self.assertEqual(normalize_youtube_url(std_url), std_url)

    def test_queue_operations(self):
        """Verify queue manipulation methods."""
        bot = DiscordVoiceBot()
        self.assertEqual(len(bot.get_queue()), 0)

        # Enqueue dummy items directly
        bot.queue.append({"title": "Song 1", "duration_str": "3:20"})
        bot.queue.append({"title": "Song 2", "duration_str": "4:15"})
        self.assertEqual(len(bot.get_queue()), 2)

        # Clear queue
        cleared = bot.clear_queue()
        self.assertEqual(cleared, 2)
        self.assertEqual(len(bot.get_queue()), 0)

    def test_autocomplete_direct_url_and_empty(self):
        """Verify direct URLs and empty queries in autocomplete."""
        import asyncio
        from engine.discord_bot import async_search_youtube_suggestions

        # Empty query
        res_empty = asyncio.run(async_search_youtube_suggestions(""))
        self.assertEqual(res_empty, [])

        # Direct URL query
        test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        res_url = asyncio.run(async_search_youtube_suggestions(test_url))
        self.assertEqual(len(res_url), 1)
        self.assertEqual(res_url[0]["value"], test_url)
        self.assertTrue(res_url[0]["name"].startswith("🔗"))
        self.assertLessEqual(len(res_url[0]["name"]), 100)

    def test_autocomplete_live_query_and_formatting(self):
        """Verify live YouTube autocomplete returns formatted choices <= 100 chars."""
        import asyncio
        from engine.discord_bot import async_search_youtube_suggestions

        res = asyncio.run(async_search_youtube_suggestions("test", max_results=5))
        self.assertIsInstance(res, list)
        self.assertGreater(len(res), 0)
        for item in res:
            self.assertIn("name", item)
            self.assertIn("value", item)
            self.assertLessEqual(len(item["name"]), 100)
            self.assertTrue(item["name"].startswith("🎵") or item["name"].startswith("🔍"))

    def test_bot_autocomplete_caching(self):
        """Verify DiscordVoiceBot in-memory TTL caching for autocomplete."""
        import asyncio
        bot = DiscordVoiceBot()
        self.assertEqual(len(bot._autocomplete_cache), 0)

        # First lookup populates cache
        res1 = asyncio.run(bot.get_autocomplete_suggestions("test"))
        self.assertIn("test", bot._autocomplete_cache)
        self.assertEqual(len(bot._autocomplete_cache), 1)

        # Second lookup returns cached items instantly
        res2 = asyncio.run(bot.get_autocomplete_suggestions("test"))
        self.assertEqual(res1, res2)

        if bot._http_session and not bot._http_session.closed:
            asyncio.run(bot._http_session.close())

    def test_ensure_voice_connected_scenarios(self):
        """Verify _ensure_voice_connected handles healthy, moving, stale, and Already connected cases."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        import discord

        bot = DiscordVoiceBot()

        # Scenario 1: Already connected to same channel
        mock_channel = MagicMock()
        mock_channel.id = 123
        mock_channel.name = "General"
        mock_vc = MagicMock()
        mock_vc.is_connected.return_value = True
        mock_vc.channel.id = 123
        mock_channel.guild.voice_client = mock_vc

        res_vc = asyncio.run(bot._ensure_voice_connected(mock_channel))
        self.assertEqual(res_vc, mock_vc)
        self.assertTrue(bot.is_in_voice)
        self.assertEqual(bot.current_channel_id, 123)

        # Scenario 2: Connected to different channel -> calls move_to
        mock_channel2 = MagicMock()
        mock_channel2.id = 456
        mock_channel2.name = "Music"
        mock_vc.channel.id = 123
        mock_vc.move_to = AsyncMock()
        mock_channel2.guild.voice_client = mock_vc

        res_vc2 = asyncio.run(bot._ensure_voice_connected(mock_channel2))
        self.assertEqual(res_vc2, mock_vc)
        mock_vc.move_to.assert_awaited_once_with(mock_channel2)
        self.assertEqual(bot.current_channel_id, 456)

        # Scenario 3: Stale/zombie voice client -> calls disconnect(force=True) and connects new
        stale_vc = MagicMock()
        stale_vc.is_connected.return_value = False
        stale_vc.disconnect = AsyncMock()

        new_vc = MagicMock()
        new_vc.is_connected.return_value = True
        new_vc.channel.id = 789

        mock_channel3 = MagicMock()
        mock_channel3.id = 789
        mock_channel3.name = "Lobby"
        mock_channel3.guild.voice_client = stale_vc
        mock_channel3.connect = AsyncMock(return_value=new_vc)

        res_vc3 = asyncio.run(bot._ensure_voice_connected(mock_channel3))
        stale_vc.disconnect.assert_awaited_once_with(force=True)
        self.assertEqual(res_vc3, new_vc)
        self.assertEqual(bot.current_channel_id, 789)

    def test_bot_local_mode_initialization(self):
        """Verify is_local flag initialization via parameter and BOT_MODE env var."""
        bot_default = DiscordVoiceBot()
        self.assertFalse(bot_default.is_local)

        bot_local = DiscordVoiceBot(is_local=True)
        self.assertTrue(bot_local.is_local)

        bot_server = DiscordVoiceBot(is_local=False)
        self.assertFalse(bot_server.is_local)

        # Test env variable detection
        orig_env = os.environ.get("BOT_MODE")
        try:
            os.environ["BOT_MODE"] = "local"
            bot_from_env = DiscordVoiceBot()
            self.assertTrue(bot_from_env.is_local)

            os.environ["BOT_MODE"] = "server"
            bot_from_server_env = DiscordVoiceBot()
            self.assertFalse(bot_from_server_env.is_local)
        finally:
            if orig_env is not None:
                os.environ["BOT_MODE"] = orig_env
            else:
                os.environ.pop("BOT_MODE", None)

    def test_local_direct_stream_enqueue(self):
        """Verify _async_enqueue_or_play generates direct stream payload when is_local=True."""
        import asyncio
        bot = DiscordVoiceBot(is_local=True)
        # Mock play track so it doesn't try to connect to voice client
        async def mock_play(track):
            pass
        bot._async_play_track = mock_play

        success, msg, is_queued, track = asyncio.run(
            bot._async_enqueue_or_play("Never gonna give you up")
        )
        self.assertTrue(success)
        self.assertTrue(track.get("is_stream"))
        self.assertIsNone(track.get("filepath"))
        self.assertIsNotNone(track.get("url"))
        self.assertTrue(track["url"].startswith("http"))
        self.assertIn("Never Gonna Give You Up", track.get("title", ""))

    def test_user_history_recording_and_cap(self):
        """Test user history records songs, enforces max 25 limit, and moves existing song to top."""
        bot = DiscordVoiceBot()
        test_uid = 999888777
        bot.clear_user_history(test_uid)

        # 1. Add songs up to 30
        for i in range(1, 31):
            bot.record_user_history(test_uid, {
                "title": f"Song {i}",
                "uploader": f"Artist {i}",
                "webpage_url": f"https://youtube.com/watch?v={i}",
                "duration_str": "3:00",
            })

        history = bot.get_user_history(test_uid)
        # Verify cap at 25
        self.assertEqual(len(history), 25)
        # Verify newest is first
        self.assertEqual(history[0]["title"], "Song 30")
        self.assertEqual(history[-1]["title"], "Song 6")

        # 2. Test deduplication - replaying Song 10 moves it to top
        bot.record_user_history(test_uid, {
            "title": "Song 10",
            "uploader": "Artist 10",
            "webpage_url": "https://youtube.com/watch?v=10",
            "duration_str": "3:00",
        })
        history = bot.get_user_history(test_uid)
        self.assertEqual(len(history), 25)
        self.assertEqual(history[0]["title"], "Song 10")
        self.assertEqual(history[1]["title"], "Song 30")

        # Cleanup
        bot.clear_user_history(test_uid)
        self.assertEqual(len(bot.get_user_history(test_uid)), 0)

    def test_autocomplete_history_formatting(self):
        """Test autocomplete produces expected clock icon choices from user history."""
        import asyncio
        from unittest.mock import MagicMock
        import discord
        from discord.ext import commands

        bot = DiscordVoiceBot()
        test_uid = 111222333
        bot.clear_user_history(test_uid)

        bot.record_user_history(test_uid, {
            "title": "Hidamari Official Music Video",
            "uploader": "MsOOJA Channel",
            "webpage_url": "https://youtube.com/watch?v=hidamari",
            "duration_str": "4:15",
        })

        intents = discord.Intents.default()
        bot.client = commands.Bot(command_prefix="!", intents=intents)
        bot._register_slash_commands()
        # Find play command
        cmd = None
        for command in bot.client.tree.get_commands():
            if command.name == "play":
                cmd = command
                break

        self.assertIsNotNone(cmd)
        mock_interaction = MagicMock()
        mock_interaction.user.id = test_uid

        # Test empty query autocomplete returns history
        choices = asyncio.run(cmd._params["query"].autocomplete(mock_interaction, ""))
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0].name, "MsOOJA Channel - Hidamari Official Music Video")
        self.assertEqual(choices[0].value, "https://youtube.com/watch?v=hidamari")

        # Test query filtering returns matching history first
        choices_filtered = asyncio.run(cmd._params["query"].autocomplete(mock_interaction, "hidamari"))
        self.assertGreaterEqual(len(choices_filtered), 1)
        self.assertEqual(choices_filtered[0].name, "MsOOJA Channel - Hidamari Official Music Video")

        # Test empty choices returned when user has no history
        mock_new_user = MagicMock()
        mock_new_user.user.id = 999111222
        bot.clear_user_history(999111222)
        no_history_choices = asyncio.run(cmd._params["query"].autocomplete(mock_new_user, ""))
        self.assertEqual(len(no_history_choices), 0)
        self.assertEqual(no_history_choices, [])

        bot.clear_user_history(test_uid)

    def test_cmd_play_message_edit_no_suppress(self):
        """Test cmd_play edits msg_handle without invalid suppress parameter and records history."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        import discord
        from discord.ext import commands

        bot = DiscordVoiceBot()
        test_uid = 444555666
        bot.clear_user_history(test_uid)

        intents = discord.Intents.default()
        bot.client = commands.Bot(command_prefix="!", intents=intents)
        bot._register_slash_commands()

        cmd = None
        for command in bot.client.tree.get_commands():
            if command.name == "play":
                cmd = command
                break
        self.assertIsNotNone(cmd)

        mock_interaction = MagicMock()
        mock_interaction.user.id = test_uid
        mock_interaction.user.display_name = "TestUser"
        mock_interaction.user.voice.channel = MagicMock()
        mock_interaction.response.defer = AsyncMock()

        # Mock msg_handle
        mock_msg_handle = MagicMock()
        mock_msg_handle.edit = AsyncMock()
        mock_interaction.followup.send = AsyncMock(return_value=mock_msg_handle)

        sample_track = {
            "title": "Special Song",
            "uploader": "Special Artist",
            "webpage_url": "https://youtube.com/watch?v=specialsong",
            "duration_str": "3:45",
        }

        with patch.object(bot, "_ensure_voice_connected", new=AsyncMock()), \
             patch.object(bot, "_async_enqueue_or_play", new=AsyncMock(return_value=(True, "Now playing", False, sample_track))):
            asyncio.run(cmd.callback(mock_interaction, "Special Song"))

        # Check msg_handle.edit was called
        mock_msg_handle.edit.assert_called_once()
        call_kwargs = mock_msg_handle.edit.call_args[1]
        self.assertIn("content", call_kwargs)
        self.assertIn("Special Song", call_kwargs["content"])
        self.assertIn("Special Artist", call_kwargs["content"])
        # Crucial check: verify suppress was NOT passed (which caused TypeError on WebhookMessage)
        self.assertNotIn("suppress", call_kwargs)

        # Verify track was recorded to user history
        history = bot.get_user_history(test_uid)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["title"], "Special Song")

        bot.clear_user_history(test_uid)

    def test_get_random_server_emoji(self):
        """Test get_random_server_emoji returns server custom emoji when available or fallback."""
        from unittest.mock import MagicMock
        from engine.discord_bot import get_random_server_emoji

        # 1. Guild is None -> fallback
        emoji_none = get_random_server_emoji(None)
        self.assertIn(emoji_none, ["🎵", "🎶", "🎧", "✨"])

        # 2. Guild has empty emojis -> fallback
        mock_guild_empty = MagicMock()
        mock_guild_empty.emojis = []
        emoji_empty = get_random_server_emoji(mock_guild_empty)
        self.assertIn(emoji_empty, ["🎵", "🎶", "🎧", "✨"])

        # 3. Guild has custom emojis -> picks from server emojis
        mock_emoji1 = MagicMock()
        mock_emoji1.__str__.return_value = "<:pepejam:111222333>"
        mock_emoji1.available = True
        mock_emoji2 = MagicMock()
        mock_emoji2.__str__.return_value = "<a:blobdance:444555666>"
        mock_emoji2.available = True

        mock_guild = MagicMock()
        mock_guild.emojis = [mock_emoji1, mock_emoji2]

        picked = get_random_server_emoji(mock_guild)
        self.assertIn(picked, ["<:pepejam:111222333>", "<a:blobdance:444555666>"])

    def test_update_voice_channel_status(self):
        """Test _update_voice_channel_status calls http.edit_voice_channel_status and handles errors gracefully."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        bot = DiscordVoiceBot()
        bot.client = MagicMock()
        bot.client.http = MagicMock()
        bot.client.http.edit_voice_channel_status = AsyncMock()

        # 1. Successful update
        bot.current_channel_id = 123456789
        asyncio.run(bot._update_voice_channel_status("🎵 Testing Song"))
        bot.client.http.edit_voice_channel_status.assert_called_with("🎵 Testing Song", channel_id=123456789)

        # 2. Resilient against permission errors (Forbidden)
        bot.client.http.edit_voice_channel_status.side_effect = Exception("Missing Permissions: Set Voice Channel Status")
        # Should not raise
        try:
            asyncio.run(bot._update_voice_channel_status("🎵 Error Case"))
        except Exception:
            self.fail("_update_voice_channel_status raised an exception when permission was denied")

    def test_format_now_playing_status(self):
        """Test format_now_playing_status formats with clean bullet style and strips - Topic with bold unicode."""
        from engine.discord_bot import format_now_playing_status, format_paused_status, clean_artist_name, to_unicode_bold

        # 1. Clean - Topic from artist
        self.assertEqual(clean_artist_name("Hoi Festa - Topic"), "Hoi Festa")
        self.assertEqual(clean_artist_name("Coldplay"), "Coldplay")
        self.assertEqual(clean_artist_name(""), "")

        # 2. Unicode bold helper
        self.assertEqual(to_unicode_bold("Asuka 123"), "𝗔𝘀𝘂𝗸𝗮 𝟭𝟮𝟯")

        # 3. Title and artist with bullet and bold
        res = format_now_playing_status("🎶", "Asuka", "Hoi Festa - Topic")
        self.assertEqual(res, "🎶 Now Playing: 𝗔𝘀𝘂𝗸𝗮 • 𝗛𝗼𝗶 𝗙𝗲𝘀𝘁𝗮")

        # 4. Avoid duplicating artist if already in title
        res2 = format_now_playing_status("🎵", "Coldplay - Yellow", "Coldplay")
        self.assertEqual(res2, "🎵 Now Playing: 𝗖𝗼𝗹𝗱𝗽𝗹𝗮𝘆 - 𝗬𝗲𝗹𝗹𝗼𝘄")

        # 5. Without artist
        res3 = format_now_playing_status("🎧", "Unknown Beat", "")
        self.assertEqual(res3, "🎧 Now Playing: 𝗨𝗻𝗸𝗻𝗼𝘄𝗻 𝗕𝗲𝗮𝘁")

        # 6. Paused format
        res_pause = format_paused_status("Asuka", "Hoi Festa - Topic")
        self.assertEqual(res_pause, "⏸️ Paused: 𝗔𝘀𝘂𝗸𝗮 • 𝗛𝗼𝗶 𝗙𝗲𝘀𝘁𝗮")

        # 7. Long title is clamped to <= 100
        long_title = "A" * 120
        res4 = format_now_playing_status("🎵", long_title, "Artist")
        self.assertLessEqual(len(res4), 100)
        self.assertTrue(res4.endswith("..."))

    def test_extract_youtube_video_id(self):
        """Test extract_youtube_video_id correctly extracts 11-char IDs from various formats."""
        from engine.discord_bot import extract_youtube_video_id

        self.assertEqual(extract_youtube_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(extract_youtube_video_id("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(extract_youtube_video_id("https://music.youtube.com/watch?v=dQw4w9WgXcQ&feat=1"), "dQw4w9WgXcQ")
        self.assertEqual(extract_youtube_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(extract_youtube_video_id("dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertIsNone(extract_youtube_video_id("random search query"))

    def test_find_cached_track_and_lru_pruning(self):
        """Test cached track retrieval and LRU size pruning."""
        import tempfile, os, json
        from engine.discord_bot import find_cached_track, save_track_cache_meta, prune_audio_cache

        with tempfile.TemporaryDirectory() as tmpdir:
            vid = "testvideo12"
            audio_file = os.path.join(tmpdir, f"{vid}.opus")
            with open(audio_file, "wb") as f:
                f.write(b"0" * 2048)  # > 1KB

            meta = {
                "title": "Test Song",
                "uploader": "Test Artist",
                "duration_sec": 120,
                "duration_str": "2:00",
                "webpage_url": f"https://www.youtube.com/watch?v={vid}",
                "video_id": vid,
            }
            save_track_cache_meta(tmpdir, vid, meta)

            found_f, found_m = find_cached_track(tmpdir, vid)
            self.assertEqual(found_f, audio_file)
            self.assertEqual(found_m.get("title"), "Test Song")

            # Create 3 files and test LRU pruning with max_files=2
            f2 = os.path.join(tmpdir, "vid2xxxxxxx.opus")
            f3 = os.path.join(tmpdir, "vid3xxxxxxx.opus")
            with open(f2, "wb") as f:
                f.write(b"0" * 2048)
            with open(f3, "wb") as f:
                f.write(b"0" * 2048)
            save_track_cache_meta(tmpdir, "vid2xxxxxxx", {"title": "Song 2"})
            save_track_cache_meta(tmpdir, "vid3xxxxxxx", {"title": "Song 3"})

            # Make f2 and f3 newer
            now = os.path.getmtime(audio_file)
            os.utime(f2, (now + 10, now + 10))
            os.utime(f3, (now + 20, now + 20))

            # Prune with max_files=2
            prune_audio_cache(tmpdir, max_bytes=10 * 1024 * 1024, max_files=2)
            # Oldest file (audio_file) should be removed
            self.assertFalse(os.path.exists(audio_file))
            self.assertFalse(os.path.exists(os.path.join(tmpdir, f"{vid}.json")))
            self.assertTrue(os.path.exists(f3))

    def test_async_enqueue_instant_cache_hit(self):
        """Test _async_enqueue_or_play returns immediately on instant cache hit without yt-dlp."""
        import asyncio, tempfile, os
        from unittest.mock import patch, MagicMock
        from engine.discord_bot import DiscordVoiceBot, save_track_cache_meta

        with tempfile.TemporaryDirectory() as tmpdir:
            vid = "quickhit123"
            cached_audio = os.path.join(tmpdir, f"{vid}.webm")
            with open(cached_audio, "wb") as f:
                f.write(b"x" * 2048)
            save_track_cache_meta(tmpdir, vid, {
                "title": "Instant Hit Song",
                "uploader": "Cached Artist",
                "duration_sec": 180,
                "duration_str": "3:00",
                "webpage_url": f"https://www.youtube.com/watch?v={vid}",
                "video_id": vid,
            })

            bot = DiscordVoiceBot(is_local=False)
            bot._async_play_track = MagicMock()
            async def _dummy_play(t):
                pass
            bot._async_play_track.side_effect = _dummy_play

            # Mock AUDIO_CACHE_DIR to use our tmpdir
            with patch("engine.discord_bot.AUDIO_CACHE_DIR", tmpdir), \
                 patch("os.path.abspath", side_effect=lambda p: tmpdir if "cache" in p else p):
                success, msg, is_queued, track = asyncio.run(
                    bot._async_enqueue_or_play(f"https://www.youtube.com/watch?v={vid}", requester="Tester")
                )

                self.assertTrue(success)
                self.assertFalse(is_queued)
                self.assertEqual(track["title"], "Instant Hit Song")
                self.assertEqual(track["filepath"], cached_audio)

    def test_load_env_file_and_cache_limits(self):
        """Test load_env_file loads MAX_CACHE_MB and MAX_CACHE_FILES from .env into get_cache_limits."""
        import tempfile, os
        from unittest.mock import patch
        from engine.discord_bot import get_cache_limits, load_env_file

        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = os.path.join(tmpdir, ".env")
            with open(env_file, "w", encoding="utf-8") as f:
                f.write("MAX_CACHE_MB=350\nMAX_CACHE_FILES=25\n")

            with patch("engine.discord_bot.ENV_PATH", env_file), \
                 patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MAX_CACHE_MB", None)
                os.environ.pop("MAX_CACHE_FILES", None)

                load_env_file()
                max_bytes, max_files = get_cache_limits()

                self.assertEqual(max_bytes, 350 * 1024 * 1024)
                self.assertEqual(max_files, 25)

    def test_buffered_audio_source_lifecycle(self):
        """Verify BufferedAudioSource buffers audio frames, proxies properties, and terminates on EOF."""
        import discord

        class MockSource(discord.AudioSource):
            def __init__(self, count=15):
                self.count = count
                self.cleaned = False
                self._process = "mock_proc"
                self._current_error = None

            def read(self):
                if self.count <= 0:
                    return b""
                self.count -= 1
                return b"A" * 3840

            def cleanup(self):
                self.cleaned = True

        mock_src = MockSource(15)
        buffered = BufferedAudioSource(mock_src, buffer_size=20)
        self.assertEqual(buffered._process, "mock_proc")
        self.assertFalse(buffered.is_opus())

        frames = []
        while True:
            frame = buffered.read()
            if not frame:
                break
            frames.append(frame)

        self.assertEqual(len(frames), 15)
        self.assertEqual(frames[0], b"A" * 3840)
        buffered.cleanup()
        self.assertTrue(mock_src.cleaned)

    def test_buffered_audio_source_with_volume_transformer(self):
        """Verify BufferedAudioSource integrates seamlessly with discord.PCMVolumeTransformer."""
        import discord

        class MockSource(discord.AudioSource):
            def __init__(self):
                self.frames = [b"\x10\x00" * 1920, b""]
            def read(self):
                return self.frames.pop(0) if self.frames else b""
            def cleanup(self):
                pass

        buffered = BufferedAudioSource(MockSource(), buffer_size=10)
        transformer = discord.PCMVolumeTransformer(buffered, volume=0.5)
        out = transformer.read()
        self.assertEqual(len(out), 3840)
        transformer.cleanup()

    def test_buffered_audio_source_underrun_silence(self):
        """Verify BufferedAudioSource returns silence frames on temporary starvation without premature EOF."""
        import discord

        class StarvedSource(discord.AudioSource):
            def __init__(self):
                self.read_calls = 0
            def read(self):
                self.read_calls += 1
                import time
                time.sleep(0.08)
                return b"B" * 3840 if self.read_calls <= 2 else b""

        buffered = BufferedAudioSource(StarvedSource(), buffer_size=10)
        frame = buffered.read()
        self.assertEqual(len(frame), 3840)
        buffered.cleanup()

    def test_find_cached_track_by_query(self):
        """Verify find_cached_track_by_query matches cached audio by title/artist and rejects mismatches."""
        import tempfile, json

        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Create a dummy audio file and json metadata
            vid = "TEST_VID_123"
            audio_path = os.path.join(tmpdir, f"{vid}.opus")
            with open(audio_path, "wb") as f:
                f.write(b"OPUS_DATA" * 200)  # > 1024 bytes

            meta = {
                "title": "Asuka Sparkle",
                "uploader": "Hoi Festa",
                "duration_sec": 180,
                "duration_str": "3:00",
                "webpage_url": f"https://www.youtube.com/watch?v={vid}",
                "video_id": vid,
            }
            with open(os.path.join(tmpdir, f"{vid}.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f)

            # Test Exact match
            fpath, m = find_cached_track_by_query(tmpdir, "Asuka Sparkle")
            self.assertEqual(fpath, audio_path)
            self.assertEqual(m["video_id"], vid)

            # Test Token search (artist + title)
            fpath, m = find_cached_track_by_query(tmpdir, "hoi festa asuka")
            self.assertEqual(fpath, audio_path)
            self.assertEqual(m["title"], "Asuka Sparkle")

            # Test ytsearch1: prefix stripping
            fpath, m = find_cached_track_by_query(tmpdir, "ytsearch1:asuka sparkle")
            self.assertEqual(fpath, audio_path)

            # Test Extra query token that doesn't match (e.g. remix) -> should return None to query YouTube
            fpath, m = find_cached_track_by_query(tmpdir, "asuka sparkle remix")
            self.assertIsNone(fpath)
            self.assertIsNone(m)

            # Test Stopwords only -> should return None
            fpath, m = find_cached_track_by_query(tmpdir, "the in on")
            self.assertIsNone(fpath)

            # Test non-existing song -> should return None
            fpath, m = find_cached_track_by_query(tmpdir, "completely unknown song")
            self.assertIsNone(fpath)

    def test_audio_cache_index_ram_operations(self):
        """Verify AudioCacheIndex provides in-memory RAM caching with nanosecond lookups."""
        import tempfile, json

        index = AudioCacheIndex()
        self.assertEqual(len(index._index), 0)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create two dummy cached tracks on disk
            for vid, title in [("VID_A", "Alpha Song"), ("VID_B", "Beta Beat")]:
                af = os.path.join(tmpdir, f"{vid}.opus")
                with open(af, "wb") as f:
                    f.write(b"AUDIO_DATA" * 150)
                meta = {
                    "title": title,
                    "uploader": "Cool Artist",
                    "duration_sec": 200,
                    "duration_str": "3:20",
                    "webpage_url": f"https://www.youtube.com/watch?v={vid}",
                    "video_id": vid,
                }
                with open(os.path.join(tmpdir, f"{vid}.json"), "w", encoding="utf-8") as f:
                    json.dump(meta, f)

            # 1. Sync from disk to RAM
            index.sync_from_disk(tmpdir)
            self.assertEqual(len(index._index), 2)

            # 2. In-Memory RAM Get (O(1))
            fpath, meta = index.get(tmpdir, "VID_A")
            self.assertIsNotNone(fpath)
            self.assertEqual(meta["title"], "Alpha Song")

            # 3. In-Memory RAM Query Search
            fpath, meta = index.search_by_query(tmpdir, "cool artist alpha")
            self.assertIsNotNone(fpath)
            self.assertEqual(meta["video_id"], "VID_A")

            # 4. Put new entry directly to RAM
            index.put(tmpdir, "VID_C", {"title": "Gamma Groove", "uploader": "DJ Gamma"}, "/dummy/path.opus")
            self.assertIn("VID_C", index._index)

            # 5. Remove entry from RAM
            index.remove("VID_C")
            self.assertNotIn("VID_C", index._index)

            # 6. Clear RAM index
            index.clear()
            self.assertEqual(len(index._index), 0)

    def test_smart_auto_leave_logic(self):
        """Verify smart auto-leave properly triggers on empty channel and idle timeouts."""
        bot = DiscordVoiceBot()
        bot.leave_voice_channel = MagicMock()
        bot.is_in_voice = True
        bot.voice_client = MagicMock()
        bot.voice_client.is_connected.return_value = True

        mock_channel = MagicMock()
        mock_channel.name = "General"
        bot.voice_client.channel = mock_channel

        # Test 1: Channel has human member -> not empty
        human_user = MagicMock()
        human_user.bot = False
        mock_channel.members = [human_user]

        bot._empty_since = None
        bot.auto_leave_empty_timeout = 10
        self.assertIsNone(bot._empty_since)

        # Test 2: Channel becomes empty -> exceeds timeout -> leaves
        mock_channel.members = []
        bot._empty_since = time.time() - 15  # 15s ago, exceeds 10s timeout

        now = time.time()
        human_members = [m for m in mock_channel.members if not m.bot]
        if len(human_members) == 0 and (now - bot._empty_since) >= bot.auto_leave_empty_timeout:
            bot.leave_voice_channel()

        bot.leave_voice_channel.assert_called_once()

        # Test 3: Idle playback timeout
        bot.leave_voice_channel.reset_mock()
        bot.is_playing = False
        bot.is_paused = False
        bot.queue = []
        bot.auto_leave_idle_timeout = 20
        bot._idle_since = time.time() - 25  # 25s ago, exceeds 20s timeout

        now = time.time()
        if not bot.is_playing and not bot.is_paused and not bot.queue:
            if (now - bot._idle_since) >= bot.auto_leave_idle_timeout:
                bot.leave_voice_channel()

        bot.leave_voice_channel.assert_called_once()

    def test_audio_cache_index_get_random_track(self):
        """Verify AudioCacheIndex get_random_track behavior."""
        import tempfile
        from engine.discord_bot import AudioCacheIndex

        with tempfile.TemporaryDirectory() as tmpdir:
            index = AudioCacheIndex()
            # 1. Empty index returns None
            self.assertIsNone(index.get_random_track(tmpdir))

            # 2. Add files and entries
            file_a = os.path.join(tmpdir, "VID_1.opus")
            file_b = os.path.join(tmpdir, "VID_2.webm")
            with open(file_a, "wb") as f:
                f.write(b"0" * 2048)
            with open(file_b, "wb") as f:
                f.write(b"0" * 2048)

            index.put(tmpdir, "VID_1", {"title": "Song One", "uploader": "Artist One", "video_id": "VID_1"}, file_a)
            index.put(tmpdir, "VID_2", {"title": "Song Two", "uploader": "Artist Two", "video_id": "VID_2"}, file_b)

            # 3. Random track returns a valid (filepath, meta) pair
            res = index.get_random_track(tmpdir)
            self.assertIsNotNone(res)
            filepath, meta = res
            self.assertIn(meta["video_id"], ["VID_1", "VID_2"])
            self.assertTrue(os.path.exists(filepath))

            # 4. Excluding VID_1 returns VID_2
            res_ex = index.get_random_track(tmpdir, exclude_vid_id="VID_1")
            self.assertIsNotNone(res_ex)
            self.assertEqual(res_ex[1]["video_id"], "VID_2")

    def test_create_track_from_cached_meta(self):
        """Verify track dict creation from cached metadata for autoplay."""
        from engine.discord_bot import create_track_from_cached_meta

        meta = {
            "video_id": "test1234",
            "title": "Cool Song",
            "uploader": "Cool Artist",
            "duration_sec": 185,
            "duration_str": "3:05",
            "webpage_url": "https://www.youtube.com/watch?v=test1234",
        }
        track = create_track_from_cached_meta("/dummy/path/test1234.opus", meta, requester="Autoplay")
        self.assertEqual(track["title"], "Cool Song")
        self.assertEqual(track["uploader"], "Cool Artist")
        self.assertEqual(track["duration_sec"], 185)
        self.assertEqual(track["duration_str"], "3:05")
        self.assertEqual(track["requester"], "Autoplay")
        self.assertIn(track["emoji"], ["🎵", "🎶", "🎧", "✨"])
        self.assertFalse(track["is_stream"])

    def test_autoplay_slash_command_and_bot_state(self):
        """Verify /autoplay slash command registration and bot toggle state."""
        import discord
        from discord.ext import commands
        from engine.discord_bot import DiscordVoiceBot

        bot = DiscordVoiceBot()
        self.assertFalse(bot.autoplay)

        bot.set_autoplay(True)
        self.assertTrue(bot.autoplay)
        bot.set_autoplay(False)
        self.assertFalse(bot.autoplay)

        intents = discord.Intents.default()
        bot.client = commands.Bot(command_prefix="!", intents=intents)
        bot._register_slash_commands()

        cmd = None
        for command in bot.client.tree.get_commands():
            if command.name == "autoplay":
                cmd = command
                break

        self.assertIsNotNone(cmd)
        param = cmd.parameters[0]
        self.assertEqual(param.name, "mode")
        choice_values = [c.value for c in param.choices]
        self.assertIn("on", choice_values)
        self.assertIn("off", choice_values)

        # Verify executing command works and defers properly without attribute errors
        import asyncio
        from unittest.mock import AsyncMock

        mock_interaction = MagicMock()
        mock_interaction.guild = None
        mock_interaction.response.defer = AsyncMock()
        mock_interaction.followup.send = AsyncMock()
        mock_interaction.user.voice = None

        # Test mode="on"
        asyncio.run(cmd.callback(mock_interaction, mode="on"))
        mock_interaction.response.defer.assert_called_once()
        mock_interaction.followup.send.assert_called_once()
        self.assertTrue(bot.autoplay)
        # Verify no cache count mentioned in message
        send_msg = mock_interaction.followup.send.call_args[0][0]
        self.assertNotIn("cached track", send_msg)

        # Test mode="off"
        mock_interaction.response.defer.reset_mock()
        mock_interaction.followup.send.reset_mock()
        asyncio.run(cmd.callback(mock_interaction, mode="off"))
        mock_interaction.response.defer.assert_called_once()
        mock_interaction.followup.send.assert_called_once()
        self.assertFalse(bot.autoplay)

        # Test mode=None (interactive dropdown view)
        mock_interaction.response.defer.reset_mock()
        mock_interaction.followup.send.reset_mock()
        asyncio.run(cmd.callback(mock_interaction, mode=None))
        mock_interaction.response.defer.assert_called_once()
        mock_interaction.followup.send.assert_called_once()
        sent_view = mock_interaction.followup.send.call_args[1].get("view")
        self.assertIsNotNone(sent_view)

    def test_user_queue_priority_over_autoplay(self):
        """Verify user queued tracks take priority when autoplay is enabled."""
        bot = DiscordVoiceBot()
        bot.autoplay = True
        bot.voice_client = MagicMock()
        bot.voice_client.is_connected.return_value = True

        # When queue has tracks, queue track is taken first
        user_track = {"title": "User Song", "requester": "Alice"}
        bot.queue.append(user_track)

        self.assertEqual(len(bot.queue), 1)
        next_track = bot.queue.pop(0)
        self.assertEqual(next_track["title"], "User Song")
        self.assertEqual(next_track["requester"], "Alice")

    def test_manual_stop_playback_flag(self):
        """Verify manual stop sets _manual_stop to prevent autoplay restarting immediately."""
        bot = DiscordVoiceBot()
        bot.autoplay = True
        bot.voice_client = MagicMock()
        bot.voice_client.is_playing.return_value = True

        self.assertFalse(bot._manual_stop)
        bot.stop_playback()
        self.assertTrue(bot._manual_stop)
        self.assertFalse(bot.is_playing)
        self.assertEqual(len(bot.queue), 0)

    def test_resolve_track_metadata_self_healing(self):
        """Verify resolve_track_metadata recovers title and saves JSON when title is raw video_id."""
        import tempfile
        from engine.discord_bot import resolve_track_metadata

        with tempfile.TemporaryDirectory() as tmpdir:
            test_vid = "R_DpYHPoQEU"
            dummy_file = os.path.join(tmpdir, f"{test_vid}.webm")
            with open(dummy_file, "wb") as f:
                f.write(b"0" * 2048)

            # Raw video_id as title triggers resolution
            raw_meta = {"title": test_vid, "video_id": test_vid}
            with patch("engine.discord_bot.fetch_youtube_oembed_meta") as mock_oembed:
                mock_oembed.return_value = {
                    "title": "Drive - Bersama Bintang",
                    "uploader": "Emotion Entertainment",
                    "video_id": test_vid,
                }
                resolved = resolve_track_metadata(tmpdir, test_vid, raw_meta, dummy_file)
                self.assertEqual(resolved["title"], "Drive - Bersama Bintang")
                self.assertEqual(resolved["uploader"], "Emotion Entertainment")
                self.assertEqual(resolved["video_id"], test_vid)

                # Verify JSON was saved on disk
                json_path = os.path.join(tmpdir, f"{test_vid}.json")
                self.assertTrue(os.path.exists(json_path))
                with open(json_path, "r", encoding="utf-8") as jf:
                    saved = json.load(jf)
                self.assertEqual(saved["title"], "Drive - Bersama Bintang")

    def test_create_track_resolves_raw_id(self):
        """Verify create_track_from_cached_meta replaces raw ID title with resolved title."""
        import tempfile
        from engine.discord_bot import create_track_from_cached_meta

        with tempfile.TemporaryDirectory() as tmpdir:
            test_vid = "vid12345678"
            dummy_file = os.path.join(tmpdir, f"{test_vid}.webm")
            with open(dummy_file, "wb") as f:
                f.write(b"0" * 2048)

            with patch("engine.discord_bot.fetch_youtube_oembed_meta") as mock_oembed:
                mock_oembed.return_value = {
                    "title": "Resolved Song Title",
                    "uploader": "Resolved Artist",
                    "video_id": test_vid,
                }
                track = create_track_from_cached_meta(dummy_file, {"title": test_vid, "video_id": test_vid})
                self.assertEqual(track["title"], "Resolved Song Title")
                self.assertEqual(track["uploader"], "Resolved Artist")

    def test_cmd_skip_and_playback_controls_song_link_formatting(self):
        """Verify skip, pause, resume and stop format song titles with markdown links."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        import discord
        from discord.ext import commands

        bot = DiscordVoiceBot()
        intents = discord.Intents.default()
        bot.client = commands.Bot(command_prefix="!", intents=intents)
        bot._register_slash_commands()

        commands_map = {cmd.name: cmd for cmd in bot.client.tree.get_commands()}
        self.assertIn("skip", commands_map)
        self.assertIn("pause", commands_map)
        self.assertIn("resume", commands_map)

        # Setup active playback with track containing webpage_url
        test_track = {
            "title": "Link Song",
            "webpage_url": "https://www.youtube.com/watch?v=linksong123",
        }
        bot.is_playing = True
        bot.current_track = test_track
        bot.current_title = test_track["title"]
        bot.voice_client = MagicMock()
        bot.voice_client.is_playing.return_value = True
        bot.voice_client.is_paused.return_value = False

        # Test cmd_pause
        mock_interaction = MagicMock()
        mock_interaction.guild = None
        mock_interaction.response.send_message = AsyncMock()
        asyncio.run(commands_map["pause"].callback(mock_interaction))
        mock_interaction.response.send_message.assert_called_once()
        pause_content = mock_interaction.response.send_message.call_args[0][0]
        self.assertIn("**[Link Song](<https://www.youtube.com/watch?v=linksong123>)**", pause_content)

        # Test cmd_resume
        bot.voice_client.is_paused.return_value = True
        mock_interaction.response.send_message.reset_mock()
        asyncio.run(commands_map["resume"].callback(mock_interaction))
        mock_interaction.response.send_message.assert_called_once()
        resume_content = mock_interaction.response.send_message.call_args[0][0]
        self.assertIn("**[Link Song](<https://www.youtube.com/watch?v=linksong123>)**", resume_content)

        # Test cmd_skip
        bot.is_playing = True
        mock_interaction.response.send_message.reset_mock()
        asyncio.run(commands_map["skip"].callback(mock_interaction))
        mock_interaction.response.send_message.assert_called_once()
        skip_content = mock_interaction.response.send_message.call_args[0][0]
        self.assertIn("**[Link Song](<https://www.youtube.com/watch?v=linksong123>)**", skip_content)

    def test_skip_advances_queue_and_sets_manual_skip(self):
        """Verify skip sets _manual_skip and advances queue cleanly."""
        bot = DiscordVoiceBot()
        bot.is_playing = True
        bot.current_title = "Song 1"
        bot.current_track = {"title": "Song 1", "webpage_url": "https://youtube.com/watch?v=s1"}
        bot.voice_client = MagicMock()
        bot.voice_client.is_playing.return_value = True

        # When skip is called, voice_client.stop() is executed and _manual_skip is True
        skipped = bot.skip()
        self.assertEqual(skipped, "Song 1")
        self.assertTrue(bot._manual_skip)
        bot.voice_client.stop.assert_called_once()

        # If voice_client is not actively playing, skip advances queue directly
        bot.voice_client.is_playing.return_value = False
        bot.voice_client.is_paused.return_value = False
        bot.voice_client.is_connected.return_value = True
        bot._loop = MagicMock()
        bot._loop.is_running.return_value = True
        bot.queue = [{"title": "Song 2", "webpage_url": "https://youtube.com/watch?v=s2"}]
        bot._async_play_track = AsyncMock()

        skipped2 = bot.skip()
        self.assertEqual(skipped2, "Song 1")
        self.assertEqual(len(bot.queue), 0)

    def test_format_song_link_and_query_fallback(self):
        """Verify format_song_link creates valid clickable links and handles queries cleanly."""
        from engine.discord_bot import format_song_link

        # Standard YouTube URL
        self.assertEqual(
            format_song_link("The Rare Occasions - Notion", "https://www.youtube.com/watch?v=PD1EXJScA6k"),
            "**[The Rare Occasions - Notion](<https://www.youtube.com/watch?v=PD1EXJScA6k>)**",
        )

        # YouTube URL with search/share params (&pp=...)
        self.assertEqual(
            format_song_link("Notion", "https://www.youtube.com/watch?v=PD1EXJScA6k&pp=ygUbVGhlIFJhcmUgT2NjYXNpb25zIC0gTm90aW9u"),
            "**[Notion](<https://www.youtube.com/watch?v=PD1EXJScA6k&pp=ygUbVGhlIFJhcmUgT2NjYXNpb25zIC0gTm90aW9u>)**",
        )

        # Raw 11-char video ID string
        self.assertEqual(
            format_song_link("Notion", "PD1EXJScA6k"),
            "**[Notion](<https://www.youtube.com/watch?v=PD1EXJScA6k>)**",
        )

        # Plain search query (not an HTTP URL or video ID)
        self.assertEqual(
            format_song_link("The Rare Occasions - Notion", "notion rare occassion"),
            "**The Rare Occasions - Notion**",
        )

        # Empty or None URL
        self.assertEqual(
            format_song_link("The Rare Occasions - Notion", ""),
            "**The Rare Occasions - Notion**",
        )

    def test_cached_meta_sanitization(self):
        """Verify create_track_from_cached_meta never stores non-HTTP query text in webpage_url."""
        from engine.discord_bot import create_track_from_cached_meta

        corrupt_meta = {
            "title": "The Rare Occasions - Notion",
            "video_id": "PD1EXJScA6k",
            "webpage_url": "notion rare occassion",
            "duration_sec": 195,
        }
        track = create_track_from_cached_meta("cache/PD1EXJScA6k.webm", corrupt_meta)
        self.assertEqual(track["webpage_url"], "https://www.youtube.com/watch?v=PD1EXJScA6k")

    def test_skip_with_autoplay_shows_next_song(self):
        """Verify that skipping when autoplay is on displays 'Now playing <song>' for the chosen cached song."""
        from unittest.mock import AsyncMock, MagicMock, patch
        import asyncio
        import discord
        from discord.ext import commands

        bot = DiscordVoiceBot()
        intents = discord.Intents.default()
        bot.client = commands.Bot(command_prefix="!", intents=intents)
        bot._register_slash_commands()

        commands_map = {cmd.name: cmd for cmd in bot.client.tree.get_commands()}
        self.assertIn("skip", commands_map)

        bot.is_playing = True
        bot.autoplay = True
        bot.current_track = {
            "title": "Current Song",
            "webpage_url": "https://www.youtube.com/watch?v=curr123",
        }
        bot.current_title = "Current Song"
        bot.voice_client = MagicMock()
        bot.voice_client.is_playing.return_value = True
        bot.voice_client.is_paused.return_value = False
        bot.voice_client.is_connected.return_value = True
        bot.queue = []

        fake_cached = (
            "cache/testvid123.webm",
            {
                "title": "Autoplay Song",
                "webpage_url": "https://www.youtube.com/watch?v=testvid123",
                "uploader": "Test Artist",
                "duration_str": "3:30",
            },
        )

        with patch("engine.discord_bot.AUDIO_CACHE_INDEX.get_random_track", return_value=fake_cached):
            mock_interaction = MagicMock()
            mock_interaction.guild = None
            mock_interaction.response.send_message = AsyncMock()

            asyncio.run(commands_map["skip"].callback(mock_interaction))

            mock_interaction.response.send_message.assert_called_once()
            msg_content = mock_interaction.response.send_message.call_args[0][0]

            self.assertIn("Skipped **[Current Song](<https://www.youtube.com/watch?v=curr123>)**", msg_content)
            self.assertIn("Now playing **[Autoplay Song](<https://www.youtube.com/watch?v=testvid123>)**", msg_content)
            self.assertIn("by **Test Artist**", msg_content)
            self.assertIn("(`3:30`)", msg_content)

    def test_autoplay_triggers_on_natural_song_completion(self):
        """Verify when a song completes naturally with autoplay enabled, next random track is played."""
        bot = DiscordVoiceBot(is_local=False)
        bot.autoplay = True
        bot._loop = MagicMock()
        bot._loop.is_running.return_value = True
        bot.voice_client = MagicMock()
        bot.voice_client.is_connected.return_value = True
        bot.queue = []

        fake_cached = (
            "cache/next_rand_vid.webm",
            {
                "title": "Next Random Song",
                "webpage_url": "https://www.youtube.com/watch?v=next_rand_vid",
                "uploader": "Random Artist",
                "duration_str": "4:00",
                "video_id": "next_rand_vid",
            },
        )

        current_track = {
            "title": "Finished Song",
            "webpage_url": "https://www.youtube.com/watch?v=finished_vid",
            "video_id": "finished_vid",
            "is_stream": False,
        }

        captured_after = None
        def mock_play(source, after=None):
            nonlocal captured_after
            captured_after = after

        bot.voice_client.play = mock_play

        with patch("engine.discord_bot.get_ffmpeg_binary", return_value="ffmpeg"), \
             patch("engine.discord_bot.discord.FFmpegPCMAudio"), \
             patch("engine.discord_bot.BufferedAudioSource"), \
             patch("engine.discord_bot.discord.PCMVolumeTransformer"):
            asyncio.run(bot._async_play_track(current_track))

        mock_channel = MagicMock()
        mock_channel.send = AsyncMock()
        bot.last_text_channel = mock_channel

        with patch("engine.discord_bot.AUDIO_CACHE_INDEX.get_random_track", return_value=fake_cached) as mock_get_rand, \
             patch("engine.discord_bot.asyncio.run_coroutine_threadsafe") as mock_run_coro:
            captured_after(None)
            mock_get_rand.assert_called_once()
            mock_run_coro.assert_called_once()

        # Now test that playing with announce=True actually sends the Now Playing message to text channel
        with patch("engine.discord_bot.get_ffmpeg_binary", return_value="ffmpeg"), \
             patch("engine.discord_bot.discord.FFmpegPCMAudio"), \
             patch("engine.discord_bot.BufferedAudioSource"), \
             patch("engine.discord_bot.discord.PCMVolumeTransformer"):
            next_track = {
                "title": "Next Random Song",
                "webpage_url": "https://www.youtube.com/watch?v=next_rand_vid",
                "uploader": "Random Artist",
                "duration_str": "4:00",
                "video_id": "next_rand_vid",
                "is_stream": False,
            }
            asyncio.run(bot._async_play_track(next_track, announce=True))

            mock_channel.send.assert_awaited_once()
            sent_msg = mock_channel.send.call_args[0][0]
            self.assertIn("Now playing **[Next Random Song](<https://www.youtube.com/watch?v=next_rand_vid>)**", sent_msg)
            self.assertIn("by **Random Artist**", sent_msg)
            self.assertIn("(`4:00`)", sent_msg)


if __name__ == "__main__":
    unittest.main()
