# Decky Recorder (GPU Screen Recorder)

[Español](README.es.md)

Lightweight game recording and instant replay for [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader),
powered by [GPU Screen Recorder](https://git.dec05eba.com/gpu-screen-recorder/about/).

- **Smooth video:** captures at a constant 30, 60, 72, 90, 100, 120 or 144 fps (CFR), so the video doesn't stutter
  when the game's frame rate varies.
- **Light on performance:** KMS capture of the screen composed by gamescope, encoded on the GPU (VA-API).
- **Instant replay:** keep the last 5–300 seconds in memory and save them as a clip.
- H.264 or HEVC, optional HDR (HEVC 10-bit), game audio and microphone.
- Videos are saved in `~/Videos/Decky Recorder`, and you can play or delete them from the panel
  (playback opens in Steam's browser; H.264 is recommended, HEVC/HDR may not play there).

## Install

Download `decky-recorder-vX.Y.Z.zip` from Releases and install it from Decky's developer
settings ("Install plugin from ZIP").

## How it works

The ZIP bundles the GPU Screen Recorder AppImage. On first load the plugin extracts it. The
plugin's backend runs as root (Decky's `root` flag) and starts GPU Screen Recorder as your user
with only the `CAP_SYS_ADMIN` capability (via `setpriv`), which its KMS capture helper inherits,
so there is no password prompt. The plugin only starts and stops it and sends it signals.

## License

MIT. GPU Screen Recorder is GPL-3.0; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
