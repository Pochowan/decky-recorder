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


class BaseVideoHandler(BaseHTTPRequestHandler):
    """Métodos comunes: HTML, vídeos con Range y validación de nombres."""
    get_dir = staticmethod(lambda: "")  # lo fija cada servidor
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # sin ruido en el log de Decky
        pass

    def _fail(self, code: int):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _resolve(self, name: str):
        if not name or name != os.path.basename(name) or name.startswith("."):
            return None
        if os.path.splitext(name)[1].lower() not in VIDEO_TYPES:
            return None
        path = os.path.join(self.get_dir(), name)
        return path if os.path.isfile(path) else None

    def _html(self, body: str, head: bool, code: int = 200, headers: dict | None = None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(data)

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

        class Handler(BaseVideoHandler):
            get_dir = staticmethod(server.get_dir)

            def do_HEAD(self):
                self._handle(head=True)

            def do_GET(self):
                self._handle(head=False)

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


# ===========================================================================
# Acceso desde el PC: dirección corta + PIN, lista de todas las grabaciones
# ===========================================================================

PC_TEXT = {
    "es": {
        "title": "Decky Recorder",
        "pin_prompt": "Introduce el PIN que aparece en el panel de la consola.",
        "enter": "Entrar",
        "wrong": "PIN incorrecto.",
        "locked": "Demasiados intentos. Espera {s} s y vuelve a probar.",
        "closed": "El acceso desde el PC está desactivado. Actívalo en el panel de la consola.",
        "empty": "Todavía no hay grabaciones.",
        "play": "Ver",
        "download": "Descargar",
        "recording": "en curso o incompleto",
        "footer": "Acceso temporal: se cierra al desactivarlo en la consola o a los {m} min.",
        "play_error": TEXT["es"]["play_error"],
    },
    "en": {
        "title": "Decky Recorder",
        "pin_prompt": "Enter the PIN shown in the console's panel.",
        "enter": "Enter",
        "wrong": "Wrong PIN.",
        "locked": "Too many attempts. Wait {s} s and try again.",
        "closed": "PC access is off. Turn it on in the console's panel.",
        "empty": "No recordings yet.",
        "play": "Play",
        "download": "Download",
        "recording": "in progress or incomplete",
        "footer": "Temporary access: it closes when turned off on the console or after {m} min.",
        "play_error": TEXT["en"]["play_error"],
    },
}

PC_STYLE = """
*{box-sizing:border-box}
body{margin:0;background:#0f1117;color:#e5e7eb;font-family:system-ui,'Segoe UI',sans-serif}
header{padding:20px 24px;background:#161a23;border-bottom:1px solid #262b36;display:flex;align-items:center;gap:12px}
header b{font-size:1.25em}.dot{width:12px;height:12px;border-radius:50%;background:#ef4444;display:inline-block}
main{max-width:980px;margin:0 auto;padding:20px 16px}
.card{display:flex;gap:16px;align-items:center;background:#161a23;border:1px solid #262b36;border-radius:12px;
padding:14px 16px;margin-bottom:12px;flex-wrap:wrap}
.info{flex:1;min-width:220px}.name{font-weight:600;word-break:break-word}.meta{color:#9ca3af;font-size:.9em;margin-top:4px}
a.btn,button{display:inline-block;padding:10px 16px;border-radius:8px;background:#1a9fff;color:#fff;text-decoration:none;
font-weight:600;border:0;font-size:1em;cursor:pointer}
a.alt{background:#2b3240}
.box{max-width:360px;margin:12vh auto;background:#161a23;border:1px solid #262b36;border-radius:14px;padding:24px;text-align:center}
input{width:100%;font-size:1.8em;letter-spacing:.3em;text-align:center;padding:10px;border-radius:8px;border:1px solid #374151;
background:#0f1117;color:#fff;margin:16px 0}
.err{color:#f87171;margin-top:4px}footer{color:#6b7280;font-size:.85em;text-align:center;padding:16px}
"""


def _page(title: str, body: str, lang: str) -> str:
    return (f'<!doctype html><html lang="{html.escape(lang)}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
            f'<style>{PC_STYLE}</style></head><body>{body}</body></html>')


def _fmt_dur(sec) -> str | None:
    if sec is None:
        return None
    s = int(sec)
    h, m = divmod(s, 3600)
    m, s = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class PcAccessServer:
    """Galería web en la red local, protegida con un PIN de 6 dígitos.

    Rutas: "/" (PIN o lista), "/login" (POST con el PIN, o GET ?pin= desde el QR),
    "/play/<n>", "/file/<n>", "/dl/<n>". Todo requiere sesión salvo "/" y "/login".
    """

    PORT = 8090
    COOKIE = "dr_session"
    IP_MAX_FAILS = 5
    IP_LOCK = 120          # s de bloqueo por IP tras IP_MAX_FAILS fallos
    GLOBAL_MAX_FAILS = 30  # fallos totales antes de cerrar el acceso

    def __init__(self, get_dir, list_videos, logger=None, on_abuse=None):
        self.get_dir = get_dir
        self.list_videos = list_videos  # callable -> [{"name","size","mtime","duration","codec"}]
        self.log = logger
        self.on_abuse = on_abuse        # callable cuando se cierra por exceso de intentos
        self.lock = threading.Lock()
        self.httpd = None
        self.thread = None
        self.pin = None
        self.expires = 0.0
        self.lang = "en"
        self.sessions: set = set()
        self.fails: dict = {}  # ip -> [fallos, bloqueado_hasta]
        self.total_fails = 0

    @property
    def active(self) -> bool:
        return self.httpd is not None and time.time() < self.expires

    @property
    def port(self) -> int | None:
        return self.httpd.server_address[1] if self.httpd else None

    def remaining(self) -> int:
        return max(0, int(self.expires - time.time())) if self.httpd else 0

    # ---- PIN / sesiones ------------------------------------------------------

    def _try_pin(self, ip: str, pin: str):
        """Devuelve (token | None, (motivo, segundos) | None)."""
        now = time.time()
        with self.lock:
            count, until = self.fails.get(ip, [0, 0.0])
            if until > now:
                return None, ("locked", int(until - now) + 1)
            given = (pin or "").strip().replace(" ", "")
            if self.pin and secrets.compare_digest(given, self.pin):
                self.fails.pop(ip, None)
                tok = secrets.token_urlsafe(24)
                self.sessions.add(tok)
                return tok, None
            count += 1
            self.total_fails += 1
            until = now + self.IP_LOCK if count >= self.IP_MAX_FAILS else 0.0
            self.fails[ip] = [0 if until else count, until]
            abuse = self.total_fails >= self.GLOBAL_MAX_FAILS
        if abuse:
            if self.log:
                self.log.warning("Acceso desde el PC cerrado: demasiados PIN incorrectos")
            threading.Thread(target=self.stop, daemon=True).start()
            if self.on_abuse:
                self.on_abuse()
            return None, ("closed", 0)
        return None, (("locked", self.IP_LOCK) if until else ("wrong", 0))

    def _authed(self, handler) -> bool:
        raw = handler.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == self.COOKIE and v:
                with self.lock:
                    return any(secrets.compare_digest(v, s) for s in self.sessions)
        return False

    # ---- servidor ------------------------------------------------------------

    def start(self, ttl: int, lang: str = "en") -> str:
        """Arranca (o reinicia) el acceso con un PIN nuevo. Devuelve el PIN."""
        self.stop()
        with self.lock:
            self.pin = f"{secrets.randbelow(10 ** 6):06d}"
            self.expires = time.time() + ttl
            self.lang = lang if lang in PC_TEXT else "en"
            self.sessions.clear()
            self.fails.clear()
            self.total_fails = 0
        server = self

        class Handler(BaseVideoHandler):
            get_dir = staticmethod(server.get_dir)

            def do_HEAD(self):
                self._route("GET", head=True)

            def do_GET(self):
                self._route("GET", head=False)

            def do_POST(self):
                self._route("POST", head=False)

            def _t(self):
                return PC_TEXT[server.lang]

            def _box(self, text: str, code: int, head: bool = False):
                t = self._t()
                self._html(_page(t["title"], f'<div class="box">{html.escape(text)}</div>', server.lang), head, code)

            def _pin_page(self, error: str = "", code: int = 200):
                t = self._t()
                err = f'<div class="err">{html.escape(error)}</div>' if error else ""
                body = (f'<div class="box"><div><span class="dot"></span> <b>{t["title"]}</b></div>'
                        f'<p>{html.escape(t["pin_prompt"])}</p>'
                        f'<form method="post" action="/login"><input name="pin" inputmode="numeric" '
                        f'autocomplete="one-time-code" maxlength="7" autofocus required>'
                        f'<button type="submit">{html.escape(t["enter"])}</button></form>{err}</div>')
                self._html(_page(t["title"], body, server.lang), False, code)

            def _list_page(self, head: bool):
                t = self._t()
                cards = []
                for v in server.list_videos():
                    q = quote(v["name"])
                    meta = [time.strftime("%d/%m/%Y %H:%M", time.localtime(v["mtime"])),
                            _fmt_dur(v.get("duration")) or t["recording"], _fmt_size(v["size"])]
                    if v.get("codec"):
                        meta.append(v["codec"].upper())
                    cards.append(
                        f'<div class="card"><div class="info"><div class="name">{html.escape(v["name"])}</div>'
                        f'<div class="meta">{html.escape(" · ".join(meta))}</div></div>'
                        f'<a class="btn alt" href="/play/{q}" target="_blank">{html.escape(t["play"])}</a>'
                        f'<a class="btn" href="/dl/{q}" download="{html.escape(v["name"], quote=True)}">'
                        f'{html.escape(t["download"])}</a></div>')
                if not cards:
                    cards.append(f'<p>{html.escape(t["empty"])}</p>')
                minutes = max(1, server.remaining() // 60)
                body = (f'<header><span class="dot"></span><b>{t["title"]}</b></header><main>{"".join(cards)}</main>'
                        f'<footer>{html.escape(t["footer"].format(m=minutes))}</footer>')
                self._html(_page(t["title"], body, server.lang), head)

            def _login(self, pin: str):
                t = self._t()
                tok, err = server._try_pin(self.client_address[0], pin)
                if tok:
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", f"{server.COOKIE}={tok}; HttpOnly; SameSite=Strict; Path=/; "
                                                   f"Max-Age={max(60, server.remaining())}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                kind, secs = err
                if kind == "closed":
                    return self._box(t["closed"], 403)
                msg = t["locked"].format(s=secs) if kind == "locked" else t["wrong"]
                self._pin_page(msg, 429 if kind == "locked" else 401)

            def _route(self, method: str, head: bool):
                t = self._t()
                if not server.active:
                    return self._box(t["closed"], 403, head)
                url = urlsplit(self.path)
                if url.path == "/login":
                    if method == "POST":
                        n = int(self.headers.get("Content-Length") or 0)
                        if n > 1024:
                            return self._fail(413)
                        form = parse_qs(self.rfile.read(n).decode(errors="ignore"))
                        return self._login((form.get("pin") or [""])[0])
                    pin = (parse_qs(url.query).get("pin") or [""])[0]  # enlace del QR
                    return self._login(pin) if pin else self._pin_page()
                if method != "GET":
                    return self._fail(405)
                authed = server._authed(self)
                if url.path == "/":
                    return self._list_page(head) if authed else self._pin_page()
                if not authed:
                    return self._fail(403)
                parts = url.path.split("/")
                if len(parts) != 3:
                    return self._fail(404)
                kind, name = parts[1], unquote(parts[2])
                path = self._resolve(name)
                if not path:
                    return self._fail(404)
                if kind == "play":
                    page = PAGE.replace("../file/", "/file/")
                    return self._html(page.format(title=html.escape(name), src=quote(name),
                                                  error=html.escape(t["play_error"])), head)
                if kind in ("file", "dl"):
                    return self._send_file(path, head, attachment=(kind == "dl"))
                return self._fail(404)

        try:
            self.httpd = ThreadingHTTPServer(("0.0.0.0", self.PORT), Handler)
        except OSError:  # puerto ocupado: cualquiera libre
            self.httpd = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="decky-recorder-pc", daemon=True)
        self.thread.start()
        if self.log:
            self.log.info(f"Acceso desde el PC activo en el puerto {self.port}")
        return self.pin

    def stop(self):
        httpd = self.httpd
        self.httpd = None
        with self.lock:
            self.pin = None
            self.sessions.clear()
        if httpd:
            httpd.shutdown()
            httpd.server_close()
            if self.log:
                self.log.info("Acceso desde el PC desactivado")
