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

        # Test empty query autocomplete returns history with clock icon
        choices = asyncio.run(cmd._params["query"].autocomplete(mock_interaction, ""))
        self.assertEqual(len(choices), 1)
        self.assertTrue(choices[0].name.startswith("🕒 "))
        self.assertIn("MsOOJA Channel - Hidamari Official Music Video", choices[0].name)
        self.assertEqual(choices[0].value, "https://youtube.com/watch?v=hidamari")

        bot.clear_user_history(test_uid)


if __name__ == "__main__":
    unittest.main()



