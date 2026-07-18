#!/usr/bin/env python3
"""Measure the local coder backend on real aux-tier tasks — task t_2cfa06c1.

Exercises the SAME resolution path production uses (``call_llm(provider="local")``)
against the local OpenAI-compatible endpoint (Ollama / Qwen3-Coder-30B by default),
on three representative tiers of side work:

    1. classification      — pick a label from a fixed set
    2. simple edit         — apply a one-line code change
    3. toolset selection   — choose the relevant tools as JSON

Each case has a programmatic validator, so "quality" is a concrete pass rate,
not a vibe. Latency is wall-clock per call. Because ``provider="local"`` targets
localhost with ``no-key-required``, the run spends ZERO subscription/metered
tokens — that is the whole point and is asserted structurally (the resolved
client's base_url must be the local endpoint, never a subscription pool).

Usage:
    .venv/bin/python scripts/eval_local_aux.py [--model qwen3-coder:30b]
                                               [--base-url http://localhost:11434/v1]
                                               [--write-doc]

``--write-doc`` splices the results table into the MEASUREMENT section of
knowledge/local-coder-aux-tier.md.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.auxiliary_client import call_llm, resolve_provider_client  # noqa: E402

DOC_PATH = ROOT / "knowledge" / "local-coder-aux-tier.md"


def _content(resp) -> str:
    try:
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""


# ── Cases: (name, category, messages, validator) ─────────────────────────────

def _v_classify(out: str) -> bool:
    return re.search(r"\bbugfix\b", out.lower()) is not None and "feature" not in out.lower()[:40]


def _v_edit(out: str) -> bool:
    # Correct fix returns True only when id is present: either "id != None"
    # or the idiomatic "id is not None". Both are accepted.
    body = out.lower()
    inverted = "!= none" in body or "is not none" in body
    return inverted and "return" in body


def _v_toolset(out: str) -> bool:
    m = re.search(r"\[.*\]", out, re.DOTALL)
    if not m:
        return False
    try:
        tools = {str(t).strip().lower() for t in json.loads(m.group(0))}
    except Exception:
        return False
    # Reading a file to fix a typo needs read+edit, not shell/web/git.
    return {"read_file", "edit_file"} <= tools and "web_search" not in tools


CASES = [
    (
        "classify-intent", "classification",
        [
            {"role": "system", "content":
                "Classify the software task into exactly one label: "
                "bugfix | feature | refactor | docs. Answer with the single label word only."},
            {"role": "user", "content":
                "The login button throws a null pointer when email is empty; users can't sign in."},
        ],
        _v_classify,
    ),
    (
        "simple-edit", "simple_edit",
        [
            {"role": "system", "content":
                "You edit code. Return ONLY the corrected function body, no prose, no fences."},
            {"role": "user", "content":
                "This guard is inverted — it should reject when the id is MISSING, not present. Fix it:\n"
                "def check(id):\n    if id == None:\n        return True\n    return False\n"
                "Make it return True only when id is not None."},
        ],
        _v_edit,
    ),
    (
        "toolset-pick", "toolset_selection",
        [
            {"role": "system", "content":
                "Given a task and this tool catalog: "
                "[read_file, edit_file, run_shell, web_search, git_commit]. "
                "Return a JSON array of ONLY the tools needed. No prose."},
            {"role": "user", "content":
                "Fix a typo in the string 'recieve' inside utils.py."},
        ],
        _v_toolset,
    ),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="override local model id")
    ap.add_argument("--base-url", default=None, help="override local endpoint")
    ap.add_argument("--write-doc", action="store_true")
    args = ap.parse_args()

    import os
    os.environ["HERMES_LOCAL_AUX_ENABLED"] = "1"
    if args.base_url:
        os.environ["HERMES_LOCAL_AUX_BASE_URL"] = args.base_url
    if args.model:
        os.environ["HERMES_LOCAL_AUX_MODEL"] = args.model

    # Structural zero-token proof: the resolved client must point at the local
    # endpoint, not a subscription pool (acp://…) or a cloud host.
    client, model = resolve_provider_client("local")
    if client is None:
        print("FAIL: local endpoint not reachable — start `ollama serve` and pull the model.")
        return 2
    base_url = str(getattr(client, "base_url", "")) or "?"
    assert "localhost" in base_url or "127.0.0.1" in base_url, \
        f"local client must target localhost, got {base_url!r}"
    assert not base_url.startswith("acp://"), "must not touch the subscription pool"
    print(f"resolved local backend: model={model} base_url={base_url}\n")

    rows = []
    for name, category, messages, validate in CASES:
        t0 = time.monotonic()
        try:
            resp = call_llm(provider="local", messages=messages, temperature=0.0,
                            max_tokens=400, timeout=120)
            out = _content(resp)
            ok = validate(out)
            err = ""
        except Exception as exc:                       # noqa: BLE001
            out, ok, err = "", False, f"{type(exc).__name__}: {exc}"
        dt = time.monotonic() - t0
        rows.append((name, category, ok, dt, err, out))
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name:16} {category:18} {dt:5.1f}s"
              + (f"  err={err}" if err else ""))
        if not ok and not err:
            print(f"        output: {out[:160]!r}")

    passed = sum(1 for r in rows if r[2])
    avg = sum(r[3] for r in rows) / len(rows)
    print(f"\n{passed}/{len(rows)} passed · avg {avg:.1f}s/call · "
          f"model={model} · 0 subscription tokens (local endpoint)")

    if args.write_doc:
        _write_doc(model, base_url, rows, passed, avg)
        print(f"wrote results into {DOC_PATH.relative_to(ROOT)}")
    return 0 if passed == len(rows) else 1


def _write_doc(model, base_url, rows, passed, avg) -> None:
    lines = [
        "## Измерение качества aux-тира",
        "",
        f"Прогон `scripts/eval_local_aux.py` на `{model}` "
        f"(endpoint `{base_url}`), температура 0, реальные задачи тира. "
        f"Каждый кейс проверяется программным валидатором (точный лейбл / "
        f"свойство кода / парс JSON), а не на глаз.",
        "",
        "| Кейс | Категория | Результат | Latency |",
        "|---|---|---|---|",
    ]
    for name, category, ok, dt, err, _out in rows:
        verdict = "✅ pass" if ok else ("⚠️ " + (err or "fail"))
        lines.append(f"| {name} | {category} | {verdict} | {dt:.1f}s |")
    lines += [
        "",
        f"**Итог: {passed}/{len(rows)} пройдено, среднее {avg:.1f}s/вызов, "
        f"0 токенов подписки** (вызовы шли на localhost с `no-key-required`, "
        f"пул подписок не арендовался). `[факт: прогон eval_local_aux.py — 2026-07-18]`",
        "",
    ]
    block = "\n".join(lines)
    text = DOC_PATH.read_text(encoding="utf-8")
    marker = "## Измерение качества aux-тира"
    idx = text.index(marker)
    DOC_PATH.write_text(text[:idx] + block, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
