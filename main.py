"""
Decky Recorder (GPU Screen Recorder) — backend.

Arquitectura:
  - La captura y la codificación las hace gpu-screen-recorder (GSR): captura KMS
    de la pantalla compuesta por gamescope y codificación VA-API en la GPU, a
    frecuencia constante (-fm cfr) para que el vídeo no vaya a trompicones.
  - GSR viaja dentro del plugin como AppImage (bin/gpu-screen-recorder.AppImage,
    con todas sus dependencias). Se extrae una vez en el directorio de runtime.
  - GSR se niega a correr como root, así que el backend (root) lo lanza como el
    usuario con setpriv y la capacidad CAP_SYS_ADMIN como "ambient". Sin
    capacidades en el fichero, GSR lanza su ayudante de captura KMS con
    `pkexec gsr-kms-server ...`; ponemos delante en PATH un `pkexec` propio que
    solo hace exec, y el ayudante hereda CAP_SYS_ADMIN (sin contraseña).
  - Dos modos:
      * Grabación normal: un proceso GSR por grabación; SIGINT la cierra y guarda.
      * Repetición instantánea: un proceso GSR con buffer (-r N). SIGUSR1 guarda
        los últimos N segundos; SIGRTMIN graba un vídeo normal mientras tanto.
  - Los ficheros se guardan en ~/Videos/Decky Recorder.
"""

from __future__ import annotations

import asyncio
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import time
from datetime import datetime

import decky  # provisto por Decky Loader

from media_server import MediaServer, PcAccessServer, lan_ip, mp4_info

PLUGIN_DIR = decky.DECKY_PLUGIN_DIR
SETTINGS_DIR = decky.DECKY_PLUGIN_SETTINGS_DIR
RUNTIME_DIR = decky.DECKY_PLUGIN_RUNTIME_DIR
LOG_DIR = decky.DECKY_PLUGIN_LOG_DIR
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
GSR_LOG = os.path.join(LOG_DIR, "gsr.log")

USER = getattr(decky, "DECKY_USER", "deck")
USER_HOME = getattr(decky, "DECKY_USER_HOME", os.path.expanduser("~"))

APPIMAGE = os.path.join(PLUGIN_DIR, "bin", "gpu-screen-recorder.AppImage")
EXTRACT_ROOT = os.path.join(RUNTIME_DIR, "gsr")
SHIM_DIR = os.path.join(RUNTIME_DIR, "shim")
PKEXEC_SHIM = "#!/bin/sh\n# Decky Recorder: gsr-kms-server ya hereda CAP_SYS_ADMIN; no hace falta pkexec\nexec \"$@\"\n"

QUALITIES = ("medium", "high", "very_high", "ultra")
FPS_OPTIONS = (30, 60, 72, 90, 100, 120, 144)
FPS_MODES = ("cfr", "content")
VIDEO_EXTS = (".mp4", ".mkv")

STREAM_SERVICES = {
    "twitch": "rtmp://live.twitch.tv/app/",
    "youtube": "rtmp://a.rtmp.youtube.com/live2/",
    "custom": None,
}
STREAM_RESOLUTIONS = {"original": None, "1080": "1920x1080", "720": "1280x720"}
STREAM_KEY_FILE = os.path.join(SETTINGS_DIR, "stream_key")
STREAM_KEY_IMPORT = os.path.join(USER_HOME, "stream_key.txt")
SHARE_TTL = 15 * 60  # segundos que vive un enlace de compartir por red local
PC_TTL = 30 * 60     # segundos que dura el acceso desde el PC
ATOM_COMPOSITE = "GAMESCOPE_COMPOSITE_FORCE"
CODECS = ("h264", "hevc")
LANGUAGES = ("auto", "es", "en")

DEFAULT_SETTINGS = {
    "fps": 60,
    "fps_mode": "cfr",  # "cfr" constante | "content" según el juego (variable, -f es el máximo)
    "quality": "very_high",
    "codec": "h264",
    "hdr": False,
    "audio_game": True,
    "audio_mic": False,
    "replay_seconds": 30,
    "replay_autostart": False,
    "output_dir": os.path.join(USER_HOME, "Videos", "Decky Recorder"),
    "language": "auto",
    # Directo (RTMP). La clave de emisión NO va aquí: se guarda aparte (STREAM_KEY_FILE, 0600).
    "stream_service": "twitch",
    "stream_custom_url": "",
    "stream_bitrate": 6000,       # kbps, constante (CBR)
    "stream_fps": 60,
    "stream_resolution": "1080",  # "original" | "1080" | "720" (máximo; mantiene la proporción)
    "stream_save_copy": False,
    # Forzar composición de gamescope mientras se captura (colores correctos en juegos HDR)
    "force_composite": False,
}

SAFE_NAME = re.compile(r"[^\w\-. ()\[\]]+", re.UNICODE)


def _sanitize(s: dict) -> dict:
    out = dict(DEFAULT_SETTINGS)
    if isinstance(s, dict):
        for k in out:
            if k in s:
                out[k] = s[k]
    try:
        fps = int(out["fps"])
    except (TypeError, ValueError):
        fps = 60
    out["fps"] = min(FPS_OPTIONS, key=lambda f: abs(f - fps))
    if out["fps_mode"] not in FPS_MODES:
        out["fps_mode"] = "cfr"
    if out["quality"] not in QUALITIES:
        out["quality"] = DEFAULT_SETTINGS["quality"]
    if out["codec"] not in CODECS:
        out["codec"] = DEFAULT_SETTINGS["codec"]
    for k in ("hdr", "audio_game", "audio_mic", "replay_autostart", "stream_save_copy", "force_composite"):
        out[k] = bool(out[k])
    if out["stream_service"] not in STREAM_SERVICES:
        out["stream_service"] = "twitch"
    url = out["stream_custom_url"] if isinstance(out["stream_custom_url"], str) else ""
    out["stream_custom_url"] = url.strip()[:500]
    try:
        out["stream_bitrate"] = int(min(max(int(out["stream_bitrate"]), 1000), 20000))
    except (TypeError, ValueError):
        out["stream_bitrate"] = 6000
    try:
        out["stream_fps"] = 30 if int(out["stream_fps"]) <= 30 else 60
    except (TypeError, ValueError):
        out["stream_fps"] = 60
    if out["stream_resolution"] not in STREAM_RESOLUTIONS:
        out["stream_resolution"] = "1080"
    out["replay_seconds"] = int(min(max(int(out["replay_seconds"]), 5), 300))
    if out["language"] not in LANGUAGES:
        out["language"] = "auto"
    if not isinstance(out["output_dir"], str) or not out["output_dir"].startswith("/"):
        out["output_dir"] = DEFAULT_SETTINGS["output_dir"]
    return out


def _user_ids():
    try:
        p = pwd.getpwnam(USER)
        return p.pw_uid, p.pw_gid
    except KeyError:
        st = os.stat(USER_HOME)
        return st.st_uid, st.st_gid


def _chown(path: str):
    uid, gid = _user_ids()
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


def _makedirs_user(path: str):
    """Crea la carpeta (y padres que falten) con el usuario como propietario."""
    missing = []
    p = os.path.abspath(path)
    while p and p != "/" and not os.path.isdir(p):
        missing.append(p)
        p = os.path.dirname(p)
    for d in reversed(missing):
        os.makedirs(d, exist_ok=True)
        _chown(d)


def _safe_name(name: str) -> str:
    name = SAFE_NAME.sub("", (name or "").strip()).strip(" .")
    return name[:80] or "Steam"


def _session_env() -> dict:
    """Entorno gráfico y de audio de la sesión del usuario (tomado del proceso de Steam)."""
    wanted = ("steam", "steamwebhelper", "gamescope-wl", "gamescope")
    keys = ("DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
            "WAYLAND_DISPLAY", "GAMESCOPE_WAYLAND_DISPLAY")
    found: dict = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm", "r") as f:
                comm = f.read().strip()
            if comm not in wanted:
                continue
            with open(f"/proc/{pid}/environ", "rb") as f:
                raw = f.read().split(b"\0")
        except OSError:
            continue
        env = {}
        for item in raw:
            if b"=" in item:
                k, v = item.split(b"=", 1)
                env[k.decode(errors="ignore")] = v.decode(errors="ignore")
        if env.get("DISPLAY"):
            found = {k: env[k] for k in keys if env.get(k)}
            if comm == "steam":  # preferido: es el Xwayland raíz de gamescope
                break
    if "DISPLAY" not in found:
        found["DISPLAY"] = ":0"
    if "XDG_RUNTIME_DIR" not in found:
        found["XDG_RUNTIME_DIR"] = f"/run/user/{_user_ids()[0]}"
    run = found["XDG_RUNTIME_DIR"]
    pulse = os.path.join(run, "pulse", "native")
    if os.path.exists(pulse):
        found["PULSE_SERVER"] = f"unix:{pulse}"
    found["PIPEWIRE_RUNTIME_DIR"] = run
    return found


def _list_videos(d: str) -> dict:
    try:
        return {
            f: os.stat(os.path.join(d, f)).st_mtime
            for f in os.listdir(d)
            if f.lower().endswith((".mp4", ".mkv"))
        }
    except OSError:
        return {}


class Plugin:
    # ---- Ciclo de vida ------------------------------------------------------

    async def _main(self):
        self.lock = asyncio.Lock()
        self.settings = self._load()
        self.gsr_bin = None
        self.gsr_path_dir = None
        self.gsr_error = None
        self.display_mode = "x11"
        self.current_app = None  # {"appid", "name"}
        self.info_cache = {}  # nombre -> (mtime, tamaño, mp4_info)
        get_dir = lambda: self.settings["output_dir"]  # noqa: E731
        self.media = MediaServer(get_dir, "127.0.0.1", decky.logger)  # reproductor en Steam
        self.local_token = self.media.add_token()
        self.lan = MediaServer(get_dir, "0.0.0.0", decky.logger)  # compartir por QR, solo bajo demanda
        self.pc = PcAccessServer(get_dir, self._scan_videos, decky.logger, on_abuse=self._pc_abuse)
        self.pc_ip = None
        self.pc_task = None
        self.loop = asyncio.get_event_loop()
        self.composite_owned = False   # True si el plugin activó la composición forzada
        self.composite_prev = None     # valor que había antes (None = la propiedad no existía)

        self.rec_proc = None          # grabación normal (proceso propio)
        self.rec_file = None
        self.rec_started = 0.0
        self.replay_proc = None       # buffer de repetición
        self.stream_proc = None       # emisión en directo (RTMP)
        self.stream_started = 0.0
        self.replay_recording = False  # grabación normal dentro del proceso de replay/directo (SIGRTMIN)
        self._ro_before = {}
        self.watchers = set()

        try:
            await asyncio.to_thread(self._prepare_gsr)
        except Exception as e:  # noqa: BLE001
            self.gsr_error = str(e)
            decky.logger.error(f"No se pudo preparar gpu-screen-recorder: {e}")
        decky.logger.info(f"Decky Recorder cargado (gsr: {self.gsr_bin or 'no disponible'})")

    async def _unload(self):
        for srv in (getattr(self, "media", None), getattr(self, "lan", None), getattr(self, "pc", None)):
            if srv:
                await asyncio.to_thread(srv.stop)
        await self._stop_proc(self.rec_proc)
        await self._stop_proc(self.replay_proc)
        await self._stop_proc(self.stream_proc)
        await self._sync_composite(force_off=True)
        for t in list(self.watchers):
            t.cancel()

    async def _uninstall(self):
        shutil.rmtree(EXTRACT_ROOT, ignore_errors=True)

    # ---- gpu-screen-recorder ------------------------------------------------

    def _prepare_gsr(self):
        """Prepara el pkexec propio y localiza GSR (AppImage incluida o del sistema)."""
        self.setpriv = shutil.which("setpriv") or next(
            (p for p in ("/usr/bin/setpriv", "/bin/setpriv") if os.path.isfile(p)), None)
        if not self.setpriv:
            raise RuntimeError("no se encontró setpriv (util-linux)")
        os.makedirs(SHIM_DIR, exist_ok=True)
        shim = os.path.join(SHIM_DIR, "pkexec")
        with open(shim, "w", encoding="utf-8") as f:
            f.write(PKEXEC_SHIM)
        os.chmod(shim, 0o755)
        for d in (RUNTIME_DIR, SHIM_DIR):
            os.chmod(d, 0o755)
        self._find_gsr()
        # El usuario tiene que poder leer y ejecutar lo extraído por root
        subprocess.run(["chmod", "-R", "a+rX", EXTRACT_ROOT], check=False, capture_output=True)

    def _find_gsr(self):
        if os.path.isfile(APPIMAGE):
            st = os.stat(APPIMAGE)
            stamp = f"{st.st_size}-{int(st.st_mtime)}"
            target = os.path.join(EXTRACT_ROOT, stamp)
            if not os.path.isdir(target):
                shutil.rmtree(EXTRACT_ROOT, ignore_errors=True)
                tmp = target + ".tmp"
                os.makedirs(tmp, exist_ok=True)
                os.chmod(APPIMAGE, 0o755)
                env = self._clean_env()
                r = subprocess.run([APPIMAGE, "--appimage-extract"], cwd=tmp, env=env,
                                   capture_output=True, timeout=180)
                if r.returncode != 0:
                    raise RuntimeError(f"--appimage-extract falló ({r.returncode}): "
                                       f"{r.stderr.decode(errors='ignore')[-400:]}")
                os.replace(tmp, target)
            appdir = self._find_appdir(target)
            if appdir:
                exe = os.path.join(appdir, "bin", "gpu-screen-recorder")
                if os.path.isfile(exe):
                    self.gsr_bin = [exe]
                else:  # sharun/AppRun acepta el nombre del binario como primer argumento
                    self.gsr_bin = [os.path.join(appdir, "AppRun"), "gpu-screen-recorder"]
                self.gsr_path_dir = os.path.join(appdir, "bin")
                return
            raise RuntimeError(f"AppImage extraída sin AppDir reconocible en {target}")
        system = shutil.which("gpu-screen-recorder")
        if system:
            self.gsr_bin = [system]
            self.gsr_path_dir = os.path.dirname(system)
            return
        raise RuntimeError("No se encontró gpu-screen-recorder (ni incluido ni en el sistema)")

    @staticmethod
    def _find_appdir(root: str):
        for dirpath, dirnames, filenames in os.walk(root):
            if "AppRun" in filenames:
                return dirpath
            if dirpath.count(os.sep) - root.count(os.sep) >= 2:
                dirnames[:] = []
        return None

    @staticmethod
    def _clean_env() -> dict:
        env = dict(os.environ)
        env.pop("LD_LIBRARY_PATH", None)  # el Python de Decky (PyInstaller) lo altera
        env.pop("LD_PRELOAD", None)
        return env

    def _gsr_env(self, mode: str = "x11") -> dict:
        """mode "x11": Xwayland de gamescope (preferido). "wayland": socket Wayland de gamescope."""
        env = self._clean_env()
        for k in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "GAMESCOPE_WAYLAND_DISPLAY"):
            env.pop(k, None)  # nada del entorno de Decky: solo lo de la sesión de Steam
        sess = _session_env()
        wayland = sess.pop("GAMESCOPE_WAYLAND_DISPLAY", None) or sess.pop("WAYLAND_DISPLAY", None)
        sess.pop("WAYLAND_DISPLAY", None)
        env.update(sess)
        if mode == "wayland":
            if not wayland:
                run = env.get("XDG_RUNTIME_DIR", "")
                wayland = "gamescope-0" if os.path.exists(os.path.join(run, "gamescope-0")) else "wayland-0"
            env["WAYLAND_DISPLAY"] = wayland
            env["XDG_SESSION_TYPE"] = "wayland"
        else:
            env["XDG_SESSION_TYPE"] = "x11"
        env.update({"HOME": USER_HOME, "USER": USER, "LOGNAME": USER})
        # pkexec propio primero; gsr-kms-server se busca junto al ejecutable o en PATH
        path = [SHIM_DIR]
        if self.gsr_path_dir:
            path.append(self.gsr_path_dir)
        path += ["/usr/local/bin", "/usr/bin", "/bin"]
        env["PATH"] = ":".join(path)
        return env

    def _as_user(self) -> list:
        """Prefijo para ejecutar como el usuario conservando solo CAP_SYS_ADMIN."""
        uid, gid = _user_ids()
        return [self.setpriv, f"--reuid={uid}", f"--regid={gid}", "--init-groups",
                "--inh-caps=+sys_admin", "--ambient-caps=+sys_admin", "--"]

    def _base_args(self) -> list:
        s = self.settings
        codec = s["codec"]
        if s["hdr"]:
            codec = "hevc_hdr"
        args = [
            "-w", "screen",
            "-f", str(s["fps"]),
            "-fm", s["fps_mode"],
            "-k", codec,
            "-q", s["quality"],
            "-c", "mp4",
            "-cursor", "no",
        ]
        sources = []
        if s["audio_game"]:
            sources.append("default_output")
        if s["audio_mic"]:
            sources.append("default_input")
        if sources:
            args += ["-a", "|".join(sources), "-ac", "aac"]
        return args

    @staticmethod
    async def _pump(stream, secret: str | None):
        """Copia la salida de GSR al log, tachando la clave de emisión si aparece."""
        with open(GSR_LOG, "ab") as log:
            while True:
                line = await stream.readline()
                if not line:
                    break
                if secret:
                    line = line.replace(secret.encode(), b"****")
                log.write(line)
                log.flush()

    async def _spawn(self, args: list, secret: str | None = None):
        if not self.gsr_bin:
            raise RuntimeError(self.gsr_error or "gpu-screen-recorder no disponible")
        cmd = self._as_user() + self.gsr_bin + args
        shown_cmd = " ".join(cmd)
        if secret:
            shown_cmd = shown_cmd.replace(secret, "****")
        decky.logger.info("gsr: " + shown_cmd)
        os.makedirs(LOG_DIR, exist_ok=True)
        await self._sync_composite(starting=True)  # antes del primer fotograma
        # X11 (Xwayland de gamescope) primero; si no puede crear la ventana, Wayland de gamescope
        for mode in (self.display_mode, "wayland" if self.display_mode == "x11" else "x11"):
            env = self._gsr_env(mode)
            shown = {k: env.get(k) for k in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY",
                                             "XDG_SESSION_TYPE", "XDG_RUNTIME_DIR")}
            with open(GSR_LOG, "ab") as log:
                log.write(f"\n==== {datetime.now().isoformat()} [{mode}] {shown}\n{shown_cmd}\n".encode())
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env=env, cwd=USER_HOME, start_new_session=True)
            pump = asyncio.get_event_loop().create_task(self._pump(proc.stdout, secret))
            self.watchers.add(pump)
            pump.add_done_callback(self.watchers.discard)
            # Si muere en el primer segundo y medio es un error de arranque (captura, codificador…)
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                self.display_mode = mode  # recordar el que funciona
                return proc
            try:
                await asyncio.wait_for(pump, timeout=2)  # que el log tenga ya el error
            except asyncio.TimeoutError:
                pass
            tail = " ".join(self._log_tail(3)).lower()
            if "failed to create window" not in tail and "connect to the" not in tail:
                break  # el fallo no es de display: no tiene sentido reintentar
        await self._sync_composite()  # no arrancó: deshacer la composición forzada
        raise RuntimeError(f"gpu-screen-recorder terminó al arrancar ({proc.returncode}): "
                           + " | ".join(self._log_tail(4)))

    async def _stop_proc(self, proc, timeout=10.0):
        if proc and proc.returncode is None:
            try:
                proc.send_signal(signal.SIGINT)
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        await self._sync_composite()

    # ---- Composición forzada de gamescope -------------------------------------
    # En juegos HDR gamescope suele enviar la imagen del juego directamente a la pantalla
    # (direct scanout) y la conversión de color la hace el hardware de pantalla. La captura
    # KMS toma el búfer antes de esa conversión y los colores salen mal. Con
    # GAMESCOPE_COMPOSITE_FORCE=1 (lo mismo que "Forzar composición" de Steam) gamescope
    # compone él mismo cada fotograma y la captura sale bien, a cambio de algo de batería.

    def _capturing(self) -> bool:
        return any(self._running(p) for p in (self.rec_proc, self.replay_proc, self.stream_proc))

    # El Xwayland de gamescope solo admite conexiones del usuario de la sesión (no de root),
    # así que xprop se ejecuta como el usuario, sin capacidades.

    def _xprop(self, args: list) -> str:
        uid, gid = _user_ids()
        cmd = [self.setpriv, f"--reuid={uid}", f"--regid={gid}", "--init-groups", "--", "xprop", "-root"] + args
        r = subprocess.run(cmd, env=self._gsr_env("x11"), capture_output=True, timeout=5)
        if r.returncode != 0:
            err = (r.stderr or r.stdout).decode(errors="ignore").strip()
            raise RuntimeError(f"xprop {' '.join(args)} -> {r.returncode}: {err[-300:]}")
        return r.stdout.decode(errors="ignore")

    @staticmethod
    def _parse_cardinal(out: str):
        """'NAME(CARDINAL) = 1' -> 1; 'NAME:  not found.' -> None."""
        m = re.search(r"=\s*(\d+)", out)
        return int(m.group(1)) if m else None

    def _composite_get(self):
        return self._parse_cardinal(self._xprop([ATOM_COMPOSITE]))

    def _composite_set(self, value):
        if value is None:
            self._xprop(["-remove", ATOM_COMPOSITE])
        else:
            self._xprop(["-f", ATOM_COMPOSITE, "32c", "-set", ATOM_COMPOSITE, str(int(value))])

    async def _sync_composite(self, starting: bool = False, force_off: bool = False):
        want = (not force_off and self.settings.get("force_composite", False)
                and (starting or self._capturing()))
        try:
            if want and not self.composite_owned:
                prev = await asyncio.to_thread(self._composite_get)
                self.composite_prev = prev
                if prev != 1:  # si Steam ya lo tenía activado, no hay nada que hacer ni que restaurar
                    await asyncio.to_thread(self._composite_set, 1)
                    self.composite_owned = True
                    now = await asyncio.to_thread(self._composite_get)
                    decky.logger.info(f"Composición forzada de gamescope activada para capturar "
                                      f"(antes={prev}, ahora={now})")
            elif not want and self.composite_owned:
                await asyncio.to_thread(self._composite_set, self.composite_prev)
                self.composite_owned = False
                decky.logger.info(f"Composición forzada de gamescope restaurada ({self.composite_prev})")
        except Exception as e:  # noqa: BLE001
            decky.logger.error(f"No se pudo cambiar GAMESCOPE_COMPOSITE_FORCE: {e}")

    def _log_tail(self, n=6):
        try:
            with open(GSR_LOG, "r", encoding="utf-8", errors="replace") as f:
                lines = [l.rstrip() for l in f.readlines() if l.strip()]
            return lines[-n:]
        except OSError:
            return []

    def _watch(self, coro):
        t = asyncio.get_event_loop().create_task(coro)
        self.watchers.add(t)
        t.add_done_callback(self.watchers.discard)

    async def _watch_exit(self, proc, kind: str):
        """Avisa al frontend si GSR se cae solo (no por una parada nuestra)."""
        await proc.wait()
        async with self.lock:
            if kind == "record" and self.rec_proc is proc:
                self.rec_proc = None
                await self._finish_file(self.rec_file)
                self.rec_file = None
                await decky.emit("recorder_error", " | ".join(self._log_tail(3)))
            elif kind == "replay" and self.replay_proc is proc:
                self.replay_proc = None
                self.replay_recording = False
                await decky.emit("recorder_error", " | ".join(self._log_tail(3)))
            elif kind == "stream" and self.stream_proc is proc:
                self.stream_proc = None
                if self.replay_recording:
                    self.replay_recording = False
                    await self._collect_new(self.settings["output_dir"], self._ro_before,
                                            self._game_label(), timeout=3)
                await decky.emit("stream_error", " | ".join(self._log_tail(3)))
            await self._sync_composite()

    async def _finish_file(self, path):
        if path and os.path.isfile(path):
            _chown(path)
            return path
        return None

    async def _collect_new(self, d: str, before: dict, prefix: str, timeout=8.0):
        """Espera el fichero nuevo que escribe GSR (replay / -ro), lo renombra y lo entrega al usuario."""
        deadline = time.monotonic() + timeout
        found = None
        while time.monotonic() < deadline:
            await asyncio.sleep(0.4)
            now = _list_videos(d)
            new = [f for f, m in now.items() if f not in before or m > before[f]]
            if new:
                found = max(new, key=lambda f: now[f])
                # esperar a que deje de crecer
                path = os.path.join(d, found)
                last = -1
                for _ in range(20):
                    size = os.path.getsize(path) if os.path.exists(path) else -1
                    if size == last and size > 0:
                        break
                    last = size
                    await asyncio.sleep(0.25)
                break
        if not found:
            return None
        src = os.path.join(d, found)
        dst = os.path.join(d, f"{prefix} {datetime.now():%Y-%m-%d %H-%M-%S}{os.path.splitext(found)[1]}")
        try:
            os.replace(src, dst)
        except OSError:
            dst = src
        _chown(dst)
        return dst

    def _game_label(self) -> str:
        return _safe_name(self.current_app["name"]) if self.current_app else "Steam"

    # ---- Persistencia -------------------------------------------------------

    def _load(self) -> dict:
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return _sanitize(json.load(f))
        except (OSError, ValueError):
            return _sanitize({})

    def _save(self):
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)

    # ---- API para el frontend -----------------------------------------------

    def _state(self) -> dict:
        recording = (self.rec_proc is not None and self.rec_proc.returncode is None) or self.replay_recording
        replay = self._running(self.replay_proc)
        streaming = self._running(self.stream_proc)
        key = self._stream_key()
        return {
            "settings": self.settings,
            "available": bool(self.gsr_bin),
            "error": self.gsr_error,
            "recording": recording,
            "recording_since": self.rec_started if recording else 0,
            "now": time.time(),
            "replay": replay,
            "streaming": streaming,
            "stream_since": self.stream_started if streaming else 0,
            "stream_key_set": bool(key),
            "stream_key_hint": key[-4:] if len(key) >= 8 else "",
            "current_app": self.current_app,
            "pc_access": self._pc_state(),
        }

    @staticmethod
    def _running(proc) -> bool:
        return proc is not None and proc.returncode is None

    def _host_proc(self):
        """Proceso GSR que puede grabar además una copia con SIGRTMIN (-ro): replay o directo."""
        for p in (self.replay_proc, self.stream_proc):
            if self._running(p):
                return p
        return None

    async def get_state(self):
        return self._state()

    async def set_settings(self, patch: dict):
        capture_keys = ("fps", "fps_mode", "quality", "codec", "hdr", "audio_game", "audio_mic", "replay_seconds", "output_dir")
        async with self.lock:
            new = _sanitize({**self.settings, **(patch or {})})
            changed = any(new[k] != self.settings[k] for k in capture_keys)
            self.settings = new
            self._save()
            await self._sync_composite()  # activar/quitar al momento si ya se está capturando
            restart = (changed and self.replay_proc is not None and self.replay_proc.returncode is None
                       and not self.replay_recording and self.rec_proc is None)
        # Si el buffer está activo, reiniciarlo para aplicar los cambios de captura
        if restart:
            await self.set_replay(False)
            await self.set_replay(True)
        return self._state()

    async def app_started(self, appid: str, name: str):
        self.current_app = {"appid": str(appid), "name": name or f"App {appid}"}
        if (self.settings["replay_autostart"] and self.replay_proc is None and self.rec_proc is None
                and self.stream_proc is None):
            try:
                await self.set_replay(True)
            except Exception as e:  # noqa: BLE001
                decky.logger.error(f"No se pudo iniciar la repetición automática: {e}")
        return self._state()

    async def app_stopped(self, appid: str):
        if self.current_app and self.current_app["appid"] == str(appid):
            self.current_app = None
            if self.settings["replay_autostart"] and self.replay_proc is not None and not self.replay_recording:
                await self.set_replay(False)
        return self._state()

    async def toggle_recording(self):
        """Empieza o termina una grabación. Devuelve {"state", "saved"?: ruta}."""
        async with self.lock:
            out = self.settings["output_dir"]
            _makedirs_user(out)
            # Con el buffer o el directo activos, la grabación va por el mismo proceso (SIGRTMIN)
            host = self._host_proc()
            if host is not None:
                if not self.replay_recording:
                    self._ro_before = _list_videos(out)
                    os.kill(host.pid, signal.SIGRTMIN)
                    self.replay_recording = True
                    self.rec_started = time.time()
                    return {"state": self._state()}
                os.kill(host.pid, signal.SIGRTMIN)
                self.replay_recording = False
                saved = await self._collect_new(out, self._ro_before, self._game_label())
                return {"state": self._state(), "saved": saved}

            if self.rec_proc is not None and self.rec_proc.returncode is None:
                proc, path = self.rec_proc, self.rec_file
                self.rec_proc = None
                self.rec_file = None
                await self._stop_proc(proc)
                saved = await self._finish_file(path)
                return {"state": self._state(), "saved": saved}

            path = os.path.join(out, f"{self._game_label()} {datetime.now():%Y-%m-%d %H-%M-%S}.mp4")
            proc = await self._spawn(self._base_args() + ["-o", path])
            self.rec_proc, self.rec_file = proc, path
            self.rec_started = time.time()
            self._watch(self._watch_exit(proc, "record"))
            return {"state": self._state()}

    async def set_replay(self, enabled: bool):
        async with self.lock:
            running = self.replay_proc is not None and self.replay_proc.returncode is None
            if enabled and not running:
                if self.rec_proc is not None:
                    raise RuntimeError("busy_recording")
                if self._running(self.stream_proc):
                    raise RuntimeError("busy_streaming")
                out = self.settings["output_dir"]
                _makedirs_user(out)
                args = self._base_args() + [
                    "-r", str(self.settings["replay_seconds"]),
                    "-o", out,
                    "-ro", out,
                ]
                proc = await self._spawn(args)
                self.replay_proc = proc
                self._watch(self._watch_exit(proc, "replay"))
            elif not enabled and running:
                proc = self.replay_proc
                self.replay_proc = None
                was_recording = self.replay_recording
                self.replay_recording = False
                before = self._ro_before
                await self._stop_proc(proc)
                if was_recording:
                    await self._collect_new(self.settings["output_dir"], before, self._game_label(), timeout=3)
            return self._state()

    async def save_clip(self):
        """Guarda los últimos N segundos del buffer."""
        async with self.lock:
            if self.replay_proc is None or self.replay_proc.returncode is not None:
                raise RuntimeError("replay_off")
            out = self.settings["output_dir"]
            before = _list_videos(out)
            os.kill(self.replay_proc.pid, signal.SIGUSR1)
            saved = await self._collect_new(out, before, f"{self._game_label()} (clip)", timeout=15)
            return {"state": self._state(), "saved": saved}

    async def log_tail(self):
        return self._log_tail(12)

    # ---- Galería --------------------------------------------------------------

    def _scan_videos(self) -> list:
        d = self.settings["output_dir"]
        busy = {os.path.basename(p) for p in (self.rec_file,) if p}
        items = []
        try:
            names = os.listdir(d)
        except OSError:
            return []
        for name in names:
            if not name.lower().endswith(VIDEO_EXTS) or name.startswith(".") or name in busy:
                continue
            try:
                st = os.stat(os.path.join(d, name))
            except OSError:
                continue
            cached = self.info_cache.get(name)
            if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                info = cached[2]
            else:
                info = mp4_info(os.path.join(d, name))
                self.info_cache[name] = (st.st_mtime, st.st_size, info)
            items.append({"name": name, "size": st.st_size, "mtime": st.st_mtime, **info})
        items.sort(key=lambda v: v["mtime"], reverse=True)
        return items

    async def list_videos(self, limit: int = 10):
        items = await asyncio.to_thread(self._scan_videos)
        return {"videos": items[:max(1, int(limit))], "total": len(items)}

    async def player_url(self, name: str, lang: str = "en"):
        await asyncio.to_thread(self.media.start)
        return self.media.url("127.0.0.1", self.local_token, "play", os.path.basename(name), lang)

    # ---- Compartir por red local (QR) ----------------------------------------

    async def share_video(self, name: str, lang: str = "en"):
        """Abre el servidor de red local con un enlace temporal a un solo vídeo."""
        name = os.path.basename(name or "")
        if not name.lower().endswith(VIDEO_EXTS) or not os.path.isfile(
                os.path.join(self.settings["output_dir"], name)):
            raise RuntimeError("bad_name")
        ip = await asyncio.to_thread(lan_ip)
        if not ip:
            raise RuntimeError("no_lan")
        await asyncio.to_thread(self.lan.start)
        tok = self.lan.add_token(name, ttl=SHARE_TTL)
        self._watch(self._expire_share(tok))
        decky.logger.info(f"Compartiendo «{name}» en la red local ({ip}:{self.lan.port}) durante {SHARE_TTL // 60} min")
        return {"url": self.lan.url(ip, tok, "share", name, lang), "token": tok, "ttl": SHARE_TTL}

    async def stop_share(self, token: str = ""):
        self.lan.revoke(token or None)
        if not self.lan.has_tokens():
            await asyncio.to_thread(self.lan.stop)
        return True

    # ---- Acceso desde el PC (dirección corta + PIN) -----------------------------

    def _pc_state(self) -> dict:
        pc = getattr(self, "pc", None)
        if not pc or not pc.active or not self.pc_ip:
            return {"active": False}
        base = f"http://{self.pc_ip}:{pc.port}"
        return {"active": True, "url": base, "pin": pc.pin, "remaining": pc.remaining(),
                "qr_url": f"{base}/login?pin={pc.pin}"}

    def _pc_abuse(self):
        # Llamado desde el hilo del servidor: avisar al frontend desde el bucle de asyncio
        asyncio.run_coroutine_threadsafe(decky.emit("pc_access_closed"), self.loop)

    async def set_pc_access(self, enabled: bool, lang: str = "en"):
        if self.pc_task:
            self.pc_task.cancel()
            self.pc_task = None
        if not enabled:
            await asyncio.to_thread(self.pc.stop)
            return self._state()
        ip = await asyncio.to_thread(lan_ip)
        if not ip:
            raise RuntimeError("no_lan")
        self.pc_ip = ip
        await asyncio.to_thread(self.pc.start, PC_TTL, lang)
        self.pc_task = asyncio.get_event_loop().create_task(self._expire_pc())
        decky.logger.info(f"Acceso desde el PC en http://{ip}:{self.pc.port} durante {PC_TTL // 60} min")
        return self._state()

    async def _expire_pc(self):
        try:
            await asyncio.sleep(PC_TTL + 1)
        except asyncio.CancelledError:
            return
        self.pc_task = None
        await asyncio.to_thread(self.pc.stop)

    async def _expire_share(self, tok: str):
        await asyncio.sleep(SHARE_TTL + 1)
        await self.stop_share(tok)

    # ---- Directo (RTMP) -------------------------------------------------------

    @staticmethod
    def _stream_key() -> str:
        try:
            with open(STREAM_KEY_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    @staticmethod
    def _store_key(key: str):
        key = (key or "").strip()
        if any(c.isspace() for c in key) or len(key) > 400:
            raise RuntimeError("bad_key")
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        if not key:
            try:
                os.remove(STREAM_KEY_FILE)
            except FileNotFoundError:
                pass
            return
        fd = os.open(STREAM_KEY_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key)
        os.chmod(STREAM_KEY_FILE + ".tmp", 0o600)
        os.replace(STREAM_KEY_FILE + ".tmp", STREAM_KEY_FILE)

    async def set_stream_key(self, key: str):
        self._store_key(key)
        return self._state()

    async def import_stream_key(self):
        """Lee la clave de ~/stream_key.txt (cómodo para pegarla desde el modo escritorio)."""
        try:
            with open(STREAM_KEY_IMPORT, "r", encoding="utf-8") as f:
                key = f.read(1024).strip()
        except OSError:
            raise RuntimeError("no_key_file")
        if not key:
            raise RuntimeError("no_key_file")
        self._store_key(key)
        return self._state()

    def _stream_url(self, key: str) -> str:
        s = self.settings
        base = STREAM_SERVICES[s["stream_service"]]
        if base:
            if not key:
                raise RuntimeError("no_key")
            return base + key
        url = s["stream_custom_url"]
        if not re.match(r"^(rtmps?|srt)://", url, re.I):
            raise RuntimeError("bad_url")
        if "{key}" in url:
            if not key:
                raise RuntimeError("no_key")
            return url.replace("{key}", key)
        return f"{url.rstrip('/')}/{key}" if key else url

    def _stream_args(self, url: str) -> list:
        s = self.settings
        args = [
            "-w", "screen",
            "-f", str(s["stream_fps"]),
            "-fm", "cfr",
            "-k", "h264",
            "-bm", "cbr",
            "-q", str(s["stream_bitrate"]),
            "-c", "flv",
            "-cursor", "no",
        ]
        res = STREAM_RESOLUTIONS[s["stream_resolution"]]
        if res:
            args += ["-s", res]
        sources = []
        if s["audio_game"]:
            sources.append("default_output")
        if s["audio_mic"]:
            sources.append("default_input")
        if sources:
            args += ["-a", "|".join(sources), "-ac", "aac"]
        if s["stream_save_copy"]:
            args += ["-ro", s["output_dir"]]
        return args + ["-o", url]

    async def start_stream(self):
        async with self.lock:
            if self._running(self.stream_proc):
                return self._state()
            if self._running(self.rec_proc):
                raise RuntimeError("busy_recording")
            if self._running(self.replay_proc):
                raise RuntimeError("busy_replay")
            key = self._stream_key()
            url = self._stream_url(key)
            out = self.settings["output_dir"]
            _makedirs_user(out)
            proc = await self._spawn(self._stream_args(url), secret=key or None)
            self.stream_proc = proc
            self.stream_started = time.time()
            self._watch(self._watch_exit(proc, "stream"))
            if self.settings["stream_save_copy"]:
                self._ro_before = _list_videos(out)
                os.kill(proc.pid, signal.SIGRTMIN)  # empieza la copia local
                self.replay_recording = True
                self.rec_started = time.time()
            return self._state()

    async def stop_stream(self):
        async with self.lock:
            proc = self.stream_proc
            self.stream_proc = None
            was_recording = self.replay_recording
            self.replay_recording = False
            await self._stop_proc(proc)
            saved = None
            if was_recording:
                saved = await self._collect_new(self.settings["output_dir"], self._ro_before,
                                                self._game_label(), timeout=5)
            return {"state": self._state(), "saved": saved}

    async def delete_video(self, name: str, limit: int = 10):
        name = os.path.basename(name or "")
        d = self.settings["output_dir"]
        path = os.path.join(d, name)
        if (not name.lower().endswith(VIDEO_EXTS) or name.startswith(".")
                or (self.rec_file and os.path.basename(self.rec_file) == name)):
            raise RuntimeError("bad_name")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        self.info_cache.pop(name, None)
        decky.logger.info(f"Vídeo borrado: {path}")
        return await self.list_videos(limit)
