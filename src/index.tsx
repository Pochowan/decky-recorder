import { addEventListener, callable, definePlugin, removeEventListener, toaster } from "@decky/api";
import {
  ButtonItem,
  ConfirmModal,
  DialogButton,
  DropdownItem,
  Field,
  Focusable,
  ModalRoot,
  Navigation,
  PanelSection,
  PanelSectionRow,
  Router,
  SliderField,
  TextField,
  ToggleField,
  showModal,
  staticClasses,
} from "@decky/ui";
import { useEffect, useState } from "react";
import { MdDelete, MdFiberManualRecord, MdPlayArrow, MdQrCode2 } from "react-icons/md";
import qrcode from "qrcode-generator";
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
  stream_service: "twitch" | "youtube" | "custom";
  stream_custom_url: string;
  stream_bitrate: number;
  stream_fps: number;
  stream_resolution: "original" | "1080" | "720";
  stream_save_copy: boolean;
};

type State = {
  settings: Settings;
  available: boolean;
  error: string | null;
  recording: boolean;
  recording_since: number;
  now: number;
  replay: boolean;
  streaming: boolean;
  stream_since: number;
  stream_key_set: boolean;
  stream_key_hint: string;
  current_app: { appid: string; name: string } | null;
  pc_access: PcAccess;
};

type PcAccess = { active: boolean; url?: string; pin?: string; remaining?: number; qr_url?: string };

type ShareInfo = { url: string; token: string; ttl: number };

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
const shareVideo = callable<[string, string], ShareInfo>("share_video");
const stopShare = callable<[string], boolean>("stop_share");
const setStreamKey = callable<[string], State>("set_stream_key");
const importStreamKey = callable<[], State>("import_stream_key");
const startStream = callable<[], State>("start_stream");
const stopStream = callable<[], ActionResult>("stop_stream");
const setPcAccess = callable<[boolean, string], State>("set_pc_access");

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

/** QR como SVG propio (sin innerHTML): un <path> con un cuadrado por módulo oscuro. */
function QrCode({ text, size = 220 }: { text: string; size?: number }) {
  const qr = qrcode(0, "M");
  qr.addData(text);
  qr.make();
  const n = qr.getModuleCount();
  const quiet = 4;
  let d = "";
  for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (qr.isDark(r, c)) d += `M${c + quiet} ${r + quiet}h1v1h-1z`;
  const total = n + quiet * 2;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${total} ${total}`} shapeRendering="crispEdges"
      style={{ background: "#fff", borderRadius: "8px", display: "block", margin: "0 auto" }}>
      <path d={d} fill="#000" />
    </svg>
  );
}

function ShareModal({ name, closeModal }: { name: string; closeModal?: () => void }) {
  const [info, setInfo] = useState<ShareInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let token = "";
    let alive = true;
    shareVideo(name, currentLang)
      .then((i) => {
        token = i.token;
        if (alive) setInfo(i);
        else stopShare(token).catch(() => undefined);
      })
      .catch((e) => alive && setError(errorText(e)));
    return () => {
      alive = false;
      if (token) stopShare(token).catch(() => undefined); // cierra el enlace al cerrar la ventana
    };
  }, [name]);

  return (
    <ModalRoot onCancel={closeModal} closeModal={closeModal}>
      <div style={{ display: "flex", flexDirection: "column", gap: "12px", alignItems: "stretch" }}>
        <div className={staticClasses.Title} style={{ textAlign: "center" }}>
          {t("share_title")}
        </div>
        <div style={{ textAlign: "center", wordBreak: "break-word", opacity: 0.8 }}>{name}</div>
        {error && <div style={{ textAlign: "center", color: "#ff6b6b" }}>{error}</div>}
        {!error && !info && <div style={{ textAlign: "center" }}>{t("loading")}</div>}
        {info && (
          <>
            <QrCode text={info.url} />
            <div style={{ textAlign: "center", fontSize: "12px", opacity: 0.7, wordBreak: "break-all" }}>
              {info.url.split("?")[0]}
            </div>
            <div style={{ textAlign: "center", fontSize: "13px" }}>
              {t("share_desc", { min: Math.round(info.ttl / 60) })}
            </div>
          </>
        )}
        <DialogButton onClick={closeModal}>{t("close")}</DialogButton>
      </div>
    </ModalRoot>
  );
}

function KeyModal({ onSaved, closeModal }: { onSaved: (s: State) => void; closeModal?: () => void }) {
  const [key, setKey] = useState("");
  return (
    <ModalRoot onCancel={closeModal} closeModal={closeModal}>
      <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
        <div className={staticClasses.Title}>{t("key_title")}</div>
        <div style={{ fontSize: "13px", opacity: 0.8 }}>{t("key_desc")}</div>
        <TextField bIsPassword value={key} onChange={(e) => setKey(e.target.value)} />
        <Focusable style={{ display: "flex", gap: "8px" }} flow-children="horizontal">
          <DialogButton onClick={closeModal}>{t("cancel")}</DialogButton>
          <DialogButton
            disabled={!key.trim()}
            onClick={async () => {
              try {
                onSaved(await setStreamKey(key));
                toaster.toast({ title: t("key_saved"), body: "" });
                closeModal?.();
              } catch (e) {
                toastError(e);
              }
            }}
          >
            {t("save")}
          </DialogButton>
        </Focusable>
      </div>
    </ModalRoot>
  );
}

function StreamSection({
  state,
  setState,
  update,
  run,
  busy,
  onSaved,
}: {
  state: State;
  setState: (s: State) => void;
  update: (p: Partial<Settings>) => void;
  run: (fn: () => Promise<void>) => Promise<void>;
  busy: boolean;
  onSaved: () => void;
}) {
  const s = state.settings;
  const [url, setUrl] = useState(s.stream_custom_url);
  useEffect(() => setUrl(s.stream_custom_url), [s.stream_custom_url]);
  const live = state.streaming;

  const keyLabel = state.stream_key_set
    ? state.stream_key_hint
      ? t("key_set", { hint: state.stream_key_hint })
      : t("key_set_nohint")
    : t("key_missing");

  return (
    <PanelSection title={t("sec_stream")}>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={busy || state.replay}
          onClick={() =>
            run(async () => {
              if (live) {
                const r = await stopStream();
                setState(r.state);
                toaster.toast({ title: t("toast_live_end"), body: r.saved ? baseName(r.saved) : "" });
                if (r.saved) onSaved();
              } else {
                setState(await startStream());
                toaster.toast({ title: t("toast_live"), body: t(`svc_${s.stream_service}` as Key) });
              }
            })
          }
        >
          {live ? t("stop_live") : t("go_live")}
        </ButtonItem>
      </PanelSectionRow>
      {state.replay && !live && (
        <PanelSectionRow>
          <Field label={t("err_busy_replay")} />
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <DropdownItem
          label={t("stream_service")}
          disabled={live}
          rgOptions={(["twitch", "youtube", "custom"] as const).map((v) => ({ data: v, label: t(`svc_${v}` as Key) }))}
          selectedOption={s.stream_service}
          onChange={(o) => update({ stream_service: o.data })}
        />
      </PanelSectionRow>
      {s.stream_service === "custom" && (
        <PanelSectionRow>
          <TextField
            label={t("custom_url")}
            description={t("custom_url_desc")}
            disabled={live}
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onBlur={() => url !== s.stream_custom_url && update({ stream_custom_url: url })}
          />
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <Field label={keyLabel} />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={live}
          onClick={() => showModal(<KeyModal onSaved={setState} />)}
        >
          {t("enter_key")}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          description={t("import_key_desc")}
          disabled={live}
          onClick={() =>
            run(async () => {
              setState(await importStreamKey());
              toaster.toast({ title: t("key_saved"), body: "~/stream_key.txt" });
            })
          }
        >
          {t("import_key")}
        </ButtonItem>
      </PanelSectionRow>
      {state.stream_key_set && (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={live}
            onClick={() =>
              run(async () => {
                setState(await setStreamKey(""));
                toaster.toast({ title: t("key_cleared"), body: "" });
              })
            }
          >
            {t("clear_key")}
          </ButtonItem>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <SliderField
          label={t("bitrate")}
          description={t("bitrate_desc")}
          value={s.stream_bitrate}
          min={1000}
          max={12000}
          step={500}
          showValue
          resetValue={6000}
          disabled={live}
          onChange={(v) => update({ stream_bitrate: v })}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <DropdownItem
          label={t("stream_res")}
          disabled={live}
          rgOptions={[
            { data: "1080", label: "1080p" },
            { data: "720", label: "720p" },
            { data: "original", label: t("res_original") },
          ]}
          selectedOption={s.stream_resolution}
          onChange={(o) => update({ stream_resolution: o.data })}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <DropdownItem
          label={t("stream_fps")}
          disabled={live}
          rgOptions={[
            { data: 60, label: "60" },
            { data: 30, label: "30" },
          ]}
          selectedOption={s.stream_fps}
          onChange={(o) => update({ stream_fps: Number(o.data) })}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField
          label={t("save_copy")}
          description={t("save_copy_desc")}
          checked={s.stream_save_copy}
          disabled={live}
          onChange={(v) => update({ stream_save_copy: v })}
        />
      </PanelSectionRow>
    </PanelSection>
  );
}

function PcQrModal({ url, closeModal }: { url: string; closeModal?: () => void }) {
  return (
    <ModalRoot onCancel={closeModal} closeModal={closeModal}>
      <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
        <div className={staticClasses.Title} style={{ textAlign: "center" }}>
          {t("pc_qr_title")}
        </div>
        <QrCode text={url} />
        <div style={{ textAlign: "center", fontSize: "13px" }}>{t("pc_qr_desc")}</div>
        <DialogButton onClick={closeModal}>{t("close")}</DialogButton>
      </div>
    </ModalRoot>
  );
}

function PcAccessSection({
  state,
  setState,
  run,
  busy,
}: {
  state: State;
  setState: (s: State) => void;
  run: (fn: () => Promise<void>) => Promise<void>;
  busy: boolean;
}) {
  const pc = state.pc_access;
  const big: React.CSSProperties = {
    fontSize: "20px",
    fontWeight: 700,
    letterSpacing: "0.04em",
    userSelect: "all",
    whiteSpace: "nowrap",
    width: "100%",
  };
  return (
    <PanelSection title={t("sec_pc")}>
      <PanelSectionRow>
        <ToggleField
          label={t("pc_toggle")}
          description={t("pc_desc")}
          checked={pc.active}
          disabled={busy}
          onChange={(v) => run(async () => setState(await setPcAccess(v, currentLang)))}
        />
      </PanelSectionRow>
      {pc.active && pc.url && pc.pin && (
        <>
          <PanelSectionRow>
            <Field
              label={t("pc_url")}
              description={t("pc_left", { min: Math.max(1, Math.ceil((pc.remaining ?? 0) / 60)) })}
              childrenLayout="below"
            >
              <div style={{ ...big, fontSize: "15px" }}>{pc.url.replace("http://", "")}</div>
            </Field>
          </PanelSectionRow>
          <PanelSectionRow>
            <Field label={t("pc_pin")} childrenLayout="below">
              <div style={{ ...big, fontSize: "26px", letterSpacing: "0.2em" }}>
                {pc.pin.slice(0, 3)} {pc.pin.slice(3)}
              </div>
            </Field>
          </PanelSectionRow>
          {pc.qr_url && (
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => showModal(<PcQrModal url={pc.qr_url!} />)}>
                {t("pc_qr")}
              </ButtonItem>
            </PanelSectionRow>
          )}
        </>
      )}
    </PanelSection>
  );
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
                onClick={() => showModal(<ShareModal name={v.name} />)}
                aria-label={t("share")}
              >
                <MdQrCode2 />
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
  const codes: [string, Key][] = [
    ["busy_recording", "err_busy_recording"],
    ["busy_replay", "err_busy_replay"],
    ["busy_streaming", "err_busy_streaming"],
    ["replay_off", "err_replay_off"],
    ["no_key_file", "err_no_key_file"],
    ["no_key", "err_no_key"],
    ["bad_url", "err_bad_url"],
    ["bad_key", "err_bad_key"],
    ["no_lan", "err_no_lan"],
  ];
  for (const [code, key] of codes) if (msg.includes(code)) return t(key);
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
  // (el directo manda sobre la grabación si están las dos a la vez)
  const since = state?.streaming ? state.stream_since : state?.recording ? state.recording_since : 0;
  useEffect(() => {
    if (!state || !since) return;
    const offset = state.now - Date.now() / 1000;
    const tick = () => setClock(Date.now() / 1000 + offset - since);
    tick();
    const iv = setInterval(tick, 1000);
    return () => clearInterval(iv);
  }, [since, state?.now]);

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

  const status = state.streaming
    ? t("status_live", { time: fmtTime(clock) })
    : state.recording
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

      {state.available && (
        <StreamSection
          state={state}
          setState={setState}
          update={update}
          run={run}
          busy={busy}
          onSaved={() => setGalleryKey((k) => k + 1)}
        />
      )}

      <Gallery refreshKey={galleryKey} />

      <PcAccessSection state={state} setState={setState} run={run} busy={busy} />

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
  const onStreamError = (msg: string) =>
    toaster.toast({ title: t("toast_stream_error"), body: msg || "—", duration: 10000 });
  addEventListener<[string]>("stream_error", onStreamError);
  const onPcClosed = () => toaster.toast({ title: t("sec_pc"), body: t("pc_closed"), duration: 10000 });
  addEventListener("pc_access_closed", onPcClosed);

  return {
    name: "Decky Recorder",
    titleView: <div className={staticClasses.Title}>Decky Recorder</div>,
    content: <Content />,
    icon: <MdFiberManualRecord />,
    onDismount() {
      reg?.unregister?.();
      removeEventListener("recorder_error", onError);
      removeEventListener("stream_error", onStreamError);
      removeEventListener("pc_access_closed", onPcClosed);
    },
  };
});
