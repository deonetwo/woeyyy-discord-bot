"""
Woeyyy Discord Bot entry point.
Supports interactive terminal mode and background daemon mode.
"""

import argparse
import os
import signal
import sys
import threading
import time
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from engine.discord_bot import DiscordVoiceBot, load_saved_token, save_token
from engine.security import SingleInstanceLock, mask_token


def status_callback(status: str, detail: str):
    """Print connection and playback status updates."""
    if status in ("ONLINE", "CONNECTED"):
        print(f"\n[INFO] Logged in: {detail}")
    elif status in ("DISCONNECTED", "OFFLINE"):
        print(f"\n[INFO] Disconnected from Discord.")
    elif status == "VOICE_CONNECTED":
        print(f"\n[INFO] Joined voice channel: #{detail}")
    elif status == "VOICE_DISCONNECTED":
        print(f"\n[INFO] Left voice channel ({detail})")
    elif status == "PLAYING":
        print(f"\n[Now Playing] {detail}")
    elif status == "PAUSED":
        print(f"\n[Paused] {detail}")
    elif status == "PLAYBACK_STOPPED":
        print(f"\n[Idle] Voice channel is idle.")
    elif status == "SEARCHING":
        print(f"[*] {detail}")
    elif status == "ERROR":
        print(f"\n[ERROR] {detail}")
    elif status == "ENQUEUED":
        print(f"\n[Queued] {detail}")
    elif status == "QUEUE_UPDATED":
        pass


def print_banner():
    print("================================================================")
    print("   Woeyyy - Discord Bot                                         ")
    print("================================================================\n")


def print_help():
    print("\nAvailable commands:")
    print("  p, play <query/url>   - Play song or YouTube URL in voice channel")
    print("  j, join [channel]     - Join voice channel")
    print("  l, leave              - Leave voice channel")
    print("  s, skip               - Skip current track")
    print("  q, queue              - Show upcoming track queue")
    print("  pause                 - Pause playback")
    print("  resume                - Resume playback")
    print("  stop                  - Stop playback and clear queue")
    print("  v, vol <0-150>        - Set playback volume percentage")
    print("  help                  - Show this help list")
    print("  exit, quit            - Disconnect and exit\n")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Woeyyy - Discord Bot"
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run in headless background daemon mode (systemd / cloud)",
    )
    parser.add_argument(
        "--token",
        type=str,
        default="",
        help="Discord bot token (or set DISCORD_BOT_TOKEN environment variable)",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Run in local direct streaming mode (zero-download, cookies not required)",
    )
    parser.add_argument(
        "--set-token",
        type=str,
        default="",
        help="Save token to .env and exit",
    )
    args = parser.parse_args()

    if args.set_token:
        save_token(args.set_token)
        print(f"Token saved to .env: {mask_token(args.set_token)}")
        sys.exit(0)

    lock = SingleInstanceLock("Local\\Woeyyy_Discord_Bot_SingleInstance_Mutex")
    if not lock.acquire():
        print("\n[ERROR] Another instance of Woeyyy Discord Bot is already running.")
        sys.exit(0)

    bot: Optional[DiscordVoiceBot] = None
    try:
        print_banner()

        is_daemon = args.daemon or (not sys.stdin.isatty())
        is_local = args.local or (os.environ.get("BOT_MODE", "").lower() == "local")

        if is_local:
            print("[INFO] Operating Mode: LOCAL (Direct Stream, Zero-Download, No Cookies)")
        else:
            print("[INFO] Operating Mode: SERVER (Cached Download)")

        token = args.token or os.environ.get("DISCORD_BOT_TOKEN")
        if not token or token.strip() == "YOUR_BOT_TOKEN_HERE":
            token = load_saved_token()

        if not token or token.strip() == "YOUR_BOT_TOKEN_HERE":
            if is_daemon:
                print("[ERROR] No bot token provided.")
                print("Set DISCORD_BOT_TOKEN in .env or pass --token <token>.")
                sys.exit(1)
            else:
                print("No saved bot token found.")
                token = input("Enter Discord Bot Token: ").strip()
                if not token or token == "YOUR_BOT_TOKEN_HERE":
                    print("[ERROR] Token cannot be empty. Exiting.")
                    sys.exit(1)
                save_token(token)
        else:
            print(f"Using bot token: {mask_token(token)}")
            if args.token:
                save_token(args.token)

        bot = DiscordVoiceBot(on_status_change=status_callback, is_local=is_local)
        print("Connecting to Discord gateway...")
        bot.start(token)

        time.sleep(2.5)

        if is_daemon:
            print("[INFO] Bot running in daemon mode.")
            print("[INFO] Listening for slash commands. Press Ctrl+C to terminate.")

            stop_event = threading.Event()

            def _handle_signal(sig, frame):
                print(f"\nReceived signal {sig}, terminating gracefully...")
                stop_event.set()

            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
            stop_event.wait()
            return

        print_help()

        while True:
            try:
                cmd_line = input("woeyyy-bot> ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not cmd_line:
                continue

            parts = cmd_line.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd in ("exit", "quit", "q!"):
                break

            elif cmd == "help":
                print_help()

            elif cmd in ("j", "join"):
                channels = bot.get_available_voice_channels()
                if not channels:
                    print("No voice channels found.")
                    continue

                if not arg:
                    print("\nAvailable Voice Channels:")
                    for idx, (ch_name, ch_id) in enumerate(channels, 1):
                        print(f"  [{idx}] {ch_name} (ID: {ch_id})")
                    choice = input("Enter channel number or name: ").strip()
                    if choice.isdigit() and 1 <= int(choice) <= len(channels):
                        target_id = channels[int(choice) - 1][1]
                        bot.join_voice_channel(target_id)
                    else:
                        match = [cid for name, cid in channels if choice.lower() in name.lower()]
                        if match:
                            bot.join_voice_channel(match[0])
                        else:
                            print("Voice channel not found.")
                else:
                    match = [cid for name, cid in channels if arg.lower() in name.lower()]
                    if match:
                        bot.join_voice_channel(match[0])
                    else:
                        print(f"Voice channel '{arg}' not found.")

            elif cmd in ("l", "leave"):
                if bot.is_in_voice:
                    bot.leave_voice_channel()
                else:
                    print("Bot is not connected to a voice channel.")

            elif cmd in ("p", "play"):
                if not arg:
                    print("Usage: play <title or URL>")
                    continue
                if not bot.is_in_voice:
                    print("Bot is not in a voice channel. Use 'join' first.")
                    continue
                bot.play_music(arg)

            elif cmd in ("s", "skip"):
                skipped = bot.skip()
                if skipped:
                    print(f"Skipped: {skipped}")
                else:
                    print("Nothing currently playing.")

            elif cmd == "pause":
                bot.pause()
                print("Playback paused.")

            elif cmd == "resume":
                bot.resume()
                print("Playback resumed.")

            elif cmd == "stop":
                bot.stop_playback()
                print("Playback stopped.")

            elif cmd in ("q", "queue"):
                q = bot.get_queue()
                if not q:
                    print("Queue is empty.")
                else:
                    print(f"\n--- Queue ({len(q)} tracks) ---")
                    for i, t in enumerate(q, 1):
                        print(f"  {i}. {t['title']} [{t['duration_str']}] ({t['requester']})")
                    print("-----------------------------")

            elif cmd in ("v", "vol", "volume"):
                if not arg:
                    print(f"Current volume: {int(bot.volume * 100)}%")
                else:
                    try:
                        val = float(arg)
                        if val > 1.5:
                            val = val / 100.0
                        val = max(0.0, min(1.5, val))
                        bot.set_volume(val)
                        print(f"Volume set to {int(val * 100)}%")
                    except ValueError:
                        print("Invalid volume. Enter a number between 0 and 150.")

            else:
                print(f"Unknown command '{cmd}'. Type 'help' for available commands.")

    finally:
        if bot is not None:
            print("\nShutting down bot...")
            bot.stop()
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    main()
