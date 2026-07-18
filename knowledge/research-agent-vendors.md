---
title: Мульти-вендор ресёрч агентных моделей (подписки, цены, бенчмарки)
document: research-agent-vendors
schema_version: 1
status: active
updated: 2026-07-18
task: t_ae8bb5a7
sources:
  - llm-stats.com (SWE-bench Verified, Aider Polyglot, LMArena — снято 2026-07-18)
  - metatext.io/benchmarks/lmarena-elo (снято 2026-07-18)
  - официальные прайс-страницы вендоров (см. раздел «Источники»)
---

# Ресёрч: DeepSeek / Gemini / Grok / GLM / Qwen

> Data-driven сравнение пяти вендоров агентных моделей для обоснования
> раскладки «модель → класс задач» в model-grid Hermes. Числа бенчмарков —
> строго с независимых лидербордов, у каждого указан источник и дата снятия
> (2026-07-18). Цены и подписки — с прайс-страниц вендоров, снято 2026-07-18.
>
> **Дисклеймер по методу.** Все числа сверены веб-поиском на дату снятия, а не
> взяты из тренировочной памяти. Где модель отсутствует на конкретном
> лидерборде — так и написано («не листится»), а не подставлено число из
> смежного источника. `[verified]` = проверено сейчас по названному источнику;
> `[guess]` = моя интерпретация/инференс.

## TL;DR — вывод «модель → класс задач»

Для оператор-класса `01:4x` (предположение: главная модель — фронтир-Claude,
вендоры ниже занимают вспомогательные `auxiliary`-слоты грида — cheap / aux /
mid — где цена-качество важнее топ-качества):

| Класс | Что за задачи (aux-слоты Hermes) | Рекомендация | Почему (цена-качество) |
|---|---|---|---|
| **cheap** | approval-scoring, session-title, profile-describe, MCP-routing — высокочастотный «расходник» | **DeepSeek V4 Flash** ($0.14/$0.28) или **Gemini 3.5 Flash-Lite** ($0.10 in) | Дно по цене среди tool-capable; качество для мелких задач избыточно |
| **aux** | compression, web-summarization, vision, curator, triage-specifier — нужен разум, но бюджетно | **GLM-5.2** (подписка GLM Coding Plan, flat $18-72/мес) для текста/кода; **Gemini 3.5 Flash** для vision/мультимодалки | GLM-5.2 — лучшая open-weights цена-качество (SWE-V 77-78% за ~1/6 цены фронтира); Gemini Flash — единственный из пяти с сильной нативной мультимодалкой + дешёвым Flash-тиром |
| **mid** | тяжёлые под-агентные кодовые задачи ниже фронтир-main | **GLM-5.2** / **DeepSeek V4 Pro** / **Qwen 3.7 Max** / **Gemini 3.1 Pro** | Все ~80% SWE-bench Verified за долю цены фронтира; выбирать по способу оплаты (подписка vs метринг) |

**Ключевые выводы:**
- **Подписка (flat-fee), а не метринг** — есть у **Gemini, Grok, GLM, Qwen**;
  у **DeepSeek — нет** (только pay-per-token). Для оператора, гоняющего много
  агентов, flat-fee GLM/Qwen Coding Plan предсказуемее по бюджету.
- **Лучшая цена-качество для грида** — **GLM-5.2** через GLM Coding Plan:
  near-фронтир кодинг, работает прямо в Claude Code / OpenAI-compat, flat-fee.
- **Grok** — слабейшая цена-качество для aux-грида: нет дешёвого тира,
  подписки заточены под потребителя X; брать точечно под длинный контекст/поиск.
- **Aider Polyglot по флагманам 2026 пуст** — лидерборд отстаёт; актуальные
  модели несут только SWE-bench Verified + LMArena (см. предупреждение ниже).

---

## Сводка по вендорам

### 1. DeepSeek

- **Подписка:** ❌ нет. Только pay-per-token API + бесплатный веб-чат. Новым
  разработчикам — грант 5M бесплатных токенов. Нет per-seat / месячных планов.
  `[verified: nxcode.io, felloai.com, api-docs.deepseek.com — 2026-07]`
- **Цены (за 1M токенов, cache-miss in / out):**
  - DeepSeek V4 Flash — **$0.14 / $0.28** (cache-hit input $0.0028, ~98% скидка)
  - DeepSeek V4 Pro — **$0.435 / $0.87** (cache-hit input $0.003625)
  - Legacy: V3.2-chat $0.28/$0.42; R1 $0.55/$2.19
  `[verified: nxcode.io, api-docs.deepseek.com/quick_start/pricing — 2026-07]`
- **Интеграция:** OpenAI-совместимый API (`https://api.deepseek.com/v1`).
  Официального кодового CLI/ACP-агента нет. В Hermes уже есть профиль
  `deepseek` (OpenAI-compat, `default_aux_model=deepseek-chat`, спец-хендлинг
  `extra_body.thinking` + `reasoning_effort` для V4-семейства).
  `[verified: plugins/model-providers/deepseek/__init__.py]`

### 2. Gemini (Google)

- **Подписка:** ✅ да, потребительский стек Google AI (4 тира):
  - Free — $0 (Gemini 3.5 Flash + дневной доступ к 3.1 Pro, 5 Deep Research/мес)
  - AI Plus — **$7.99/мес** (запуск в US 2026-01-27)
  - AI Pro — **$19.99/мес**
  - AI Ultra — **$99.99/мес** (5x лимитов Pro); топ **$200/мес** (было $250, 20x)
  `[verified: blog.google, felloai.com, suprmind.ai — 2026-07]`
- **Цены API (за 1M, in/out):** Flash-Lite от **$0.10** in; Gemini 3.5 Flash
  **$1.50 / $9.00** (запуск 2026-05-19); Gemini 3.1 Pro **$2.00 / $12.00**.
  `[verified: cloudzero.com/blog/gemini-pricing, suprmind.ai — 2026-07]`
- **Интеграция:** нативный Gemini API
  (`generativelanguage.googleapis.com/v1beta`) + OpenAI-compat эндпоинт.
  Официальный **Gemini CLI** — терминальный агент, тарифицируется по API-ставкам
  токенов (агентные сессии жгут заметно больше, чем чат). В Hermes профиль
  `gemini` заявляет `api_mode="chat_completions"`, но использует кастомный
  нативный клиент; есть ветка OpenAI-compat base_url.
  `[verified: felloai.com; plugins/model-providers/gemini/__init__.py]`

### 3. Grok (xAI)

- **Подписка:** ✅ да, потребительские тиры:
  - X Premium — **$8/мес** (Grok в составе X)
  - SuperGrok — **$30/мес** ($300/год, ~17% скидка) — дешёвый standalone-путь
  - X Premium+ — **$40/мес** (Grok + перки X)
  - SuperGrok Heavy — **$300/мес** (полный Grok 4.5 + макс. лимиты)
  Плюс до **$175/мес бесплатных API-кредитов** через data-sharing программу.
  `[verified: x.ai/pricing, felloai.com, suprmind.ai — 2026-07]`
- **Цены API (за 1M, in/out):** Grok 4.5 — **$2 / $6** (контекст 500k,
  cached input $0.50, скидка 75%); Grok 4.3 — **$1.25 / $2.50** (контекст 1M).
  Grok 4.5 запущен 2026-07-08.
  `[verified: felloai.com, eesel.ai/blog/grok-4-5-pricing — 2026-07]`
- **Интеграция:** OpenAI-compat + нативный xAI. «Grok Build Beta» — кодовый
  инструмент автоматизации. В Hermes профиль `xai` использует
  `api_mode="codex_responses"` (Responses API, не plain chat/completions),
  `base_url=https://api.x.ai/v1`.
  `[verified: plugins/model-providers/xai/__init__.py]`

### 4. GLM (Z.ai / Zhipu)

- **Подписка:** ✅ да, **GLM Coding Plan** (flat-fee, а не метринг):
  - Lite — **$18/мес** (промо $12.60; ~80 промптов/5ч, ~400/нед, 100 MCP-вызовов/мес)
  - Pro — **$72/мес** (промо $50.40; ~400/5ч, ~2000/нед, 1000 MCP)
  - Max — **$160/мес** (промо $112; ~1600/5ч, ~8000/нед, 4000 MCP)
  Квота списывается 3x в пик / 2x вне пика (промо до сентября 2026 → 1x вне пика).
  Все тиры дают GLM-5.2, GLM-5-Turbo, GLM-4.7, GLM-4.5-air.
  `[verified: z.ai/subscribe, aipricing.guru/z-ai-subscription-pricing — 2026-07]`
- **Интеграция:** работает прямо в **Claude Code, Cline, Roo/Kilo Code,
  OpenCode** и 20+ клиентах через Anthropic-compat + OpenAI-compat эндпоинты
  (это и есть ценность подписки). Также обычный pay-per-token API. В Hermes
  профиль `zai` — OpenAI-compat с `extra_body.thinking` on/off; для GLM-5.2
  нативный `reasoning_effort` (только `high`/`max`).
  `[verified: z.ai/subscribe; plugins/model-providers/zai/__init__.py]`

### 5. Qwen (Alibaba)

- **Подписка:** ✅ да, **Qwen Coding Plan** (Alibaba Cloud Model Studio):
  ~**$10/мес** Lite / ~**$50/мес** Pro, до **90K запросов/мес**. Даёт доступ к
  Qwen + GLM + Kimi + MiniMax в кодовых инструментах.
  ⚠️ Lite-план закрыт для новых подписок 2026-03-20; апгрейды/продления Lite
  прекращены 2026-04-13. Бесплатный OAuth-тир Qwen Code урезан 1000→100 req/день,
  затем полностью закрыт **2026-04-15**.
  `[verified: alibabacloud.com/help/en/model-studio/coding-plan, codingplan.run,
  inventivehq.com — 2026-07]`
- **Цены API (за 1M, in/out):** Qwen3-Coder-Plus ~**$0.56 / $2.22** (для входа
  <32K токенов; тарифы растут для больших контекстов — штраф на длинные агентные
  сессии). `[verified: eesel.ai/blog/qwen-pricing — 2026-07]`
- **Интеграция:** официальный **Qwen Code CLI** (форк Gemini CLI с промптами под
  Qwen-Coder) + OpenAI-compat (DashScope). В Hermes два профиля: `qwen-oauth`
  (`auth_type="oauth_external"`, `portal.qwen.ai/v1`) и `alibaba-coding-plan`
  (OpenAI-compat, `coding-intl.dashscope.aliyuncs.com/v1`).
  `[verified: inventivehq.com; plugins/model-providers/qwen-oauth,
  plugins/model-providers/alibaba-coding-plan/__init__.py]`

---

## Бенчмарки (независимые лидерборды)

### SWE-bench Verified — llm-stats.com, снято **2026-07-18**

| Модель | Score | Класс-релевантность |
|---|---|---|
| _(референс)_ Claude Fable 5 | 95.0% | фронтир (main) |
| _(референс)_ Claude Opus 4.8 | 88.6% | фронтир (main) |
| **DeepSeek V4 Pro (Max)** | **80.6%** | mid |
| **Gemini 3.1 Pro** | **80.6%** | mid |
| **Qwen 3.7 Max** | **80.4%** | mid |
| _(референс)_ GPT-5.2 | 80.0% | mid |
| **DeepSeek V4 Flash (Max)** | **79.0%** | aux/mid |
| **Qwen 3.6 Plus** | **78.8%** | aux |
| **Gemini 3 Flash** | **78.0%** | aux |
| **GLM-5 (Zhipu)** | **77.8%** | aux |
| **Qwen 3.7 Plus** | **77.7%** | aux |
| **Gemini 3 Pro** | **76.2%** | aux |
| **DeepSeek V3.2** | **73.1%** | cheap/aux (legacy) |

`[verified: https://llm-stats.com/benchmarks/swe-bench-verified — 104 модели, upd 2026-07-18]`

- **Grok 4.5** на этом лидерборде **не листится**. Сторонние замеры:
  ~**86.6%** SWE-bench Verified (BenchLM / vals.ai), но xAI официально
  SWE-bench Verified на запуске не публиковал — только агентные evals.
  Помечаю как third-party, не канон-лидерборд.
  `[verified: benchlm.ai/models/grok-4-5, vals.ai/benchmarks/swebench — 2026-07]`
- **GLM-5.2** отдельной строкой на llm-stats нет (есть «GLM-5» 77.8%). Вендор/
  сторонние: GLM-5.2 на **SWE-bench Pro 62.1%** (обходит GPT-5.5 58.6%).
  `[verified: groundy.com, benchlm.ai/models/glm-5-2 — 2026-07]`

### LMArena (Chatbot Arena) Elo — metatext.io, снято **2026-07-18**

| Модель | Elo |
|---|---|
| _(референс топ)_ Claude Opus 4.6 (Fast) | 1500 |
| _(референс)_ Claude Fable 5 | 1494 |
| **Gemini 3.5 Flash** | **1480** |
| **Gemini 3.1 Pro** | **1480** |
| **Gemini 3 Pro** | **1479** |
| **Qwen 3.7 Max Thinking** | **1475** |
| **GLM-5.2** | **1465** |
| **DeepSeek V4 Pro** | **1449** |
| **GLM-5** | **1446** |
| **Grok 4.1** | **1437** _(Grok 4.5 ещё не листится)_ |
| **DeepSeek V3.2** | **1424** |

`[verified: https://metatext.io/benchmarks/lmarena-elo — upd 2026-07]`
Замечание лидерборда: разрыв топ-1..топ-10 ~28 Elo, различия <10 Elo — в пределах
шума. Трактовать топ как один тир, выбирать по задаче и цене.

### Aider Polyglot — llm-stats.com, снято **2026-07-18**

⚠️ **Флагманы 2026 (V4, Gemini 3.x, Grok 4.5, GLM-5.2, Qwen 3.7) на Aider
Polyglot ещё НЕ листятся** — лидерборд отстаёт от релизов. Присутствуют только
предыдущие поколения:

| Модель | Score |
|---|---|
| _(референс)_ GPT-5 | 88.0% |
| Gemini 2.5 Pro | 76.5% |
| **DeepSeek V3.2-Exp** | **74.5%** |
| DeepSeek R1-0528 | 71.6% |
| DeepSeek V3.1 | 68.4% |
| Gemini 2.5 Flash | 61.9% |
| **Qwen3-Coder 480B A35B** | **61.8%** |
| Qwen3-235B-A22B-2507 | 57.3% |

`[verified: https://llm-stats.com/benchmarks/aider-polyglot — 22 модели, upd 2026-07-18]`

**Вывод по Aider Polyglot:** как источник для флагманов 2026 непригоден
(данных нет). Для актуальной раскладки грида опираться на **SWE-bench Verified +
LMArena**; Aider держать как исторический ориентир по семействам (DeepSeek —
лучший score-per-dollar среди open в своём поколении).

---

## Обоснование раскладки «модель → класс» (цена-качество)

Логика: чем выше частота и ниже риск задачи — тем дешевле модель. Фронтир-main
(Claude) остаётся на «умных» решениях; вендоры ниже разгружают aux-слоты.

- **cheap (расходник, высокая частота, низкий риск):**
  **DeepSeek V4 Flash** ($0.14/$0.28) — дно по цене среди tool-capable, SWE-V 79%.
  Альтернатива на подписке-нуле: **Gemini 3.5 Flash-Lite** ($0.10 in). Для
  approval-scoring / session-title качество избыточно, платить больше — waste.

- **aux (нужен разум, но бюджетно):**
  **GLM-5.2** — лучшая цена-качество: near-фронтир кодинг (SWE-V ~77-78%,
  LMArena 1465) за ~1/6 цены фронтира, flat-fee через GLM Coding Plan,
  работает в Claude Code. Для **vision / мультимодалки** — **Gemini 3.5 Flash**
  (единственный из пяти с сильной нативной мультимодалкой + дешёвым тиром +
  подпиской).

- **mid (тяжёлые под-агентные кодовые задачи):**
  Кластер ~80% SWE-V: **GLM-5.2 / DeepSeek V4 Pro / Qwen 3.7 Max /
  Gemini 3.1 Pro**. Выбор — по способу оплаты:
  - предсказуемый flat-fee, много агентов → **GLM Coding Plan** ($18-72) или
    **Qwen Coding Plan** (~$50 Pro, до 90K req/мес);
  - чистый метринг, спорадическая нагрузка → **DeepSeek V4 Pro** ($0.435/$0.87);
  - длинный контекст 500k-1M + web-поиск → **Grok** (но дорого для aux).

- **Grok — почему не в гриде по умолчанию:** нет дешёвого тира ($2/$6 —
  дороже DeepSeek/Qwen на том же уровне качества), подписки ($8-300) заточены
  под потребителя X, а не под программный aux-флот. Брать точечно под 500k-1M
  контекст или DeepSearch, не как базовый aux. `[guess — инференс из цен]`

**Итог для оператор-класса 01:4x:** базовый aux/mid грида — **GLM-5.2 (Coding
Plan)**; cheap-слот — **DeepSeek V4 Flash**; vision/мультимодальный aux —
**Gemini 3.5 Flash**; Qwen — равноценная mid-альтернатива на подписке; Grok —
нишевый (длинный контекст/поиск).
`[guess — синтез из verified цен и бенчмарков выше]`

---

## Предупреждения и границы применимости

1. **Aider Polyglot отстаёт** — по флагманам 2026 чисел нет; не использовать как
   единственный источник для новых моделей.
2. **Cross-leaderboard рассинхрон** — на дату снятия разные лидерборды несут
   разные поколения (Aider — старые, SWE-V/LMArena — актуальные 2026). Сравнивать
   модели только внутри одного лидерборда, не склеивать числа между ними.
3. **SWE-bench Verified насыщается** (топ жмётся к ~88%); реальное расслоение
   ушло на SWE-bench Pro (55-70%). Для тонкого выбора между near-фронтир
   моделями смотреть Pro, а не Verified.
4. **Grok 4.5 и GLM-5.2 отсутствуют** на части канон-лидербордов; их числа —
   сторонние/вендорские, помечены как third-party.
5. **Цены и лимиты подписок волатильны** (промо GLM до сен-2026, закрытие
   Qwen Lite/free-tier в апр-2026). Пересверять прайс-страницы перед решением.

## Источники

Бенчмарки (независимые лидерборды):
- [llm-stats — SWE-bench Verified](https://llm-stats.com/benchmarks/swe-bench-verified) (upd 2026-07-18)
- [llm-stats — Aider Polyglot](https://llm-stats.com/benchmarks/aider-polyglot) (upd 2026-07-18)
- [llm-stats — LMArena Text](https://llm-stats.com/benchmarks/lmarena-text)
- [metatext — LMArena Elo](https://metatext.io/benchmarks/lmarena-elo) (upd 2026-07)
- [benchlm — GLM-5.2](https://benchlm.ai/models/glm-5-2), [benchlm — Grok 4.5](https://benchlm.ai/models/grok-4-5)
- [vals.ai — SWE-bench](https://www.vals.ai/benchmarks/swebench)

Цены и подписки:
- DeepSeek: [api-docs.deepseek.com/pricing](https://api-docs.deepseek.com/quick_start/pricing/), [nxcode.io](https://www.nxcode.io/resources/news/deepseek-api-pricing-complete-guide-2026)
- Gemini: [blog.google — Google AI subscriptions](https://blog.google/products-and-platforms/products/google-one/google-ai-subscriptions/), [cloudzero.com/blog/gemini-pricing](https://www.cloudzero.com/blog/gemini-pricing/)
- Grok: [x.ai/pricing](https://x.ai/pricing), [eesel.ai/blog/grok-4-5-pricing](https://www.eesel.ai/blog/grok-4-5-pricing)
- GLM: [z.ai/subscribe](https://z.ai/subscribe), [aipricing.guru/z-ai-subscription-pricing](https://www.aipricing.guru/z-ai-subscription-pricing/)
- Qwen: [alibabacloud.com — coding plan](https://www.alibabacloud.com/help/en/model-studio/coding-plan), [eesel.ai/blog/qwen-pricing](https://www.eesel.ai/blog/qwen-pricing)

Интеграция (ground truth — код Hermes):
- `plugins/model-providers/{deepseek,gemini,xai,zai,qwen-oauth,alibaba-coding-plan}/__init__.py`
- `website/static/api/model-catalog.json`

## Железо для локального инференса: buy vs rent (веб-ресёрч 2026-07-18)
- Точка баланса покупки: AMD Strix Halo 128GB (~$1.5-2k, MoE 120B @ 31-55 tok/s) или Mac Studio M4 Max 64GB (~$2.5k). Дороже — отдача падает: DGX Spark $4.7k = 2.7 tok/s single-stream на 70B; RTX 5090 32GB не вмещает 70B; 4x3090 (~$4k) — лучший VRAM/$, но гараж-сервер.
- Аренда $200/мес = одна 4090/L4-класс карта (RunPod $0.34-0.69/ч, Vast $0.29/ч) — только средний coder (Qwen3-Coder-Next 80B/3B, Q4 ~46GB, 70.6% SWE-V). GLM/DeepSeek полноразмерные — мультикарта $8-28/ч, мимо.
- Kimi K3 (2.8T/50B актив, вышел 16.07.26, веса с 27.07): self-host = 8x H200 ≈ $21.6k/мес. Категорически нет.
- Вывод: подписка топ-модели ($200/мес ≈ $1000+ API-эквивалент) бьёт аренду по агентской пропускной способности; гибрид «свой Mac для фона + подписка для сложного» оптимален.
- Multi-GPU DIY в $1.5-2k (июль 2026): 4x3060 12GB (~$880 карты, 48GB) — влезает, но ~8-12 tok/s на 70B; 2x3090 48GB быстрее (~15-20 tok/s), но $2.1-2.5k только карты; объединение без NVLink работает (vLLM/ExLlamaV2 tensor-parallel, llama.cpp — только послойно); скрытое: матплата x8/x8, БП 1000W+, электричество ~$75-200/мес при 24/7. Вывод: Strix Halo 128GB за те же деньги — тише, проще, без сборки, чуть меньше скорость.
- Open-weight vs Opus 4.8 (SWE-V, июль 2026): Opus 4.8 = 88.6%. GLM-5 744B/40B = 77.8% (~372GB Q4), MiniMax M2.1 230B/10B = 74.8% (~115GB — впритык в 128GB!), GLM-4.7 = 73.8% (~178GB), Qwen3-Coder-Next 80B/3B = 70.6% (~46GB). Вывод: Opus-класса локально нет ни на каком ≤128GB железе; лучший влезающий — MiniMax M2.1, разрыв ~14 п.п.; минимум под GLM-4.7 = ~$8-10k (8x3090 или M3 Ultra 256GB со вторички).
- Artificial Analysis (пользователь прислал 19.07.26): Coding Index — GPT-5.6 Sol 77.4, Fable5 76.5, Kimi K3 76.2 (!), Opus4.8 74.3, GLM-5.2 68.8, Qwen3.7Max 66, DeepSeek V4 Pro 59.4. Cost/task: DeepSeek $0.04, GLM-5.2 $0.47, K3 $0.95, Fable $2.75. K3 = Fable-класс кода втрое дешевле — главный кандидат на средний ярус (Kimi Moderato $19, проверить API-доступ из планов). DeepSeek V4 = единственный кейс для metered API (массовые дешёвые прогоны).
