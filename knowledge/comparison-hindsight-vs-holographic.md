---
title: Hindsight vs Holographic — сравнение систем памяти + оценка локальной модели
document: comparison-hindsight-vs-holographic
schema_version: 1
status: active
updated: 2026-07-18
task: t_773ee299
sources:
  - plugins/memory/holographic/ (исходники Hermes — ground truth Holographic)
  - plugins/memory/hindsight/ (исходники Hermes — ground truth интеграции Hindsight)
  - github.com/vectorize-io/hindsight (репо, LICENSE, README — снято 2026-07-18)
  - vectorize.io/hindsight, vectorize.io/pricing (вендор — снято 2026-07-18)
  - venturebeat.com Hindsight coverage (снято 2026-07-18)
---

# Ресёрч: Hindsight vs Holographic для памяти Hermes

> Data-driven сравнение двух memory-провайдеров, **которые оба уже реализованы
> как плагины в Hermes** (`plugins/memory/holographic/`, `plugins/memory/hindsight/`).
> Речь не о «внедрить Hindsight с нуля», а о выборе активного провайдера: активен
> может быть только один внешний провайдер за раз (`memory.provider`), встроенная
> MEMORY.md/USER.md работает поверх любого.
>
> **Метод.** Механика каждой системы сверена по исходникам (цитирую `file:line`),
> внешние факты о продукте Hindsight (лицензия, зрелость, цена, бенчмарк) —
> веб-поиском по первоисточникам на 2026-07-18. `[факт]` = проверено сейчас по
> названному источнику; `[домысел]` = мой инференс/оценка, не из источника.

## TL;DR — рекомендация

**Не менять Holographic по умолчанию.** Он и есть текущий выбор, и он лучше всего
ложится на local-first этику Hermes: ноль внешних зависимостей, ноль сети, ноль
LLM-вызовов, ноль стоимости, работает офлайн. `[факт: README + код ниже]`

**Переходить на Hindsight стоит только при выполнении ВСЕХ трёх условий:**
1. оператор реально упирается в качество ретрива Holographic на длинной
   мультисессионной истории (сотни+ фактов), а не гипотетически;
2. готов держать инфраструктуру памяти: PostgreSQL-демон + LLM (локальный или
   облачный) для извлечения фактов и синтеза;
3. ему нужен слой «наблюдений/убеждений» (reflect-синтез), которого у
   Holographic нет.

Если нужен именно Hindsight, но без vendor-lock — брать режим `local_embedded` с
локальной OpenAI-совместимой моделью (см. раздел про локальную модель), а не
Cloud.

| Ось | Holographic | Hindsight |
|---|---|---|
| Где живёт | локально, SQLite | Cloud / локальный Postgres / внешний инстанс |
| Нужен LLM для памяти | **нет** (регекс-извлечение) | **да** (retain + reflect) |
| Сеть | не нужна | нужна (Cloud) или локальный демон |
| Стоимость | бесплатно | Cloud pay-per-token / self-host бесплатно (+ ресурсы) |
| Vendor-lock | нет | средний (Cloud) / низкий (OSS, MIT) |
| Качество ретрива | хорошее на малом/среднем объёме | SOTA на длинной истории (вендор-бенчмарк) |
| Зрелость кода | внутренний плагин Hermes | внешний продукт, ~18.5k★, MIT |

---

## 1. Holographic — механика (ground truth: исходники)

Локальное SQLite-хранилище фактов с гибридным ретривом и HRR-алгеброй. Полностью
внутри процесса Hermes, без сети и без LLM.

- **Хранилище:** локальный SQLite (`$HERMES_HOME/memory_store.db`), таблицы
  `facts` + FTS5-индекс `facts_fts`, `entities`, `fact_entities`, `memory_banks`.
  `[факт: plugins/memory/holographic/README.md; retrieval.py:161-170, 390-398]`
- **Ретрив = гибрид трёх сигналов** с весами FTS5 0.4 / Jaccard 0.3 / HRR 0.3,
  затем домножение на trust-score:
  `relevance = 0.4·fts + 0.3·jaccard + 0.3·hrr; score = relevance · trust`
  `[факт: retrieval.py:29-32, 91-97]`
- **HRR (Holographic Reduced Representations)** — vector-symbolic архитектура на
  фазовых векторах: каждый концепт — вектор углов в [0,2π), генерируется
  детерминированно из SHA-256 (одинаково на всех машинах), размерность `hrr_dim`
  по умолчанию 1024. Операции: `bind` (циркулярная свёртка = сложение фаз),
  `unbind` (корреляция), `bundle` (суперпозиция).
  `[факт: holographic.py:1-20, 43-98]`
- **Уникальные операции поверх HRR-алгебры** (ни у одного embedding-провайдера
  их нет): `probe` — все факты про сущность через unbind; `reason` —
  многосущностный AND-запрос через min-семантику; `contradict` — автоматическое
  обнаружение противоречий (высокое пересечение сущностей + низкая похожесть
  контента). `[факт: retrieval.py:114-190, 260-336, 338-442]`
- **Trust-scoring с асимметричным фидбеком:** helpful +0.05 / unhelpful −0.10,
  тренируется инструментом `fact_feedback`. `[факт: memory-providers.md:475]`
- **Извлечение фактов (auto_extract, default false) — чисто регексом**, без LLM:
  паттерны «I prefer/like/use…», «we decided/agreed…» на user-сообщениях.
  Дёшево, но грубо — берёт сырую строку, не синтезирует.
  `[факт: __init__.py:370-408]`
- **numpy опционален:** без numpy HRR отключается, веса перераспределяются в
  FTS 0.6 / Jaccard 0.4 — деградация до чистого keyword-поиска, но работает.
  `[факт: retrieval.py:38-42]`
- **Ёмкость (важное ограничение):** HRR — распределённое хранилище с шумом.
  SNR = √(dim / n_items); при n > dim/4 (≈256 фактов при dim=1024) SNR падает
  ниже 2.0 и точность HRR-ретрива деградирует (в лог пишется warning). FTS5 при
  этом продолжает работать без деградации — то есть система не «ломается», но
  HRR-компонента слабеет. `[факт: holographic.py:179-203]` `[домысел: реальный
  предел зависит от dim; можно поднять hrr_dim, ценой памяти 8 КБ/факт]`
- **Инструменты:** `fact_store` (9 действий: add/search/probe/related/reason/
  contradict/update/remove/list), `fact_feedback`. `[факт: README]`

## 2. Hindsight — механика (ground truth: исходники + первоисточники вендора)

Долговременная память со знаниевым графом, разрешением сущностей и
мульти-стратегийным ретривом. Внешний open-source продукт Vectorize,
подключённый в Hermes как плагин.

- **Что это:** `github.com/vectorize-io/hindsight`, лицензия **MIT**, Python,
  **~18.5k звёзд**, последний релиз **v0.8.4 (2026-07-01)** на момент снятия.
  `[факт: github.com/vectorize-io/hindsight — снято 2026-07-18]`
- **Модель памяти — «биомиметическая», три типа:** World (факты о мире),
  Experiences (собственный опыт агента), Mental Models / Observations
  (консолидированные убеждения, синтезированные рефлексией поверх сырых фактов).
  `[факт: github README + vectorize.io/hindsight]`
- **Ретрив TEMPR — четыре параллельные стратегии:** Semantic (векторный) +
  Keyword (BM25) + Graph (сущностные/временны́е/причинные связи) + Temporal
  (фильтр по времени), слияние через reciprocal rank fusion + cross-encoder
  reranking. `[факт: vectorize.io/hindsight, github README]`
- **Хранилище:** PostgreSQL (embedded или внешний); Oracle AI Database для
  enterprise. `[факт: github README]`
- **Три операции:** Retain (сохранить, с авто-извлечением сущностей), Recall
  (мульти-стратегийный поиск), **Reflect (LLM-синтез межпамятных наблюдений)** —
  именно reflect отличает Hindsight: он строит «убеждения», а не только хранит
  факты. `[факт: plugins/memory/hindsight/README.md; vectorize.io/hindsight]`
- **Требует LLM для памяти.** Retain (извлечение) и reflect (синтез) идут через
  LLM. В Cloud — на стороне вендора; в local_embedded — через ваш LLM-ключ
  (OpenAI/Anthropic/Gemini/Groq/Ollama/LM Studio/openai_compatible). Эмбеддинги
  и reranking в local-режиме считаются локально, без доп. ключей.
  `[факт: plugins/memory/hindsight/README.md]`
- **Три режима подключения:** `cloud` (API-ключ Vectorize), `local_embedded`
  (Hermes сам поднимает демон с встроенным Postgres, гасит после 5 мин
  простоя), `local_external` (указываешь на свой запущенный инстанс).
  `[факт: plugins/memory/hindsight/README.md; __init__.py:53-58]`
- **Бенчмарк LongMemEval (заявление вендора):** vectorize.io/hindsight приводит
  **94.6%** против Supermemory 85.2% / Zep 71.2% / GPT-4o 60.2%; заголовок
  VentureBeat называет **91%** (вероятно другая конфигурация/подмножество).
  Вендор пишет о «независимой репликации» Virginia Tech Sanghani Center и
  Washington Post. `[факт: vectorize.io/hindsight + venturebeat — снято
  2026-07-18]` `[домысел: 94.6 vs 91 — разные конфиги recall_budget; цифры
  вендор-репортед, независимого прогона именно этих чисел я не проверял]`
- **Инструменты:** `hindsight_retain`, `hindsight_recall`, `hindsight_reflect`.
  `[факт: README]`
- **Замечание по интеграции:** recall по умолчанию сужен до типа `observation`
  (консолидированные убеждения), а не сырых world/experience — плотнее по
  токенам на инъекцию в контекст. Расширяется через `recall_types`.
  `[факт: plugins/memory/hindsight/README.md — раздел Behavior change]`

## 3. Сравнительная таблица по общим критериям

| Критерий | Holographic | Hindsight |
|---|---|---|
| **Механика хранения** | SQLite + FTS5 + HRR-векторы (8 КБ/факт) `[факт: код]` | Postgres, sparse+dense векторы + графовые/временны́е связи `[факт: github]` |
| **Механика ретрива** | FTS5 0.4 + Jaccard 0.3 + HRR 0.3, × trust `[факт: retrieval.py:91-97]` | TEMPR: vector+BM25+graph+temporal → RRF + rerank `[факт: vendor]` |
| **Качество ретрива** | хорошее на малом/среднем объёме; HRR деградирует >~256 фактов `[факт: holographic.py:179-203]` | SOTA на длинной мультисессионной истории (вендор-бенчмарк 91-94.6% LongMemEval) `[факт: vendor; независимость не проверял]` |
| **Синтез/убеждения** | нет; только сырые факты + contradict-детект `[факт: код]` | да — reflect строит consolidated observations `[факт: vendor]` |
| **Уникальное** | probe/reason/contradict — HRR-алгебра, contradiction-детект `[факт: retrieval.py]` | reflect-синтез, графовый ретрив, temporal `[факт: vendor]` |
| **Нужен LLM** | нет (извлечение — регекс) `[факт: __init__.py:370-408]` | да (retain + reflect) `[факт: README]` |
| **Сеть** | не нужна `[факт: код — нет http/requests]` | нужна (Cloud) или локальный демон `[факт: README]` |
| **Стоимость** | бесплатно `[факт]` | Cloud pay-per-token: retain $10/M вх. токенов, recall $0.75/M, reflect $3/M, storage $0.25/M/мес (первые 30 дней бесплатно), стартовые кредиты `[факт: vectorize.io/pricing — снято 2026-07-18]`; self-host — бесплатно + ресурсы |
| **Локальность** | 100% локально, офлайн `[факт]` | частично: local_embedded/local_external возможны, но нужен LLM `[факт: README]` |
| **Vendor-lock** | нет (внутренний плагин) | низкий на OSS (MIT, self-host), средний на Cloud (API+биллинг) `[факт: LICENSE + pricing]` |
| **Зрелость** | внутренний код Hermes, зависит от numpy | внешний продукт, ~18.5k★, v0.8.4, MIT, заявлено prod-использование Fortune-500 `[факт: github; «Fortune-500» — маркетинг вендора, домысел по верифицируемости]` |
| **Интеграция с Hermes** | нативный плагин, `fact_store`/`fact_feedback`, profile-scoped по `$HERMES_HOME` `[факт]` | нативный плагин, авто-upgrade клиента, per-profile config.json, bank_id-темплейты `[факт: README]` |
| **Приватность** | данные не покидают машину `[факт]` | Cloud = данные у вендора; local = у себя `[факт]` |

## 4. Локальная модель: что можно выносить локально и какой ценой

Вопрос задачи: можно ли подключить локальную модель и вынести часть работы
(эмбеддинги/индексация/ретрив) локально.

**Holographic — уже полностью локальный, LLM вообще не нужен.**
- Ретрив (FTS5 + Jaccard + HRR) — чистый CPU/numpy, ноль сети, ноль LLM.
  `[факт: retrieval.py; holographic.py]`
- «Эмбеддинги» тут — не нейросетевые: HRR-векторы генерятся из SHA-256
  детерминированно, это не модельные эмбеддинги. Качество — не семантическое в
  смысле трансформеров, а композиционно-алгебраическое + лексическое (FTS5).
  `[факт: holographic.py:43-67]` `[домысел: на парафразах без общих слов HRR/FTS
  проиграют настоящим семантическим эмбеддингам]`
- Извлечение фактов — регекс, не модель. Хочешь лучше — единственный LLM в
  контуре это основная модель агента (когда она сама вызывает `fact_store add`).
  `[факт: __init__.py:370-408]`
- Ресурсы: ~ноль сверх SQLite; 8 КБ/факт на HRR-вектор. `[факт: holographic.py:164]`

**Hindsight — локальный вынос возможен, и довольно полный:**
- Режим `local_embedded` поднимает локальный Postgres-демон; **эмбеддинги и
  reranking считаются локально** без внешних ключей. `[факт: README]`
- LLM для retain/reflect можно указать локальный: `openai_compatible` +
  `llm_base_url` (llama.cpp / vLLM / LM Studio) либо `ollama`/`lmstudio`.
  То есть реально собрать **полностью локальный стек**: локальный LLM +
  локальный Postgres + локальные эмбеддинги, без облака и без vendor-биллинга.
  `[факт: plugins/memory/hindsight/README.md — Local Embedded LLM]`
- **Цена по качеству:** извлечение сущностей и синтез наблюдений напрямую зависят
  от локальной модели. Дефолты вендора для локальных провайдеров — мелкие модели
  (`gemma3:12b` для ollama, `qwen/qwen3.5-9b` для openrouter). `[факт:
  __init__.py:66-76]` `[домысел: на моделях ~7-12B качество reflect/extract
  заметно ниже, чем у фронтир-моделей; это главный риск локального Hindsight —
  reflect и есть его киллер-фича, и она деградирует сильнее всего на слабой LLM]`
- **Цена по ресурсам:** Postgres-демон + инференс локальной LLM (GPU/RAM) +
  cross-encoder reranker. На порядок тяжелее Holographic, который ест почти
  ничего. `[домысел, оценка по составу стека]`

**Вывод по локальности:** если критична локальность/приватность/офлайн и хочется
«ноль инфраструктуры» — Holographic уже оптимален. Если нужен семантический
ретрив и reflect, но без облака — Hindsight `local_embedded` с локальной моделью
это даёт, ценой Postgres + GPU-инференса и просадки качества синтеза на мелкой
локальной LLM.

## 5. Рекомендация оператору

**Оставить Holographic как активный провайдер по умолчанию.** Он уже выбран, он
бесплатный, локальный, офлайновый, без vendor-lock и без LLM-зависимости — это
прямое попадание в local-first профиль Hermes. `[факт: код + README]`

**Менять на Hindsight — при выполнении всех условий:**
1. **Есть измеренная боль ретрива.** Оператор наблюдает, что Holographic не
   находит релевантные факты на реальном объёме (сотни+ фактов, где HRR по SNR
   деградирует, а лексического FTS5 не хватает на парафразах). Не гипотетически.
2. **Нужен reflect/наблюдения.** Требуется межсессионный синтез «убеждений», а не
   только хранение фактов — единственная функция, которой у Holographic нет
   вообще.
3. **Оператор принимает инфраструктурную цену:** Postgres-демон + LLM для памяти
   (локальный или облачный).

**Как менять, если решено:** брать `local_embedded` с локальной
OpenAI-совместимой моделью — так сохраняется локальность/приватность и нет
Cloud-биллинга и vendor-lock. Cloud-режим — только если готовы платить
pay-per-token ($10/M входных на retain — доминирующая статья) и отдавать данные
вендору ради максимального качества и нулевой инфраструктуры.

**Чего НЕ делать:** переходить «на всякий случай» ради красивых бенчмарк-цифр.
Бенчмарк LongMemEval вендор-репортед (91-94.6%), независимого прогона именно этих
чисел я не проверял; а Holographic на LongMemEval вообще не мерян — сравнение
«94.6% vs Holographic» некорректно, это разные постановки. Пороговое решение —
реальная боль оператора, а не лидерборд. `[домысел: обоснование решения]`

---

## Источники

- **Holographic (ground truth):** `plugins/memory/holographic/holographic.py`,
  `retrieval.py`, `__init__.py`, `README.md` (исходники Hermes).
- **Hindsight-интеграция (ground truth):** `plugins/memory/hindsight/__init__.py`,
  `plugin.yaml`, `README.md`; `website/docs/user-guide/features/memory-providers.md`.
- **Hindsight-продукт:** [github.com/vectorize-io/hindsight](https://github.com/vectorize-io/hindsight)
  (MIT, ~18.5k★, v0.8.4 2026-07-01); [vectorize.io/hindsight](https://vectorize.io/hindsight)
  (TEMPR, LongMemEval 94.6%); [vectorize.io/pricing](https://vectorize.io/pricing)
  (pay-per-token); [VentureBeat — Hindsight 91% LongMemEval](https://venturebeat.com/data/with-91-accuracy-open-source-hindsight-agentic-memory-provides-20-20-vision).
  Всё снято 2026-07-18.
