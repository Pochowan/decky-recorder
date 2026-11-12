// Traducciones de la interfaz de Decky Recorder.
// Para añadir un idioma: añade su código a LANGUAGES y un objeto completo en STRINGS.

export const LANGUAGES = [
  { code: "es", name: "Español" },
  { code: "en", name: "English" },
] as const;

export type Lang = (typeof LANGUAGES)[number]["code"];

const es = {
  loading: "Cargando…",
  load_error: "No se pudo conectar con el plugin. Reinicia Decky o la consola.",
  retry: "Reintentar",
  unavailable: "gpu-screen-recorder no está disponible: {error}",
  show_log: "Ver registro",
  log_title: "Registro de gpu-screen-recorder",
  status_idle: "Listo para grabar",
  status_recording: "● Grabando {time}",
  status_replay: "Repetición activa (últimos {s} s)",
  start: "Empezar a grabar",
  stop: "Parar y guardar",
  sec_replay: "Repetición instantánea",
  replay: "Buffer de repetición",
  replay_desc: "Graba en memoria de forma continua para guardar los últimos segundos cuando quieras.",
  save_clip: "Guardar clip ({s} s)",
  replay_seconds: "Duración del clip (s)",
  autostart: "Activar al abrir un juego",
  autostart_desc: "Enciende el buffer al iniciar un juego y lo apaga al cerrarlo.",
  sec_settings: "Ajustes de captura",
  fps: "Fotogramas por segundo",
  fps_desc: "No pases de la frecuencia de tu pantalla: gamescope no genera más imágenes.",
  fps_mode: "Modo de FPS",
  mode_cfr: "Constante (recomendado)",
  mode_content: "Según el juego (variable)",
  mode_cfr_desc: "Vídeo estable aunque el juego varíe: el más compatible y fluido.",
  mode_content_desc: "Sigue los FPS reales del juego, con los FPS elegidos como máximo. Algunos reproductores y editores lo manejan peor.",
  sec_videos: "Grabaciones",
  no_videos: "Todavía no hay grabaciones.",
  incomplete: "en curso o incompleto",
  show_more: "Mostrar más ({n} en total)",
  play: "Ver",
  delete: "Borrar",
  delete_title: "¿Borrar este vídeo?",
  delete_body: "Se borrará «{name}» de forma permanente. No se puede deshacer.",
  deleted: "Vídeo borrado",
  hevc_note: "HEVC/HDR: puede que el navegador de Steam no lo reproduzca.",
  open_error: "No se pudo abrir el reproductor",
  quality: "Calidad",
  q_medium: "Media",
  q_high: "Alta",
  q_very_high: "Muy alta",
  q_ultra: "Ultra",
  codec: "Códec",
  codec_h264: "H.264 (más compatible)",
  codec_hevc: "HEVC (archivos más pequeños)",
  hdr: "Grabar en HDR",
  hdr_desc: "HEVC 10 bits. Solo si la pantalla está en HDR; si no, los colores saldrán mal.",
  audio_game: "Audio del juego",
  audio_mic: "Micrófono",
  folder: "Carpeta: {dir}",
  language: "Idioma",
  lang_auto: "Automático (Steam)",
  toast_started: "Grabación iniciada",
  toast_saved: "Vídeo guardado",
  toast_clip: "Clip guardado",
  toast_not_found: "Se envió la orden, pero no apareció el archivo. Revisa el registro.",
  toast_error: "Error de grabación",
  err_busy_recording: "Para la grabación antes de activar la repetición.",
  err_replay_off: "La repetición instantánea no está activa.",
};

export type Key = keyof typeof es;
type Dict = Record<Key, string>;

const en: Dict = {
  loading: "Loading…",
  load_error: "Could not connect to the plugin. Restart Decky or the console.",
  retry: "Retry",
  unavailable: "gpu-screen-recorder is not available: {error}",
  show_log: "Show log",
  log_title: "gpu-screen-recorder log",
  status_idle: "Ready to record",
  status_recording: "● Recording {time}",
  status_replay: "Instant replay on (last {s} s)",
  start: "Start recording",
  stop: "Stop and save",
  sec_replay: "Instant replay",
  replay: "Replay buffer",
  replay_desc: "Records continuously in memory so you can save the last seconds at any time.",
  save_clip: "Save clip ({s} s)",
  replay_seconds: "Clip length (s)",
  autostart: "Turn on when a game starts",
  autostart_desc: "Starts the buffer when a game launches and stops it when it closes.",
  sec_settings: "Capture settings",
  fps: "Frames per second",
  fps_desc: "Don't go above your display's refresh rate: gamescope doesn't produce more frames.",
  fps_mode: "Frame rate mode",
  mode_cfr: "Constant (recommended)",
  mode_content: "Match the game (variable)",
  mode_cfr_desc: "Steady video even when the game's frame rate varies: the smoothest and most compatible.",
  mode_content_desc: "Follows the game's real frame rate, capped at the selected FPS. Some players and editors handle it worse.",
  sec_videos: "Recordings",
  no_videos: "No recordings yet.",
  incomplete: "in progress or incomplete",
  show_more: "Show more ({n} total)",
  play: "Play",
  delete: "Delete",
  delete_title: "Delete this video?",
  delete_body: "\"{name}\" will be permanently deleted. This can't be undone.",
  deleted: "Video deleted",
  hevc_note: "HEVC/HDR: Steam's browser may not be able to play it.",
  open_error: "Could not open the player",
  quality: "Quality",
  q_medium: "Medium",
  q_high: "High",
  q_very_high: "Very high",
  q_ultra: "Ultra",
  codec: "Codec",
  codec_h264: "H.264 (most compatible)",
  codec_hevc: "HEVC (smaller files)",
  hdr: "Record in HDR",
  hdr_desc: "10-bit HEVC. Only when the display is in HDR, otherwise colors will be wrong.",
  audio_game: "Game audio",
  audio_mic: "Microphone",
  folder: "Folder: {dir}",
  language: "Language",
  lang_auto: "Automatic (Steam)",
  toast_started: "Recording started",
  toast_saved: "Video saved",
  toast_clip: "Clip saved",
  toast_not_found: "The command was sent but no file appeared. Check the log.",
  toast_error: "Recording error",
  err_busy_recording: "Stop the recording before turning on instant replay.",
  err_replay_off: "Instant replay is not on.",
};

const STRINGS: Record<Lang, Dict> = { es, en };

/** Idioma de Steam (nombre interno de Steam o etiqueta BCP-47) -> código propio. */
export function mapSteamLanguage(raw: string | undefined | null): Lang {
  const s = (raw || "").toLowerCase();
  if (s === "spanish" || s === "latam" || s.startsWith("es")) return "es";
  return "en";
}

export function translate(lang: Lang, key: Key, params?: Record<string, string | number>): string {
  let s: string = (STRINGS[lang] ?? en)[key] ?? en[key] ?? key;
  if (params) for (const [k, v] of Object.entries(params)) s = s.split(`{${k}}`).join(String(v));
  return s;
}
