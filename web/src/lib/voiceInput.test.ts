import { describe, expect, it, vi } from "vitest";

import {
  AUDIO_MIME_CANDIDATES,
  describeTranscribeError,
  pickAudioMimeType,
  sanitizeTranscript,
  transcribeAudioBlob,
} from "./voiceInput";

// The lib runs in the `node` vitest env (no DOM), so browser globals and the
// api client are stubbed via dependency injection or per-test fakes.

describe("pickAudioMimeType", () => {
  it("returns the first supported candidate", () => {
    const supported = pickAudioMimeType((t) => t === "audio/mp4");
    expect(supported).toBe("audio/mp4");
  });

  it("prefers earlier candidates", () => {
    const supported = pickAudioMimeType(() => true);
    expect(supported).toBe(AUDIO_MIME_CANDIDATES[0]);
  });

  it("returns empty string when nothing is supported", () => {
    expect(pickAudioMimeType(() => false)).toBe("");
  });

  it("treats a throwing check as unsupported", () => {
    expect(
      pickAudioMimeType(() => {
        throw new Error("boom");
      }),
    ).toBe("");
  });
});

describe("sanitizeTranscript", () => {
  it("collapses newlines and whitespace so injection can't submit", () => {
    expect(sanitizeTranscript("hello\nworld\r\n  again ")).toBe(
      "hello world again",
    );
  });

  it("trims to empty when only whitespace", () => {
    expect(sanitizeTranscript("  \n\t ")).toBe("");
  });
});

describe("describeTranscribeError", () => {
  it("unwraps a FastAPI detail envelope from a fetchJSON status error", () => {
    const err = new Error(
      '400: {"detail": "mlx-whisper is not installed."}',
    );
    expect(describeTranscribeError(err)).toBe("mlx-whisper is not installed.");
  });

  it("falls back to the raw body when it is not JSON", () => {
    expect(describeTranscribeError(new Error("500: boom"))).toBe("boom");
  });

  it("handles non-Error throwables", () => {
    expect(describeTranscribeError("nope")).toBe("nope");
  });

  it("returns a default when the message is empty", () => {
    expect(describeTranscribeError(new Error("500: "))).toBe(
      "Transcription failed",
    );
  });
});

describe("transcribeAudioBlob", () => {
  const blob = { type: "audio/webm" } as Blob;

  it("posts a base64 data URL, forces mlx, maps ru, and sanitises the result", async () => {
    const post = vi.fn().mockResolvedValue({ ok: true, transcript: "  привет\nмир " });
    const toDataUrl = vi.fn().mockResolvedValue("data:audio/webm;base64,AAAA");

    const text = await transcribeAudioBlob(
      blob,
      { language: "ru", mimeType: "audio/webm" },
      { post, toDataUrl },
    );

    expect(text).toBe("привет мир");
    expect(post).toHaveBeenCalledTimes(1);
    const [url, init] = post.mock.calls[0];
    expect(url).toBe("/api/audio/transcribe");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string);
    expect(body).toMatchObject({
      data_url: "data:audio/webm;base64,AAAA",
      mime_type: "audio/webm",
      language: "ru",
      provider: "mlx",
    });
  });

  it("maps the auto toggle to the 'auto' language contract", async () => {
    const post = vi.fn().mockResolvedValue({ ok: true, transcript: "hi" });
    const toDataUrl = vi.fn().mockResolvedValue("data:audio/webm;base64,AAAA");

    await transcribeAudioBlob(blob, { language: "auto" }, { post, toDataUrl });

    const body = JSON.parse(post.mock.calls[0][1].body as string);
    expect(body.language).toBe("auto");
    expect(body.provider).toBe("mlx");
  });

  it("propagates transcription errors to the caller", async () => {
    const post = vi.fn().mockRejectedValue(
      new Error('400: {"detail": "Microphone empty"}'),
    );
    const toDataUrl = vi.fn().mockResolvedValue("data:audio/webm;base64,AAAA");

    await expect(
      transcribeAudioBlob(blob, { language: "auto" }, { post, toDataUrl }),
    ).rejects.toThrow("Microphone empty");
  });
});
