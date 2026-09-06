# Known Issues

This document tracks known limitations and environment-specific behaviors.

---

### Audio Hitching on Windows Local Host (Solitary Acoustic Intros)

#### Symptoms
When running the bot locally on a Windows machine (in either `--local` stream mode or default cached download mode), brief audio stuttering or hitching may occur during the intro of specific tracks that feature isolated, high-transient instruments separated by silence gaps.

- **Example**: The intro (seconds 0–5) of **Ms.OOJA - 「Hidamari」**.
- **Server Deployment**: This does not happen when running on a Linux server or VPS; playback is smooth from the first second.
- **General Tracks**: Tracks with continuous instrumentation (drums, bass, vocals) play without issues across all environments.

#### Suspected Factors
This behavior is limited to running the bot locally on Windows alongside a desktop Discord client. Likely contributing factors:
- **Local Network / UDP Contention**: The local network adapter handles both outgoing UDP packets from the bot and incoming voice packets for the Discord desktop app simultaneously. During silence gaps between notes, queues idle, and sudden note attacks may experience brief local packet contention.
- **Windows Thread Scheduling**: Background process timer resolution and thread scheduling differences on Windows compared to Linux.
- **Discord Client Voice Gating**: Discord's desktop client voice processing (VAD) re-opening its audio gate after silence gaps between isolated notes.

Standard production deployments on Linux servers are unaffected.
