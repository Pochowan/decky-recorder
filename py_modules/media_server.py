"""
Galería de grabaciones: datos de los vídeos y servidores HTTP para verlos y compartirlos.

- mp4_info(): duración y códec leyendo solo la caja `moov` del MP4 (sin ffprobe).
- MediaServer: HTTP con tokens aleatorios en la ruta. Sirve una página con un
  <video>, el fichero con soporte de Range (para avanzar y retroceder) y una
  descarga. Solo entrega ficheros de la carpeta de grabaciones, por nombre y sin
  subcarpetas. Se usa dos veces:
    * local (127.0.0.1, token permanente): reproductor en el navegador de Steam.
    * red local (0.0.0.0, solo mientras se comparte): un token por vídeo que
      caduca, para descargarlo en el móvil escaneando un QR.
"""

from __future__ import annotations

import html
import os
import secrets
import socket
import struct
import threading
import time
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
    if i >= 0 and i + 36 <= len(data):
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


def lan_ip() -> str | None:
    """IP de la interfaz con la ruta por defecto (no envía nada: UDP sin datos)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1: solo para elegir interfaz
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


TEXT = {
    "es": {
        "play_error": "El navegador no puede reproducir este vídeo (probablemente HEVC o HDR). "
                      "Descárgalo y ábrelo con otro reproductor.",
        "download": "Descargar vídeo",
        "share_note": "Enlace temporal de Decky Recorder: deja de funcionar al cerrar el código QR.",
    },
    "en": {
        "play_error": "This browser can't play this video (probably HEVC or HDR). "
                      "Download it and open it with another player.",
        "download": "Download video",
        "share_note": "Temporary Decky Recorder link: it stops working when the QR code is closed.",
    },
}

PAGE = """<!doctype html>
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

SHARE_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
body{{margin:0;background:#111;color:#eee;font-family:system-ui,sans-serif;padding:16px}}
h1{{font-size:1.1em;word-break:break-word;margin:0 0 12px}}
video{{width:100%;max-height:60vh;background:#000;border-radius:8px}}
a.b{{display:block;margin:16px 0;padding:14px;border-radius:10px;background:#1a9fff;color:#fff;
text-align:center;text-decoration:none;font-weight:600;font-size:1.1em}}
p{{color:#999;font-size:.9em}}
</style></head>
<body><h1>{title}</h1>
<video src="../file/{src}" controls playsinline preload="metadata"></video>
<a class="b" href="../dl/{src}" download="{attr}">{download}</a>
<p>{size} · {note}</p>
</body></html>
"""


def _fmt_size(n: int) -> str:
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.1f} MB"


class MediaServer:
    """Servidor HTTP de vídeos. Rutas: /<token>/<play|file|share|dl>/<nombre>."""

    def __init__(self, get_dir, host="127.0.0.1", logger=None):
        self.get_dir = get_dir  # callable -> carpeta de grabaciones actual
        self.host = host
        self.log = logger
        self.tokens: dict = {}  # token -> {"name": str | None, "expires": float | None}
        self.lock = threading.Lock()
        self.httpd = None
        self.thread = None

    # ---- tokens ------------------------------------------------------------

    def add_token(self, name: str | None = None, ttl: float | None = None) -> str:
        tok = secrets.token_urlsafe(18)
        with self.lock:
            self.tokens[tok] = {"name": name, "expires": time.time() + ttl if ttl else None}
        return tok

    def revoke(self, tok: str | None = None):
        with self.lock:
            if tok is None:
                self.tokens.clear()
            else:
                self.tokens.pop(tok, None)

    def _check(self, tok: str, name: str) -> bool:
        now = time.time()
        with self.lock:
            for t, info in list(self.tokens.items()):
                if info["expires"] and info["expires"] < now:
                    del self.tokens[t]
            for t, info in self.tokens.items():
                if secrets.compare_digest(t, tok):
                    return info["name"] is None or info["name"] == name
        return False

    def has_tokens(self) -> bool:
        now = time.time()
        with self.lock:
            return any(not i["expires"] or i["expires"] >= now for i in self.tokens.values())

    @property
    def port(self) -> int | None:
        return self.httpd.server_address[1] if self.httpd else None

    def url(self, host: str, tok: str, kind: str, name: str, lang: str = "en") -> str:
        return f"http://{host}:{self.port}/{tok}/{kind}/{quote(name)}?l={quote(lang)}"

    # ---- servidor ----------------------------------------------------------

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

            def _html(self, body: str, head: bool):
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if not head:
                    self.wfile.write(data)

            def _handle(self, head: bool):
                url = urlsplit(self.path)
                parts = url.path.split("/")
                if len(parts) != 4:
                    return self._fail(404)
                tok, kind, name = parts[1], parts[2], unquote(parts[3])
                if not server._check(tok, name):
                    return self._fail(404)
                path = self._resolve(name)
                if not path:
                    return self._fail(404)
                lang = (parse_qs(url.query).get("l") or ["en"])[0]
                txt = TEXT.get(lang, TEXT["en"])
                if kind == "play":
                    return self._html(PAGE.format(title=html.escape(name), src=quote(name),
                                                  error=html.escape(txt["play_error"])), head)
                if kind == "share":
                    return self._html(SHARE_PAGE.format(
                        title=html.escape(name), src=quote(name), attr=html.escape(name, quote=True),
                        download=html.escape(txt["download"]), size=_fmt_size(os.path.getsize(path)),
                        note=html.escape(txt["share_note"])), head)
                if kind in ("file", "dl"):
                    return self._send_file(path, head, attachment=(kind == "dl"))
                return self._fail(404)

            def _send_file(self, path: str, head: bool, attachment: bool):
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
                if attachment:
                    name = os.path.basename(path)
                    self.send_header("Content-Disposition",
                                     f"attachment; filename*=UTF-8''{quote(name)}")
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

        self.httpd = ThreadingHTTPServer((self.host, 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name=f"decky-recorder-http-{self.host}",
                                       daemon=True)
        self.thread.start()
        if self.log:
            self.log.info(f"Servidor de vídeos en {self.host}:{self.port}")

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
            if self.log:
                self.log.info(f"Servidor de vídeos en {self.host} detenido")
