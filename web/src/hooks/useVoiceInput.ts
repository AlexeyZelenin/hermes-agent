/**
 * useVoiceInput — MediaRecorder orchestration for the chat composer mic button.
 *
 * A single `toggle()` drives the whole flow:
 *   idle → (getUserMedia + MediaRecorder) → recording
 *   recording → stop → processing → transcribe → onText(text) → idle
 *
 * Errors (mic denied, unsupported, empty capture, transcription failure) are
 * surfaced via `error` and leave the composer fully usable. The recognition
 * language ("ru" | "auto") is persisted in localStorage; default "auto".
 */
import { useCallback, useEffect, useRef, useState } from "react";

import {
  type MicLanguage,
  describeTranscribeError,
  pickAudioMimeType,
  transcribeAudioBlob,
} from "@/lib/voiceInput";

export type VoiceStatus = "idle" | "recording" | "processing";

const LANGUAGE_STORAGE_KEY = "hermes.voiceInput.language";

function loadLanguage(): MicLanguage {
  try {
    return window.localStorage.getItem(LANGUAGE_STORAGE_KEY) === "ru"
      ? "ru"
      : "auto";
  } catch {
    return "auto";
  }
}

export interface UseVoiceInput {
  status: VoiceStatus;
  error: string | null;
  language: MicLanguage;
  supported: boolean;
  setLanguage: (language: MicLanguage) => void;
  toggle: () => void;
  clearError: () => void;
}

export function useVoiceInput(onText: (text: string) => void): UseVoiceInput {
  const [status, setStatus] = useState<VoiceStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [language, setLanguageState] = useState<MicLanguage>(loadLanguage);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);

  // Read the latest values inside async recorder callbacks without stale closures.
  const languageRef = useRef(language);
  languageRef.current = language;
  const onTextRef = useRef(onText);
  onTextRef.current = onText;

  const supported =
    typeof navigator !== "undefined" &&
    !!navigator.mediaDevices &&
    typeof navigator.mediaDevices.getUserMedia === "function" &&
    typeof MediaRecorder !== "undefined";

  const stopStream = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }, []);

  const setLanguage = useCallback((next: MicLanguage) => {
    setLanguageState(next);
    try {
      window.localStorage.setItem(LANGUAGE_STORAGE_KEY, next);
    } catch {
      /* private mode / storage blocked — in-memory only */
    }
  }, []);

  const clearError = useCallback(() => setError(null), []);

  const start = useCallback(async () => {
    setError(null);
    if (!supported) {
      setError("Voice input isn't supported in this browser.");
      return;
    }

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setError(
        "Microphone access was blocked. Allow it in your browser settings and try again.",
      );
      return;
    }
    streamRef.current = stream;
    chunksRef.current = [];

    const mimeType = pickAudioMimeType();
    let recorder: MediaRecorder;
    try {
      recorder = new MediaRecorder(
        stream,
        mimeType ? { mimeType } : undefined,
      );
    } catch {
      stopStream();
      setError("Couldn't start the recorder on this device.");
      return;
    }
    recorderRef.current = recorder;

    recorder.ondataavailable = (event: BlobEvent) => {
      if (event.data && event.data.size > 0) chunksRef.current.push(event.data);
    };

    recorder.onstop = async () => {
      stopStream();
      const type = recorder.mimeType || mimeType || "audio/webm";
      const blob = new Blob(chunksRef.current, { type });
      chunksRef.current = [];
      recorderRef.current = null;

      if (blob.size === 0) {
        setStatus("idle");
        setError("No audio was captured. Try again.");
        return;
      }

      setStatus("processing");
      try {
        const text = await transcribeAudioBlob(blob, {
          language: languageRef.current,
          mimeType: type,
        });
        if (text) onTextRef.current(text);
        else setError("Nothing was recognised. Try speaking more clearly.");
      } catch (err) {
        setError(describeTranscribeError(err));
      } finally {
        setStatus("idle");
      }
    };

    try {
      recorder.start();
    } catch {
      stopStream();
      recorderRef.current = null;
      setStatus("idle");
      setError("Couldn't start recording on this device.");
      return;
    }
    setStatus("recording");
  }, [supported, stopStream]);

  const stop = useCallback(() => {
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.stop(); // → onstop → processing → transcribe
    } else {
      setStatus("idle");
    }
  }, []);

  const toggle = useCallback(() => {
    if (status === "recording") stop();
    else if (status === "idle") void start();
    // "processing": ignore taps until the transcript resolves.
  }, [status, start, stop]);

  // Tear down any live recorder/stream if the page unmounts mid-recording.
  useEffect(() => {
    return () => {
      try {
        recorderRef.current?.stop();
      } catch {
        /* already stopped */
      }
      streamRef.current?.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    };
  }, []);

  return {
    status,
    error,
    language,
    supported,
    setLanguage,
    toggle,
    clearError,
  };
}
