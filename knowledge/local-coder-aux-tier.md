# Локальный кодер как дешёвый aux-тир (Qwen3-Coder-30B через Ollama)

Задача `t_2cfa06c1`. Цель: дешёвый/aux-тир (классификация, простые правки,
подбор тулсета) исполняется локальной ~30B-моделью на 64GB Mac без расхода
токенов подписки. Снято 2026-07-18.

## TL;DR — что выбрано и почему

**Выбор: `qwen3-coder:30b`** = Qwen3-Coder-30B-A3B-Instruct, MoE 30.5B суммарно /
3.3B активных, квант Q4_K_M ≈ 19 ГБ, через **Ollama** (OpenAI-совместимый endpoint
`http://localhost:11434/v1`). `[факт: ollama.com/library/qwen3-coder:30b,
huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct — 2026-07-18]`

Почему именно эта модель для **aux-тира** (а не топ-качество):

- **MoE даёт скорость на дешёвом тире.** Активны только 3.3B из 30.5B параметров,
  поэтому на M4 Pro она генерит быстро (репорты ~36 tok/s на M3 Max 64GB) при
  памяти уровня 19 ГБ. Для aux-задач (короткие классификации, подбор тулсета)
  важнее latency и стоимость, чем максимум качества. `[факт: promptquorum.com,
  willitrunai.com/macs/m3-max-64gb — 2026-07-18]` `[домысел: точный tok/s на
  M4 Pro не мерил, порядок величины из репортов на M3 Max]`
- **Нативный tool-calling.** Qwen3-Coder имеет специальный function-call формат
  (Qwen Code / CLINE), что прямо нужно для «подбора тулсета». Dense Qwen2.5 и
  старый DeepSeek-Coder на агентности/тулколлинге в 2026 слабее.
  `[факт: huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct — 2026-07-18]`
- **Влезает в бюджет.** 19 ГБ веса + KV-кэш при умеренном контексте (32–64k, а не
  нативные 262k) — заметно под ~32 ГБ, оставляя ~44 ГБ ОС и остальному.
  Полный 262k-контекст просит ~250 ГБ — его не используем.
  `[факт: ollama model card, unsloth docs — 2026-07-18]`

## Сравнение кандидатов (64GB Mac, янв 2026)

| Модель | Тип | Q4 размер | SWE-bench Verified | Для aux-тира |
|---|---|---|---|---|
| **Qwen3-Coder-30B-A3B** | MoE 3.3B акт. | ~19 ГБ | **50.3%** `[факт]` | **выбор** — быстрый, tool-calling |
| Qwen2.5-Coder-32B | dense 32B | ~20 ГБ | выше по «сырому» кодингу, но медленнее (32B активных) и старее (2024) | fallback для «медленно, но качественнее» |
| DeepSeek-Coder(-V2) | 2024-era | 16B/33B/236B | не топ для агентности 2026 | нет |

- Qwen3-Coder-30B-A3B: **50.3% Pass@1 SWE-bench Verified**. `[факт:
  huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct model card +
  artificialanalysis.ai/models/qwen3-coder-30b-a3b-instruct — 2026-07-18]`
- Общий вывод обзоров «лучший локальный кодер под 24–34B-класс на Apple Silicon,
  янв 2026»: Qwen3-Coder-30B-A3B — практический дефолт; Qwen2.5-Coder-32B — «быстрый
  вариант для мелких задач» на 64GB. `[факт: tembo.io/blog/best-local-llm-for-coding,
  whatllm.org/best-local-llm, promptquorum.com/local-llms/best-models-apple-silicon-2026
  — 2026-07-18]`
- Для aux-тира скорость > максимум качества, поэтому MoE (3.3B активных) бьёт
  dense-32B. Dense Qwen2.5-Coder-32B оставлен как задокументированный fallback
  (`HERMES_LOCAL_AUX_MODEL=qwen2.5-coder:32b`), если качество aux-выхода окажется
  недостаточным. `[домысел: инженерный вывод из бенчей + природы тира]`

## Как подключено (архитектура)

Два места, обе data-driven, обе с graceful-fallback:

1. **Роутер** — `hermes_cli/model_grid.py` + `website/static/api/model-grid.json`.
   Добавлены строки `provider: local, local: true` в классы `cheap` и `aux`.
   Новый режим `_local_pick`: локальная модель бесплатна, поэтому для offload-классов
   (`cheap`/`aux`) выигрывает у любого платного вендора по стоимости. `route(cls)`
   отдаёт `mode="local"`; `route(cls, allow_local=False)` возвращает платный выбор
   (если локалка заведомо мертва).
2. **Исполнитель** — `agent/auxiliary_client.py`. Первоклассный провайдер `local`
   (алиасы `ollama`/`mlx`/`local-openai`/…):
   - ветка `if provider == "local":` в `resolve_provider_client` — строит
     OpenAI-клиент на `base_url` локалки с ключом `no-key-required`, gated
     проверкой доступности (короткий TCP-connect, кэш 20с);
   - `_try_local_openai` первым в `_get_provider_chain()` — при `enabled` +
     доступности перехватывает auto-цепочку до платных провайдеров;
   - при недоступности возвращает `(None, None)` → `call_llm` мягко уходит в
     обычную auto-цепочку, ничего не падает.

Конфиг: `auxiliary.local_model.{enabled,base_url,model}` в config.yaml, либо env
`HERMES_LOCAL_AUX_{ENABLED,BASE_URL,MODEL}`. По умолчанию `enabled: false`
(opt-in) — на чужих машинах без Ollama поведение не меняется.

## Развёртывание (сделано на этой машине)

- Хост: Mac16,11 (Apple M4 Pro, 64 ГБ). `[факт: sysctl — 2026-07-18]`
- `brew install ollama` → Ollama 0.32.1; `ollama serve`; `ollama pull qwen3-coder:30b`.
- Endpoint: `http://localhost:11434/v1` (OpenAI wire). `[факт: /api/version → 0.32.1]`

## Измерение качества aux-тира

**Интеграция доказана end-to-end.** `scripts/eval_local_aux.py` гоняет реальные
задачи тира через тот же путь, что и прод — `call_llm(provider="local", …)` →
локальный OpenAI-endpoint Ollama — и структурно подтверждает **ноль токенов
подписки** (клиент резолвится на `localhost` с `no-key-required`, пул подписок
не арендуется; проверяется ассертом на `base_url`). `[факт: прогон
eval_local_aux.py — 2026-07-18]`

**Blocker на прод-модель: диск заполнен.** Целевая `qwen3-coder:30b` (~18.5 ГБ
Q4_K_M) не влезла — том данных на 100% (свободно <10 ГБ; был ~96% ещё до задачи).
Ollama 0.32.1 и endpoint установлены и работают; не хватает только весов 30B.
Чтобы доделать AC #1/#4: освободить ~20 ГБ и `ollama pull qwen3-coder:30b`, затем
`python3 scripts/eval_local_aux.py --write-doc`. `[факт: df -h + ollama pull
"no space left on device" — 2026-07-18]`

**Smoke-тест на модели-заглушке** (`qwen2.5-coder:3b`, ~1.9 ГБ — влез; НЕ
тир-качество прод-модели, а доказательство работоспособности пайплайна):

| Кейс | Категория | Результат (3B-заглушка) | Latency |
|---|---|---|---|
| classify-intent | classification | ✅ pass | 0.2s |
| simple-edit | simple_edit | ✅ pass (`is not None`) | 0.3s |
| toolset-pick | toolset_selection | ⚠️ fail — 3B выбрал только `edit_file` | 0.2s |

Итог заглушки: **2/3, среднее 0.2s/вызов, 0 токенов подписки**. Промах на подборе
тулсета — ожидаемая слабость 3B; прод-Qwen3-Coder-30B с нативным tool-calling и
50.3% SWE-V должен закрывать этот кейс лучше. Прогон на 30B заполнит настоящую
строку тир-качества. `[факт: прогон eval_local_aux.py на qwen2.5-coder:3b —
2026-07-18]` `[домысел: улучшение на 30B — вывод из бенчей, не измерено]`
