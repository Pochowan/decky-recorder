# Changelog

## [0.3.0] - 2026-11-15

- New option "Fix colors in HDR games": forces gamescope composition (GAMESCOPE_COMPOSITE_FORCE,
  the same switch as Steam's "Force Composite") only while recording, streaming or with instant
  replay on. In HDR games gamescope often sends the game's image straight to the display and the
  color conversion happens in the display hardware, so KMS capture got reddish, dark colors.
  Off by default because it uses a bit more battery. The previous value is restored afterwards,
  and it is left alone if Steam's own option was already on.

## [0.2.0] - 2026-11-14

- PC access: a short local-network address and a 6-digit PIN open a web page with all your
  recordings, to watch or download them from a PC's browser. Session cookie (HttpOnly,
  SameSite=Strict), new PIN every time, 5 wrong PINs lock that IP for 2 minutes and 30 in total
  close the access. It turns itself off after 30 minutes or from the panel, and offers a QR code
  to open the same list on a phone.

## [0.1.0] - 2026-11-12

- First version: start/stop recording, instant replay buffer with "save clip",
  30/60/72/90/100/120/144 fps at a constant frame rate or matching the game (variable), H.264/HEVC, optional HDR (HEVC 10-bit),
  game audio and microphone, auto-start of the replay buffer with each game.
- Recordings gallery in the panel: play videos in Steam's browser (served only on
  127.0.0.1 with a random token, seeking supported) and delete them.
- Live streaming to Twitch, YouTube Live or a custom RTMP/SRT server (H.264 CBR,
  720p/1080p/original, 30/60 fps), optionally saving a local copy. The stream key is
  stored apart from the settings (0600) and masked in every log.
- Share a recording with a QR code: a temporary link on the local network (one video,
  expires after 15 minutes or when the QR window closes) to watch or download it on a phone.
- Recording and encoding by GPU Screen Recorder 6.1.6 (bundled AppImage), run as the
  user with only CAP_SYS_ADMIN, so KMS capture works in Game Mode without a password prompt.
