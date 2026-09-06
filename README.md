# Woeyyy - Discord Bot

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Discord.py](https://img.shields.io/badge/discord.py-v2.3%2B-5865F2.svg)](https://github.com/Rapptz/discord.py)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Woeyyy is a headless Discord bot built with discord.py and yt-dlp. It runs with low memory overhead (~25MB RAM).

The project is completely headless, with no GUI dependencies or local audio device requirements. It can run continuously on Linux servers (Ubuntu/Debian/systemd) or Windows.

## Features

- **Direct Voice Streaming**: Plays 48kHz stereo Opus audio directly into Discord voice channels.
- **YouTube and YouTube Music**: Supports search queries, regular YouTube links, and `music.youtube.com` URL normalization.
- **Slash Commands & Autocomplete**: Autocomplete suggestions appear in chat when typing `/play`, prioritizing your recent playback history.
- **Stream Auto-Recovery**: Automatically detects premature stream termination and falls back to cached download.
- **Queue Management**: Enqueues tracks, automatically advances to the next song, and supports track skipping.
- **Input Sanitization & Process Isolation**: Single-instance mutex on Windows, SSRF prevention on user-supplied URLs, and safe token storage.
- **Dual Execution Modes**: Interactive CLI mode for manual testing and background daemon mode for production servers.

## Commands

### Slash Commands

| Command | Description |
|---|---|
| `/join` | Connect bot to your voice channel |
| `/play <query/url>` | Play audio or add to queue (supports history autocomplete) |
| `/skip` | Skip the currently playing track |
| `/pause` | Pause playback |
| `/resume` | Resume playback |
| `/queue` | Display current track queue |
| `/clear` | Clear the track queue |
| `/stop` | Stop playback and clear queue |
| `/volume <0-150>` | Set playback volume percentage |
| `/leave` | Disconnect bot from voice channel |

### Terminal CLI Commands

When running interactively (`python main.py`):
- `p, play <query/url>`: Play track in voice channel
- `j, join [channel]`: Connect bot to voice channel
- `l, leave`: Disconnect from voice channel
- `s, skip`: Skip current track
- `q, queue`: Display queue status
- `pause` / `resume`: Pause or resume playback
- `stop`: Stop playback and clear queue
- `v, vol <0-150>`: Set volume percentage
- `help`: Display help message
- `exit`, `quit`: Disconnect and exit

## Discord Developer Portal Setup

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Go to the **Bot** tab, click **Reset Token**, and copy the token.
3. Under **Privileged Gateway Intents**, enable **Message Content Intent** and save changes.
4. Go to **OAuth2 -> URL Generator**:
   - Under **Scopes**, select `bot` and `applications.commands`.
   - Under **Bot Permissions**, select `Send Messages`, `Read Message History`, `Embed Links`, `Connect`, `Speak`, and `Use Voice Activity`.
5. Copy the generated URL, open it in a browser, and invite the bot to your server.

## Installation and Usage

### Windows

#### Prerequisites
- Python 3.10 to 3.14 (ensure Python is added to PATH).

#### Quick Start (Local Direct Stream Mode)
For running on a local PC (instant playback, zero download wait time, no cookies required):
```cmd
run_local.bat
```

#### Quick Start (Standard Server Mode)
```cmd
run.bat
```

#### Manual Setup
```cmd
python -m venv .venv
call .venv\Scripts\activate.bat
pip install -r requirements.txt

# Save bot token
python main.py --set-token "YOUR_BOT_TOKEN"

# Run in local direct streaming mode (zero-download)
python main.py --local

# Or run in standard mode / daemon mode
python main.py
python main.py --daemon
```

### Linux (Ubuntu / Debian / AWS EC2)

#### 1. System Dependencies
```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git ffmpeg libopus0 libopus-dev unzip

# Install Deno (JavaScript runtime for yt-dlp)
curl -fsSL https://deno.land/install.sh | sh
sudo cp ~/.deno/bin/deno /usr/local/bin/
```

#### 2. Project Setup
```bash
git clone https://github.com/deonetwo/woeyyy-discord-bot.git
cd woeyyy-discord-bot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### 3. Token & Cookies Configuration
Create a `.env` file with your bot token:
```bash
echo "DISCORD_BOT_TOKEN=YOUR_BOT_TOKEN" > .env
chmod 600 .env
```

> **Note on Cookies**: Cookies are **optional**. Both streaming and downloading work out of the box in guest mode without cookies. If an optional `cookies.txt` is provided and fails or expires, the bot automatically retries in clean guest mode.

```bash
# Optional: place exported Netscape-format cookies.txt in the project root
chmod 600 cookies.txt
```

#### 4. Running with Systemd
The included `woeyyy-bot.service` loads the bot token directly from `.env` via `EnvironmentFile`:
```bash
# If your user or directory differs from /home/ubuntu/woeyyy-discord-bot, adjust paths in woeyyy-bot.service first
sudo cp woeyyy-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now woeyyy-bot
```

#### 5. Service Management
```bash
sudo systemctl status woeyyy-bot
sudo journalctl -u woeyyy-bot -f
sudo systemctl restart woeyyy-bot
sudo systemctl stop woeyyy-bot
```

## Testing

Run unit tests with Python's built-in test runner:
```bash
python -m unittest discover tests
```

## Project Structure

```text
woeyyy-discord-bot/
├── engine/
│   ├── __init__.py         # Package exports
│   ├── discord_bot.py      # Core Discord voice client & streaming logic
│   └── security.py         # Mutex isolation, token masking, and URL sanitization
├── tests/
│   ├── test_discord_bot.py # Discord bot unit tests
│   └── test_security.py    # Security validation unit tests
├── bot_cli.py              # CLI entry point wrapper
├── main.py                 # Primary entry point
├── requirements.txt        # Python package dependencies
├── run.bat                 # Windows setup and launcher script
├── run_bot.bat             # Windows launcher script
├── run_local.bat           # Windows local streaming launcher script (zero-download)
├── woeyyy-bot.service      # Systemd service unit definition
├── KNOWN_ISSUES.md         # Environment limitations and known issues
└── README.md
```

## Known Issues

For details on known limitations and environment-specific notes (such as local Windows audio playback behavior), see [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

## License
Distributed under the MIT License.
