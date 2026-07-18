/**
 * voiceInput — chat-composer microphone dictation helpers.
 *
 * The chat composer records the operator's voice with MediaRecorder, POSTs the
 * audio to `/api/audio/transcribe`, and injects the recognised text into the
 * xterm-hosted Ink composer. Transcription runs through local mlx-whisper
 * (forced `provider: "mlx"` server-side) — never a cloud STT API.
 *
 * The browser-free logic (mime selection, encoding, request, error mapping,
 * sanitisation) lives here so it can be unit-tested under the `node` vitest
 * env; the MediaRecorder orchestration lives in `useVoiceInput`.
 */
import { fetchJSON } from "@/lib/api";

export type MicLanguage = "auto" | "ru";

export interface TranscribeResponse {
  ok: boolean;
  transcript: string;
  provider?: string | null;
}

/**
 * MediaRecorder container candidates, most-preferred first. Chromium picks
 * webm/opus; Safari only offers mp4. All are decoded server-side by ffmpeg
 * before mlx-whisper, so any accepted one works.
 */
export const AUDIO_MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/mp4",
  "audio/ogg;codecs=opus",
  "audio/ogg",
];

/** Return the first MediaRecorder-supported container, or "" for the UA default. */
export function pickAudioMimeType(
  isSupported?: (type: string) => boolean,
): string {
  const check =
    isSupported ??
    ((type: string) =>
      typeof MediaRecorder !== "undefined" &&
      typeof MediaRecorder.isTypeSupported === "function" &&
      MediaRecorder.isTypeSupported(type));
  for (const type of AUDIO_MIME_CANDIDATES) {
    try {
      if (check(type)) return type;
    } catch {
      /* isTypeSupported can throw on some UAs — treat as unsupported */
    }
  }
  return "";
}

/** Read a recorded Blob into a base64 `data:` URL for the JSON upload. */
export function blobToDataUrl(
  blob: Blob,
  makeReader: () => FileReader = () => new FileReader(),
): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = makeReader();
    reader.onerror = () =>
      reject(reader.error ?? new Error("Failed to read the recording"));
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.readAsDataURL(blob);
  });
}

/**
 * Collapse CR/LF and runs of whitespace to single spaces and trim. Newlines in
 * the transcript would otherwise submit the composer when injected as keystrokes.
 */
export function sanitizeTranscript(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

/**
 * Turn a thrown transcription error into a short, user-facing message.
 * `fetchJSON` throws `"<status>: <body>"` where the body is FastAPI's
 * `{"detail": "..."}` envelope — unwrap it so the backend's specific message
 * (e.g. "mlx-whisper is not installed…") reaches the operator.
 */
export function describeTranscribeError(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err);
  const statusMatch = raw.match(/^\s*\d+:\s*([\s\S]*)$/);
  let detail = statusMatch ? statusMatch[1] : raw;
  try {
    const parsed = JSON.parse(detail);
    if (parsed && typeof parsed.detail === "string") detail = parsed.detail;
  } catch {
    /* body wasn't JSON — use the text as-is */
  }
  detail = detail.trim();
  return detail || "Transcription failed";
}

export interface TranscribeDeps {
  toDataUrl?: (blob: Blob) => Promise<string>;
  post?: typeof fetchJSON;
}

/**
 * Upload a recorded audio blob and return the recognised (sanitised) text.
 * Always forces local mlx-whisper server-side; `language` maps the UI toggle
 * ("ru" | "auto") to the backend contract.
 */
export async function transcribeAudioBlob(
  blob: Blob,
  opts: { language: MicLanguage; mimeType?: string },
  deps: TranscribeDeps = {},
): Promise<string> {
  const toDataUrl = deps.toDataUrl ?? blobToDataUrl;
  const post = deps.post ?? fetchJSON;

  const dataUrl = await toDataUrl(blob);
  const res = await post<TranscribeResponse>("/api/audio/transcribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      data_url: dataUrl,
      mime_type: opts.mimeType || blob.type || undefined,
      language: opts.language === "ru" ? "ru" : "auto",
      provider: "mlx",
    }),
  });
  return sanitizeTranscript(res.transcript || "");
}
