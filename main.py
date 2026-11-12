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

from media_server import MediaServer, mp4_info

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
    for k in ("hdr", "audio_game", "audio_mic", "replay_autostart"):
        out[k] = bool(out[k])
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
        self.media = MediaServer(lambda: self.settings["output_dir"], decky.logger)

        self.rec_proc = None          # grabación normal (proceso propio)
        self.rec_file = None
        self.rec_started = 0.0
        self.replay_proc = None       # buffer de repetición
        self.replay_recording = False  # grabación normal dentro del proceso de replay (SIGRTMIN)
        self.watchers = set()

        try:
            await asyncio.to_thread(self._prepare_gsr)
        except Exception as e:  # noqa: BLE001
            self.gsr_error = str(e)
            decky.logger.error(f"No se pudo preparar gpu-screen-recorder: {e}")
        decky.logger.info(f"Decky Recorder cargado (gsr: {self.gsr_bin or 'no disponible'})")

    async def _unload(self):
        if getattr(self, "media", None):
            await asyncio.to_thread(self.media.stop)
        await self._stop_proc(self.rec_proc)
        await self._stop_proc(self.replay_proc)
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

    async def _spawn(self, args: list):
        if not self.gsr_bin:
            raise RuntimeError(self.gsr_error or "gpu-screen-recorder no disponible")
        cmd = self._as_user() + self.gsr_bin + args
        decky.logger.info("gsr: " + " ".join(cmd))
        os.makedirs(LOG_DIR, exist_ok=True)
        # X11 (Xwayland de gamescope) primero; si no puede crear la ventana, Wayland de gamescope
        for mode in (self.display_mode, "wayland" if self.display_mode == "x11" else "x11"):
            env = self._gsr_env(mode)
            shown = {k: env.get(k) for k in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY",
                                             "XDG_SESSION_TYPE", "XDG_RUNTIME_DIR")}
            log = open(GSR_LOG, "ab")
            log.write(f"\n==== {datetime.now().isoformat()} [{mode}] {shown}\n{' '.join(cmd)}\n".encode())
            log.flush()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log,
                    env=env, cwd=USER_HOME, start_new_session=True)
            finally:
                log.close()
            # Si muere en el primer segundo y medio es un error de arranque (captura, codificador…)
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                self.display_mode = mode  # recordar el que funciona
                return proc
            tail = " ".join(self._log_tail(3)).lower()
            if "failed to create window" not in tail and "connect to the" not in tail:
                break  # el fallo no es de display: no tiene sentido reintentar
        raise RuntimeError(f"gpu-screen-recorder terminó al arrancar ({proc.returncode}): "
                           + " | ".join(self._log_tail(4)))

    async def _stop_proc(self, proc, timeout=10.0):
        if not proc or proc.returncode is not None:
            return
        try:
            proc.send_signal(signal.SIGINT)
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass

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
        replay = self.replay_proc is not None and self.replay_proc.returncode is None
        return {
            "settings": self.settings,
            "available": bool(self.gsr_bin),
            "error": self.gsr_error,
            "recording": recording,
            "recording_since": self.rec_started if recording else 0,
            "now": time.time(),
            "replay": replay,
            "current_app": self.current_app,
        }

    async def get_state(self):
        return self._state()

    async def set_settings(self, patch: dict):
        capture_keys = ("fps", "fps_mode", "quality", "codec", "hdr", "audio_game", "audio_mic", "replay_seconds", "output_dir")
        async with self.lock:
            new = _sanitize({**self.settings, **(patch or {})})
            changed = any(new[k] != self.settings[k] for k in capture_keys)
            self.settings = new
            self._save()
            restart = (changed and self.replay_proc is not None and self.replay_proc.returncode is None
                       and not self.replay_recording and self.rec_proc is None)
        # Si el buffer está activo, reiniciarlo para aplicar los cambios de captura
        if restart:
            await self.set_replay(False)
            await self.set_replay(True)
        return self._state()

    async def app_started(self, appid: str, name: str):
        self.current_app = {"appid": str(appid), "name": name or f"App {appid}"}
        if self.settings["replay_autostart"] and self.replay_proc is None and self.rec_proc is None:
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
            # Con el buffer activo, la grabación va por el mismo proceso (SIGRTMIN)
            if self.replay_proc is not None and self.replay_proc.returncode is None:
                if not self.replay_recording:
                    self._ro_before = _list_videos(out)
                    os.kill(self.replay_proc.pid, signal.SIGRTMIN)
                    self.replay_recording = True
                    self.rec_started = time.time()
                    return {"state": self._state()}
                os.kill(self.replay_proc.pid, signal.SIGRTMIN)
                self.replay_recording = False
                saved = await self._collect_new(out, getattr(self, "_ro_before", {}), self._game_label())
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
                before = getattr(self, "_ro_before", {})
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
        return self.media.player_url(os.path.basename(name), lang)

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
