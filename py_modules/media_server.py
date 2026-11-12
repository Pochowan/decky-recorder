"""
Galería de grabaciones: datos de los vídeos y un servidor HTTP local para verlos.

- mp4_info(): duración y códec leyendo solo la caja `moov` del MP4 (sin ffprobe).
- MediaServer: HTTP en 127.0.0.1 (puerto libre) con un token aleatorio en la ruta.
  Sirve una página con un <video> y el propio fichero con soporte de Range (para
  poder avanzar y retroceder). Solo entrega ficheros de la carpeta de grabaciones,
  por nombre y sin subcarpetas. El frontend la abre en el navegador de Steam.
"""

from __future__ import annotations

import html
import os
import secrets
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

VIDEO_TYPES = {".mp4": "video/mp4", ".mkv": "video/x-matroska"}
MAX_MOOV = 64 * 1024 * 1024
CHUNK = 1024 * 1024


def mp4_info(path: str) -> dict:
    """{"duration": segundos | None, "codec": "h264" | "hevc" | "av1" | None}."""
    out = {"duration": None, "codec": None}
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            pos, data = 0, None
            while pos + 8 <= size:
                f.seek(pos)
                sz, typ = struct.unpack(">I4s", f.read(8))
                if sz == 1:
                    sz = struct.unpack(">Q", f.read(8))[0]
                elif sz == 0:
                    sz = size - pos
                if sz < 8:
                    break
                if typ == b"moov":
                    if sz > MAX_MOOV:
                        return out
                    f.seek(pos)
                    data = f.read(sz)
                    break
                pos += sz
    except OSError:
        return out
    if not data:
        return out  # aún grabando (el moov se escribe al final) o fichero dañado
    i = data.find(b"mvhd")
    if i >= 0 and i + 32 <= len(data):
        version = data[i + 4]
        try:
            if version == 1:
                timescale, duration = struct.unpack(">IQ", data[i + 24:i + 36])
            else:
                timescale, duration = struct.unpack(">II", data[i + 16:i + 24])
            if timescale:
                out["duration"] = round(duration / timescale, 1)
        except struct.error:
            pass
    if b"hvc1" in data or b"hev1" in data:
        out["codec"] = "hevc"
    elif b"avc1" in data:
        out["codec"] = "h264"
    elif b"av01" in data:
        out["codec"] = "av1"
    return out


PLAYER_ERROR = {
    "es": "El navegador de Steam no puede reproducir este vídeo (probablemente HEVC o HDR). "
          "Ábrelo en el modo escritorio o en otro dispositivo.",
    "en": "Steam's browser can't play this video (probably HEVC or HDR). "
          "Open it in Desktop Mode or on another device.",
}

PLAYER_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
html,body{{margin:0;height:100%;background:#000;color:#ddd;font-family:sans-serif;overflow:hidden}}
video{{width:100%;height:100%;object-fit:contain;display:block}}
#e{{display:none;position:absolute;inset:0;align-items:center;justify-content:center;
text-align:center;padding:2em;font-size:1.3em;line-height:1.5}}
</style></head>
<body><video id="v" src="../file/{src}" controls autoplay playsinline></video>
<div id="e">{error}</div>
<script>
const v=document.getElementById('v');
v.addEventListener('error',()=>{{v.style.display='none';document.getElementById('e').style.display='flex';}});
</script></body></html>
"""


class MediaServer:
    def __init__(self, get_dir, logger=None):
        self.get_dir = get_dir  # callable -> carpeta de grabaciones actual
        self.log = logger
        self.token = secrets.token_urlsafe(18)
        self.httpd = None
        self.thread = None

    @property
    def base_url(self) -> str | None:
        if not self.httpd:
            return None
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/{self.token}"

    def player_url(self, name: str, lang: str) -> str | None:
        base = self.base_url
        if not base:
            return None
        return f"{base}/play/{quote(name)}?l={quote(lang)}"

    def start(self):
        if self.httpd:
            return
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # sin ruido en el log de Decky
                pass

            def do_HEAD(self):
                self._handle(head=True)

            def do_GET(self):
                self._handle(head=False)

            def _fail(self, code: int):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _resolve(self, name: str):
                if not name or name != os.path.basename(name) or name.startswith("."):
                    return None
                if os.path.splitext(name)[1].lower() not in VIDEO_TYPES:
                    return None
                path = os.path.join(server.get_dir(), name)
                return path if os.path.isfile(path) else None

            def _handle(self, head: bool):
                url = urlsplit(self.path)
                parts = url.path.split("/")
                if len(parts) != 4 or not secrets.compare_digest(parts[1], server.token):
                    return self._fail(404)
                kind, name = parts[2], unquote(parts[3])
                path = self._resolve(name)
                if not path:
                    return self._fail(404)
                if kind == "play":
                    lang = (parse_qs(url.query).get("l") or ["en"])[0]
                    body = PLAYER_PAGE.format(
                        title=html.escape(name),
                        src=quote(name),
                        error=html.escape(PLAYER_ERROR.get(lang, PLAYER_ERROR["en"])),
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    if not head:
                        self.wfile.write(body)
                    return
                if kind == "file":
                    return self._send_file(path, head)
                return self._fail(404)

            def _send_file(self, path: str, head: bool):
                size = os.path.getsize(path)
                start, end = 0, size - 1
                rng = self.headers.get("Range")
                partial = False
                if rng and rng.startswith("bytes="):
                    try:
                        a, b = rng[6:].split(",")[0].split("-")
                        if a:
                            start = int(a)
                            end = int(b) if b else size - 1
                        else:  # sufijo: los últimos N bytes
                            start = max(0, size - int(b))
                        end = min(end, size - 1)
                        if start > end:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{size}")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        partial = True
                    except ValueError:
                        start, end = 0, size - 1
                length = end - start + 1
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", VIDEO_TYPES[os.path.splitext(path)[1].lower()])
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if head:
                    return
                try:
                    with open(path, "rb") as f:
                        f.seek(start)
                        left = length
                        while left > 0:
                            buf = f.read(min(CHUNK, left))
                            if not buf:
                                break
                            self.wfile.write(buf)
                            left -= len(buf)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # el reproductor cortó la petición al buscar

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="decky-recorder-http", daemon=True)
        self.thread.start()
        if self.log:
            self.log.info(f"Servidor de vídeos en 127.0.0.1:{self.httpd.server_address[1]}")

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
