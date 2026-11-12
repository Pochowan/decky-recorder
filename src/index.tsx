import { addEventListener, callable, definePlugin, removeEventListener, toaster } from "@decky/api";
import {
  ButtonItem,
  ConfirmModal,
  DialogButton,
  DropdownItem,
  Field,
  Focusable,
  Navigation,
  PanelSection,
  PanelSectionRow,
  Router,
  SliderField,
  ToggleField,
  showModal,
  staticClasses,
} from "@decky/ui";
import { useEffect, useState } from "react";
import { MdDelete, MdFiberManualRecord, MdPlayArrow } from "react-icons/md";
import { Key, LANGUAGES, Lang, mapSteamLanguage, translate } from "./i18n";

type Settings = {
  fps: number;
  fps_mode: "cfr" | "content";
  quality: "medium" | "high" | "very_high" | "ultra";
  codec: "h264" | "hevc";
  hdr: boolean;
  audio_game: boolean;
  audio_mic: boolean;
  replay_seconds: number;
  replay_autostart: boolean;
  output_dir: string;
  language: string;
};

type State = {
  settings: Settings;
  available: boolean;
  error: string | null;
  recording: boolean;
  recording_since: number;
  now: number;
  replay: boolean;
  current_app: { appid: string; name: string } | null;
};

type ActionResult = { state: State; saved?: string | null };

type Video = {
  name: string;
  size: number;
  mtime: number;
  duration: number | null;
  codec: "h264" | "hevc" | "av1" | null;
};
type VideoList = { videos: Video[]; total: number };

const getState = callable<[], State>("get_state");
const setSettings = callable<[Partial<Settings>], State>("set_settings");
const toggleRecording = callable<[], ActionResult>("toggle_recording");
const setReplay = callable<[boolean], State>("set_replay");
const saveClip = callable<[], ActionResult>("save_clip");
const appStarted = callable<[string, string], State>("app_started");
const appStopped = callable<[string], State>("app_stopped");
const logTail = callable<[], string[]>("log_tail");
const listVideos = callable<[number], VideoList>("list_videos");
const playerUrl = callable<[string, string], string | null>("player_url");
const deleteVideo = callable<[string, number], VideoList>("delete_video");

/** Idioma de la interfaz de Steam (para la opción "Automático"). */
function steamLanguage(): Lang {
  const w = window as any;
  const candidates = [w.LocalizationManager?.m_rgLocalesToUse?.[0], w.LocalizationManager?.m_strLanguage, navigator.language];
  for (const c of candidates) if (typeof c === "string" && c) return mapSteamLanguage(c);
  return "en";
}

let currentLang: Lang = steamLanguage();

function resolveLang(setting: string | undefined): Lang {
  if (setting && setting !== "auto" && LANGUAGES.some((l) => l.code === setting)) return setting as Lang;
  return steamLanguage();
}

const t = (key: Key, params?: Record<string, string | number>) => translate(currentLang, key, params);

function appName(appid: string): string {
  const w = window as any;
  try {
    const ov = w.appStore?.GetAppOverviewByAppID?.(Number(appid));
    if (ov?.display_name) return ov.display_name;
  } catch {
    /* ignorar */
  }
  const running = Router.MainRunningApp as any;
  if (running && String(running.appid) === appid && running.display_name) return running.display_name;
  return `App ${appid}`;
}

function fmtTime(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${pad(m)}:${pad(s % 60)}`;
}

function baseName(p: string): string {
  return p.split("/").pop() || p;
}

function fmtSize(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  return `${Math.max(0.1, bytes / 1024 ** 2).toFixed(1)} MB`;
}

function fmtDate(mtime: number): string {
  const d = new Date(mtime * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Nombre sin extensión ni la marca de fecha que añade el plugin. */
function videoTitle(name: string): string {
  return name.replace(/\.(mp4|mkv)$/i, "").replace(/ \d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}$/, "");
}

async function openVideo(name: string) {
  try {
    const url = await playerUrl(name, currentLang);
    if (!url) throw new Error("no url");
    Navigation.CloseSideMenus();
    Navigation.NavigateToExternalWeb(url);
  } catch (e) {
    toaster.toast({ title: t("open_error"), body: String((e as any)?.message ?? e) });
  }
}

const PAGE = 8;

function Gallery({ refreshKey }: { refreshKey: number }) {
  const [list, setList] = useState<VideoList | null>(null);
  const [limit, setLimit] = useState(PAGE);

  useEffect(() => {
    listVideos(limit).then(setList).catch(() => setList({ videos: [], total: 0 }));
  }, [limit, refreshKey]);

  const askDelete = (v: Video) =>
    showModal(
      <ConfirmModal
        strTitle={t("delete_title")}
        strDescription={t("delete_body", { name: v.name })}
        strOKButtonText={t("delete")}
        bDestructiveWarning
        onOK={async () => {
          try {
            setList(await deleteVideo(v.name, limit));
            toaster.toast({ title: t("deleted"), body: v.name });
          } catch (e) {
            toastError(e);
          }
        }}
      />,
    );

  return (
    <PanelSection title={t("sec_videos")}>
      {list && list.videos.length === 0 && (
        <PanelSectionRow>
          <Field label={t("no_videos")} />
        </PanelSectionRow>
      )}
      {list?.videos.map((v) => {
        const details = [
          fmtDate(v.mtime),
          v.duration != null ? fmtTime(v.duration) : t("incomplete"),
          fmtSize(v.size),
          v.codec ? v.codec.toUpperCase() : null,
        ]
          .filter(Boolean)
          .join(" · ");
        return (
          <PanelSectionRow key={v.name}>
            <Field
              label={videoTitle(v.name)}
              description={v.codec === "hevc" ? `${details}\n${t("hevc_note")}` : details}
              bottomSeparator="standard"
            />
            <Focusable style={{ display: "flex", gap: "8px", marginTop: "4px" }} flow-children="horizontal">
              <DialogButton
                style={{ flex: 1, minWidth: 0, display: "flex", alignItems: "center", justifyContent: "center", gap: "4px" }}
                onClick={() => openVideo(v.name)}
              >
                <MdPlayArrow /> {t("play")}
              </DialogButton>
              <DialogButton
                style={{ width: "48px", minWidth: "48px", padding: 0, display: "flex", alignItems: "center", justifyContent: "center" }}
                onClick={() => askDelete(v)}
                aria-label={t("delete")}
              >
                <MdDelete />
              </DialogButton>
            </Focusable>
          </PanelSectionRow>
        );
      })}
      {list && list.total > list.videos.length && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => setLimit(limit + PAGE)}>
            {t("show_more", { n: list.total })}
          </ButtonItem>
        </PanelSectionRow>
      )}
    </PanelSection>
  );
}

/** Los errores del backend llegan como excepción; traduce los conocidos. */
function errorText(e: unknown): string {
  const msg = String((e as any)?.message ?? e);
  if (msg.includes("busy_recording")) return t("err_busy_recording");
  if (msg.includes("replay_off")) return t("err_replay_off");
  return msg;
}

function toastError(e: unknown) {
  toaster.toast({ title: t("toast_error"), body: errorText(e), duration: 8000 });
}

function toastSaved(saved: string | null | undefined, clip: boolean) {
  if (saved) toaster.toast({ title: t(clip ? "toast_clip" : "toast_saved"), body: baseName(saved) });
  else toaster.toast({ title: t("toast_error"), body: t("toast_not_found"), duration: 8000 });
}

function Content() {
  const [state, setState] = useState<State | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [clock, setClock] = useState(0); // segundos desde el inicio de la grabación
  const [galleryKey, setGalleryKey] = useState(0); // fuerza recargar la galería tras guardar

  const refresh = async () => {
    try {
      const s = await getState();
      currentLang = resolveLang(s.settings.language);
      setState(s);
      setFailed(false);
    } catch {
      setFailed(true);
    }
  };

  useEffect(() => {
    refresh();
    const iv = setInterval(refresh, 3000);
    return () => clearInterval(iv);
  }, []);

  // Cronómetro local entre refrescos (corrige la diferencia de reloj con el backend)
  useEffect(() => {
    if (!state?.recording) return;
    const offset = state.now - Date.now() / 1000;
    const tick = () => setClock(Date.now() / 1000 + offset - state.recording_since);
    tick();
    const iv = setInterval(tick, 1000);
    return () => clearInterval(iv);
  }, [state?.recording, state?.recording_since, state?.now]);

  if (failed)
    return (
      <PanelSection>
        <PanelSectionRow>
          <Field label={t("load_error")} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={refresh}>
            {t("retry")}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    );
  if (!state)
    return (
      <PanelSection>
        <PanelSectionRow>
          <Field label={t("loading")} />
        </PanelSectionRow>
      </PanelSection>
    );

  currentLang = resolveLang(state.settings.language);
  const s = state.settings;

  const run = async (fn: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      toastError(e);
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const update = (patch: Partial<Settings>) => run(async () => setState(await setSettings(patch)));

  const showLog = async () => {
    const lines = await logTail();
    toaster.toast({ title: t("log_title"), body: lines.slice(-4).join("\n") || "—", duration: 15000 });
  };

  const status = state.recording
    ? t("status_recording", { time: fmtTime(clock) })
    : state.replay
      ? t("status_replay", { s: s.replay_seconds })
      : t("status_idle");

  const languageOptions = [
    { data: "auto", label: t("lang_auto") },
    ...LANGUAGES.map((l) => ({ data: l.code, label: l.name })),
  ];

  return (
    <>
      <PanelSection>
        {!state.available ? (
          <>
            <PanelSectionRow>
              <Field label={t("unavailable", { error: state.error ?? "?" })} />
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={showLog}>
                {t("show_log")}
              </ButtonItem>
            </PanelSectionRow>
          </>
        ) : (
          <>
            <PanelSectionRow>
              <Field label={status} description={state.current_app?.name} />
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    const wasRecording = state.recording;
                    const r = await toggleRecording();
                    setState(r.state);
                    if (wasRecording) {
                      toastSaved(r.saved, false);
                      setGalleryKey((k) => k + 1);
                    } else toaster.toast({ title: t("toast_started"), body: state.current_app?.name ?? "" });
                  })
                }
              >
                {state.recording ? t("stop") : t("start")}
              </ButtonItem>
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

      {state.available && (
        <PanelSection title={t("sec_replay")}>
          <PanelSectionRow>
            <ToggleField
              label={t("replay")}
              description={t("replay_desc")}
              checked={state.replay}
              disabled={busy}
              onChange={(v) => run(async () => setState(await setReplay(v)))}
            />
          </PanelSectionRow>
          {state.replay && (
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    const r = await saveClip();
                    setState(r.state);
                    toastSaved(r.saved, true);
                    setGalleryKey((k) => k + 1);
                  })
                }
              >
                {t("save_clip", { s: s.replay_seconds })}
              </ButtonItem>
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <SliderField
              label={t("replay_seconds")}
              value={s.replay_seconds}
              min={5}
              max={300}
              step={5}
              showValue
              resetValue={30}
              onChange={(v) => update({ replay_seconds: v })}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField
              label={t("autostart")}
              description={t("autostart_desc")}
              checked={s.replay_autostart}
              onChange={(v) => update({ replay_autostart: v })}
            />
          </PanelSectionRow>
        </PanelSection>
      )}

      <Gallery refreshKey={galleryKey} />

      <PanelSection title={t("sec_settings")}>
        <PanelSectionRow>
          <DropdownItem
            label={t("fps")}
            description={t("fps_desc")}
            rgOptions={[30, 60, 72, 90, 100, 120, 144].map((f) => ({ data: f, label: String(f) }))}
            selectedOption={s.fps}
            onChange={(o) => update({ fps: Number(o.data) })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label={t("fps_mode")}
            description={t(s.fps_mode === "content" ? "mode_content_desc" : "mode_cfr_desc")}
            rgOptions={[
              { data: "cfr", label: t("mode_cfr") },
              { data: "content", label: t("mode_content") },
            ]}
            selectedOption={s.fps_mode}
            onChange={(o) => update({ fps_mode: o.data })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label={t("quality")}
            rgOptions={(["medium", "high", "very_high", "ultra"] as const).map((q) => ({
              data: q,
              label: t(`q_${q}` as Key),
            }))}
            selectedOption={s.quality}
            onChange={(o) => update({ quality: o.data })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label={t("codec")}
            disabled={s.hdr}
            rgOptions={[
              { data: "h264", label: t("codec_h264") },
              { data: "hevc", label: t("codec_hevc") },
            ]}
            selectedOption={s.hdr ? "hevc" : s.codec}
            onChange={(o) => update({ codec: o.data })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField label={t("hdr")} description={t("hdr_desc")} checked={s.hdr} onChange={(v) => update({ hdr: v })} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField label={t("audio_game")} checked={s.audio_game} onChange={(v) => update({ audio_game: v })} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField label={t("audio_mic")} checked={s.audio_mic} onChange={(v) => update({ audio_mic: v })} />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label={t("language")}
            rgOptions={languageOptions}
            selectedOption={s.language || "auto"}
            onChange={(o) => update({ language: String(o.data) })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label={t("folder", { dir: s.output_dir })} />
        </PanelSectionRow>
        {state.available && (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={showLog}>
              {t("show_log")}
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  const sc = (window as any).SteamClient;

  const reg = sc?.GameSessions?.RegisterForAppLifetimeNotifications?.((e: any) => {
    const appid = String(e.unAppID);
    if (e.bRunning) {
      setTimeout(() => appStarted(appid, appName(appid)).catch(toastError), 1000);
    } else {
      appStopped(appid).catch(() => undefined);
    }
  });

  const running = Router.MainRunningApp as any;
  if (running?.appid) {
    const id = String(running.appid);
    appStarted(id, running.display_name || appName(id)).catch(() => undefined);
  }

  // Avisos del backend aunque el menú rápido esté cerrado (p. ej. GSR se cae)
  const onError = (msg: string) => toaster.toast({ title: t("toast_error"), body: msg || "—", duration: 10000 });
  addEventListener<[string]>("recorder_error", onError);

  return {
    name: "Decky Recorder",
    titleView: <div className={staticClasses.Title}>Decky Recorder</div>,
    content: <Content />,
    icon: <MdFiberManualRecord />,
    onDismount() {
      reg?.unregister?.();
      removeEventListener("recorder_error", onError);
    },
  };
});
