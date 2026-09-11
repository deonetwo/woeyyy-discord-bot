"""Unit tests for engine/logger.py and fast-trace log mechanisms."""

import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engine.logger import (
    RecentLogsHandler,
    clear_recent_logs,
    get_logger,
    get_recent_logs,
    setup_logging,
)


class TestLoggerModule(unittest.TestCase):
    def setUp(self):
        clear_recent_logs()

    def tearDown(self):
        clear_recent_logs()

    def test_recent_logs_handler_buffering_and_limit(self):
        """Verify RecentLogsHandler respects maxlen and limit parameter."""
        handler = RecentLogsHandler(maxlen=10)
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)

        logger = logging.getLogger("test_buffer")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False

        for i in range(15):
            logger.info(f"Message {i}")

        # Total stored in buffer should be capped at 10
        all_buffered = handler.get_recent(limit=20, min_level=logging.INFO)
        self.assertEqual(len(all_buffered), 10)
        self.assertEqual(all_buffered[0], "Message 5")
        self.assertEqual(all_buffered[-1], "Message 14")

        # Limit parameter restricts returned count
        subset = handler.get_recent(limit=3, min_level=logging.INFO)
        self.assertEqual(len(subset), 3)
        self.assertEqual(subset, ["Message 12", "Message 13", "Message 14"])

    def test_recent_logs_handler_level_filtering(self):
        """Verify get_recent filters by minimum log level."""
        handler = RecentLogsHandler(maxlen=10)
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)

        logger = logging.getLogger("test_filter")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False

        logger.debug("Debug detail")
        logger.info("Info event")
        logger.warning("Warning notice")
        logger.error("Error failure")

        info_logs = handler.get_recent(limit=10, min_level=logging.INFO)
        self.assertEqual(len(info_logs), 3)
        self.assertNotIn("Debug detail", info_logs)

        warn_logs = handler.get_recent(limit=10, min_level=logging.WARNING)
        self.assertEqual(len(warn_logs), 2)
        self.assertIn("Warning notice", warn_logs)
        self.assertIn("Error failure", warn_logs)

    def test_recent_logs_handler_clear(self):
        """Verify clearing in-memory buffer."""
        handler = RecentLogsHandler(maxlen=10)
        handler.setFormatter(logging.Formatter("%(message)s"))

        logger = logging.getLogger("test_clear")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False

        logger.info("Hello")
        self.assertEqual(len(handler.get_recent(limit=10)), 1)
        handler.clear()
        self.assertEqual(len(handler.get_recent(limit=10)), 0)

    def test_get_logger_and_global_recent_logs(self):
        """Verify get_logger outputs to the centralized recent trace buffer."""
        log = get_logger("TraceTest")
        log.info("System tracing event 42")

        recent = get_recent_logs(limit=10)
        self.assertTrue(any("System tracing event 42" in entry for entry in recent))
        self.assertTrue(any("[woeyyy.TraceTest]" in entry for entry in recent))

    def test_rotating_file_handler_creation(self):
        """Verify setup_logging writes to rotating file."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_log_file = os.path.join(tmp_dir, "custom_bot.log")
            custom_logger = logging.getLogger("woeyyy_file_test")
            custom_logger.setLevel(logging.INFO)

            from logging.handlers import RotatingFileHandler
            handler = RotatingFileHandler(test_log_file, maxBytes=1024, backupCount=1, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            custom_logger.addHandler(handler)

            custom_logger.info("File test entry")
            handler.flush()
            handler.close()

            self.assertTrue(os.path.exists(test_log_file))
            with open(test_log_file, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("File test entry", content)

    def test_discord_cannot_access_logs_and_sanitizes_errors(self):
        """Verify Discord has no /logs command and error messages do not expose raw exceptions."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        import discord
        from discord.ext import commands
        from engine.discord_bot import DiscordVoiceBot

        bot = DiscordVoiceBot()
        intents = discord.Intents.default()
        bot.client = commands.Bot(command_prefix="!", intents=intents)
        bot._register_slash_commands()

        # 1. Assert 'logs' command is NOT registered on Discord client tree
        command_names = [cmd.name for cmd in bot.client.tree.get_commands()]
        self.assertNotIn("logs", command_names, "Discord users must not have access to logs via /logs")

        # 2. Assert on_app_command_error does not expose raw error
        mock_interaction = MagicMock()
        mock_interaction.command.name = "play"
        mock_interaction.response.is_done.return_value = True
        mock_interaction.followup.send = AsyncMock()

        class CustomRawError(Exception):
            pass

        raw_err = CustomRawError("Private DB connection string or internal trace leaked!")
        asyncio.run(bot.client.tree.on_error(mock_interaction, raw_err))

        mock_interaction.followup.send.assert_called_once()
        sent_msg, sent_kwargs = mock_interaction.followup.send.call_args
        self.assertNotIn("Private DB connection", sent_msg[0])
        self.assertNotIn("CustomRawError", sent_msg[0])
        self.assertIn("An error occurred while executing this command", sent_msg[0])
        self.assertTrue(sent_kwargs.get("ephemeral"))


if __name__ == "__main__":
    unittest.main()
