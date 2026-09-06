"""
Unit tests for Woeyyy Discord Bot.
Verifies bot controller initialization, configuration storage,
FFmpeg binary presence, and thread lifecycle.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engine.discord_bot import (
    DiscordVoiceBot,
    FFMPEG_EXECUTABLE,
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


if __name__ == "__main__":
    unittest.main()




