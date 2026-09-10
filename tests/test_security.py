"""Unit tests for engine/security.py."""

import unittest
from engine.security import (
    SingleInstanceLock,
    mask_token,
    sanitize_audio_target,
)


class TestSecurityModule(unittest.TestCase):
    def test_single_instance_lock_mutex(self):
        """Verify Windows Kernel Named Mutex prevents duplicate instances."""
        lock1 = SingleInstanceLock("Local\\Woeyyy_UnitTest_Mutex")
        acquired1 = lock1.acquire()
        self.assertTrue(acquired1, "First instance must successfully acquire lock")

        lock2 = SingleInstanceLock("Local\\Woeyyy_UnitTest_Mutex")
        acquired2 = lock2.acquire()
        self.assertFalse(acquired2, "Second instance must be rejected as already running")

        # Release first instance
        lock1.release()
        lock2.release()

        # Re-acquire should now succeed
        lock3 = SingleInstanceLock("Local\\Woeyyy_UnitTest_Mutex")
        acquired3 = lock3.acquire()
        self.assertTrue(acquired3, "Lock can be acquired again after release")
        lock3.release()

    def test_mask_token(self):
        """Verify sensitive bot tokens are properly masked."""
        self.assertEqual(mask_token(""), "")
        self.assertEqual(mask_token("short"), "********")
        token = "SAMPLE_BOT_TOKEN_SECRET_999"
        masked = mask_token(token)
        self.assertTrue(masked.startswith("SAMPL..."))
        self.assertTrue(masked.endswith("999"))
        self.assertNotIn("SECRET", masked)

    def test_sanitize_audio_target_valid(self):
        """Verify safe URLs and queries pass sanitization."""
        # Safe queries
        ok, res, _ = sanitize_audio_target("Coldplay - Yellow")
        self.assertTrue(ok)
        self.assertEqual(res, "Coldplay - Yellow")

        # Safe YouTube URL
        ok, res, _ = sanitize_audio_target("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertTrue(ok)

        # Safe YouTube Music URL
        ok, res, _ = sanitize_audio_target("https://music.youtube.com/watch?v=abc123")
        self.assertTrue(ok)

    def test_sanitize_audio_target_dangerous_protocols(self):
        """Verify dangerous schemes are blocked."""
        # Local File Inclusion
        ok, _, reason = sanitize_audio_target("file:///C:/Windows/win.ini")
        self.assertFalse(ok)
        self.assertIn("Prohibited protocol", reason)

        # FTP protocol
        ok, _, reason = sanitize_audio_target("ftp://evil.com/exploit")
        self.assertFalse(ok)

        # Pipe / concat protocol injection
        ok, _, reason = sanitize_audio_target("pipe:0")
        self.assertFalse(ok)

    def test_sanitize_audio_target_ssrf_prevention(self):
        """Verify SSRF attempts to private networks and cloud metadata are blocked."""
        # Localhost
        ok, _, reason = sanitize_audio_target("http://localhost:8080/admin")
        self.assertFalse(ok)
        self.assertIn("blocked", reason)

        # 127.0.0.1
        ok, _, reason = sanitize_audio_target("http://127.0.0.1:3000/keys")
        self.assertFalse(ok)

        # AWS / GCP metadata service
        ok, _, reason = sanitize_audio_target("http://169.254.169.254/latest/meta-data/")
        self.assertFalse(ok)

        # Internal LAN IP
        ok, _, reason = sanitize_audio_target("http://192.168.1.1/router")

if __name__ == "__main__":
    unittest.main()
