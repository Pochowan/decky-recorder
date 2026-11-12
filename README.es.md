# Decky Recorder (GPU Screen Recorder)

[English](README.md)

Grabación de partidas y repetición instantánea, ligera, para [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader),
basada en [GPU Screen Recorder](https://git.dec05eba.com/gpu-screen-recorder/about/).

- **Vídeo fluido:** captura a 30, 60, 72, 90, 100, 120 o 144 fps constantes (CFR), así el vídeo no da tirones
  aunque los FPS del juego varíen.
- **Ligero:** captura KMS de la imagen que compone gamescope, codificada en la GPU (VA-API).
- **Repetición instantánea:** guarda en memoria los últimos 5–300 segundos y conviértelos en un clip.
- H.264 o HEVC, HDR opcional (HEVC 10 bits), audio del juego y micrófono.
- Los vídeos se guardan en `~/Videos/Decky Recorder` y puedes verlos o borrarlos desde el panel
  (se abren en el navegador de Steam; se recomienda H.264, HEVC/HDR puede no reproducirse ahí).

## Instalación

Descarga `decky-recorder-vX.Y.Z.zip` de Releases e instálalo desde los ajustes de desarrollador
de Decky ("Instalar plugin desde ZIP").

## Cómo funciona

El ZIP incluye la AppImage de GPU Screen Recorder. Al cargar por primera vez, el plugin la extrae.
El backend del plugin corre como root (flag `root` de Decky) y lanza GPU Screen Recorder como tu
usuario con solo la capacidad `CAP_SYS_ADMIN` (mediante `setpriv`), que hereda su ayudante de
captura KMS, así que no pide contraseña. El plugin solo lo arranca, lo para y le envía señales.

## Licencia

MIT. GPU Screen Recorder es GPL-3.0; consulta [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
