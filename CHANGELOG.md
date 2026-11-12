# Changelog

## [0.1.0] - 2026-11-12

- First version: start/stop recording, instant replay buffer with "save clip",
  30/60/72/90/100/120/144 fps at a constant frame rate or matching the game (variable), H.264/HEVC, optional HDR (HEVC 10-bit),
  game audio and microphone, auto-start of the replay buffer with each game.
- Recordings gallery in the panel: play videos in Steam's browser (served only on
  127.0.0.1 with a random token, seeking supported) and delete them.
- Recording and encoding by GPU Screen Recorder 6.1.6 (bundled AppImage), run as the
  user with only CAP_SYS_ADMIN, so KMS capture works in Game Mode without a password prompt.
