"""
Security and process isolation utilities for Woeyyy Discord Bot:
- Single-instance enforcement using Windows named mutex
- File permission hardening
- Sensitive token masking
- Input sanitization against SSRF and invalid schemes
"""

import ctypes
import os
import re
import subprocess
import sys
from ctypes import wintypes
from typing import Tuple
from urllib.parse import urlparse

MUTEX_NAME = "Local\\Woeyyy_Discord_Bot_SingleInstance_Mutex"


class SingleInstanceLock:
    """Single-instance guard using a Windows named mutex."""

    def __init__(self, mutex_name: str = MUTEX_NAME):
        self.mutex_name = mutex_name
        self._handle = None
        self.is_locked = False

    def acquire(self) -> bool:
        """
        Attempt to acquire the single-instance lock.
        Returns True if this process is the first/only instance.
        Returns False if another instance is already running.
        """
        if sys.platform != "win32":
            # Fallback for non-windows environments
            return True

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            CreateMutexW = kernel32.CreateMutexW
            CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
            CreateMutexW.restype = wintypes.HANDLE

            # ERROR_ALREADY_EXISTS = 183
            self._handle = CreateMutexW(None, False, self.mutex_name)
            last_err = ctypes.get_last_error()

            if self._handle and last_err == 183:
                # Mutex exists: another instance is running
                self.is_locked = False
                return False

            if self._handle:
                self.is_locked = True
                return True

            return False
        except Exception as e:
            print(f"[Security] Warning: Could not create single-instance mutex: {e}")
            return True

    def release(self):
        """Release the mutex handle upon clean exit."""
        if self._handle and sys.platform == "win32":
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
            self.is_locked = False

    @staticmethod
    def focus_existing_window(title_keyword: str = "Woeyyy") -> bool:
        """
        Find an existing window containing title_keyword and bring it to the foreground.
        """
        if sys.platform != "win32":
            return False

        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            found_hwnd = None

            def _enum_windows_cb(hwnd, _):
                nonlocal found_hwnd
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        if title_keyword.lower() in buf.value.lower():
                            found_hwnd = hwnd
                            return False
                return True

            user32.EnumWindows(WNDENUMPROC(_enum_windows_cb), 0)

            if found_hwnd:
                # SW_RESTORE = 9
                user32.ShowWindow(found_hwnd, 9)
                user32.SetForegroundWindow(found_hwnd)
                return True
        except Exception:
            pass
        return False


def secure_file_permissions(filepath: str):
    """
    Harden file permissions on Windows so only the current user profile
    can read/write the file, stripping inherited permissions from other accounts.
    """
    if not os.path.exists(filepath):
        return

    if sys.platform == "win32":
        username = os.environ.get("USERNAME")
        if username:
            try:
                cmd = ["icacls", os.path.abspath(filepath), "/inheritance:r", "/grant:r", f"{username}:(R,W)"]
                subprocess.run(cmd, capture_output=True, text=True, check=False)
            except Exception:
                pass
    else:
        try:
            os.chmod(filepath, 0o600)
        except Exception:
            pass


def mask_token(token: str) -> str:
    """
    Mask a bot token for secure logging or console display.
    Example: my_secret_token_value_123 -> my_se...123
    """
    if not token:
        return ""
    clean = token.strip()
    if len(clean) <= 10:
        return "********"
    return f"{clean[:5]}...{clean[-3:]}"


# Dangerous URL protocols that must NEVER be passed to yt-dlp / FFmpeg
DISALLOWED_SCHEMES = frozenset(["file", "ftp", "smb", "gopher", "data", "pipe", "concat", "hls+file"])

# Private / localhost IPv4 & IPv6 patterns for SSRF defense
PRIVATE_HOST_PATTERNS = [
    re.compile(r"^localhost$", re.I),
    re.compile(r"^127\.\d+\.\d+\.\d+$"),
    re.compile(r"^10\.\d+\.\d+\.\d+$"),
    re.compile(r"^172\.(1[6-9]|2\d|3[0-1])\.\d+\.\d+$"),
    re.compile(r"^192\.168\.\d+\.\d+$"),
    re.compile(r"^169\.254\.\d+\.\d+$"),  # Link-local & cloud metadata service
    re.compile(r"^0\.0\.0\.0$"),
    re.compile(r"^::1$"),
    re.compile(r"^fc00:", re.I),
    re.compile(r"^fe80:", re.I),
]


def sanitize_audio_target(query_or_url: str) -> Tuple[bool, str, str]:
    """
    Sanitizes user input intended for yt-dlp or audio streaming.
    Guards against:
    - SSRF attacks targeting localhost or AWS/GCP metadata endpoints (169.254.169.254)
    - Local File Inclusion (file://, pipe:, concat:)
    - Protocol injections

    Returns:
        (is_safe, sanitized_target, reason)
    """
    clean = query_or_url.strip()
    if not clean:
        return False, "", "Input is empty"

    # Check for URI scheme
    parsed = urlparse(clean)
    scheme = parsed.scheme.lower()

    if scheme:
        # Scheme provided: must strictly be http or https
        if scheme in DISALLOWED_SCHEMES or scheme not in ("http", "https"):
            return False, "", f"Prohibited protocol scheme: '{scheme}'"

        hostname = (parsed.hostname or "").lower()
        if not hostname:
            return False, "", "Missing hostname in URL"

        # Check for SSRF against private addresses
        for pat in PRIVATE_HOST_PATTERNS:
            if pat.match(hostname):
                return False, "", f"Access to private/local network address '{hostname}' is blocked"

        # Safe HTTP/HTTPS URL
        return True, clean, "Safe URL"

    # No scheme: plain search query (e.g. "Coldplay Yellow")
    # Disallow control characters
    sanitized = re.sub(r"[\r\n\x00-\x1f\x7f]", "", clean)
    return True, sanitized, "Search Query"
