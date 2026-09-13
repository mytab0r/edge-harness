Задача: #823.

# Tasks: llm-provider-usage-manifest

Разбивка на этапы. Этап 1 — репо-сторона, не зависит от UI морды, строится и
доказывается тестами полностью в этом PR. Этап 2 — морда-push, зависит от
хост-бриджа dsh-edge (см. находку ниже) — вынесен отдельной задачей.

## Находки при апробации дизайна (обнаружено на apply, не пересмотр design.md)

- **hands.yml уже подключён к `dsh_run_with_provider_chain`** (#805,
  `scripts/hands/dsh_task.sh:82,373`, `docs/runbooks/switch-llm-provider.md`
  раздел «Область охвата — все три канала»). Формулировка proposal.md
  Problem п.3 («hands.yml — до сих пор `dsh_run_with_retry`, БЕЗ failover») —
  устарела относительно кода на момент apply (2026-09-09): все три канала
  (`ai-review`, `worker`, `hands`) сейчас используют ОДИН и тот же механизм
  чтения `DSH_PROVIDER_CHAIN`. Из этого следует: чтение по id — уравнивает
  все три места ОДНИМ и тем же изменением (см. Этап 1.2), а не два места
  «с failover» + одно «без». Это не отменяет ценность манифеста (видимость и
  общее место назначения остаются нужны), но меняет формулировку столбца
  «исполняется ли failover» в генерируемой таблице (Этап 1.4): все три —
  «да», а не «нет — hands без failover».
- **Классы «EdgeSessionStore»/«installEdgePlugins» из design.md «Механизм
  пуска» не существуют в этом репозитории.** `#createIssue`/`#dispatchOwnerDecision`
  (`cf-worker/src/harness.ts:1997,2406`) — реальные методы класса `Harness`
  (не `EdgeSessionStore`) в ЭТОМ репозитории (edge-harness), обслуживающего
  ДРУГОЙ Cloudflare Worker/DO — журнал/задачи/руки («Мозг», AGENTS.md), а НЕ
  морду dsh-edge. Сама морда dsh-edge — код стороннего репозитория
  `pawaca/dsh-edge` (docs/research/11-dsh-edge.md), клонируемого только на
  этапе CI (`deploy-dsh-edge.yml`, `клон pawaca/dsh-edge на пине` — исходники
  НЕ вендорятся в это дерево, только `dsh-edge/patches/*.patch` его патчат).
  `EdgeSessionStore`/`session-store.ts`, упомянутые в design.md, — это,
  судя по всему, файлы САМОГО `pawaca/dsh-edge` (research/11: «session-store.ts
  (62 KB)» в перечне собственного кода апстрима), не что-либо в
  `cf-worker/src/harness.ts` этого репозитория. Значит план design.md
  «плагин зовёт инжектированный сервис, который реализован в хост-мосте DO,
  который уже читает `this.env.GH_DISPATCH_TOKEN` тем же способом, что
  `#createIssue`» смешивает ДВА РАЗНЫХ Worker'а: тот, что реально содержит
  `#createIssue` (Harness DO этого репозитория), не имеет отношения к плагинам
  морды dsh-edge вообще — методы одного класса недоступны плагину,
  запущенному в другом Cloudflare Worker без service binding, которого
  design.md не называет. Заведено как белое пятно —
  issue #824 (шаблон «🕳️ Белое пятно»), ссылка на этот change. Реализация
  Option A design.md в буквальном виде («тот же класс») невозможна; реальный
  путь — патч-серия в `dsh-edge/patches/` (тот же механизм, что уже патчит 7
  upstream-пакетов dsh-edge под Workers) на СВОЙ env-биндинг деплоя морды, не
  переиспользование Harness DO. Это отдельная, более крупная задача (клонировать
  апстрим, написать и провалидировать патч, обновить `dsh-edge/manifest.mjs`/
  `plugins.json`) — вне бюджета этого PR, см. Этап 2 ниже.

## Этап 1 — репо-сторона (этот PR, полностью тестируемо без CF)

- [x] 1.1 Схема манифеста: `config/provider-usage.json` (вне `scripts/`,
      `.github/workflows/`, `docs/agents/` — класс #153). Сиды из РЕАЛЬНОГО
      текущего `vars.DSH_PROVIDER_CHAIN` (прочитан `gh variable get`
      2026-09-09) — не выдуманные провайдеры, нулевое изменение поведения:
      все три потребителя изначально смотрят на одну и ту же цепочку
      `default-chain`, как и сейчас (одна репо-переменная на всех).
- [x] 1.2 `scripts/lib/dsh-ci.sh`: `dsh_load_provider_chain_from_manifest`
      + необязательный параметр `consumer_id` у `dsh_require_provider_chain`.
      Манифест приоритетнее `vars.DSH_PROVIDER_CHAIN`, если файл присутствует;
      файла нет вовсе — прежнее поведение (переходный период, design.md
      «Потребители»). Присутствует, но нет записи потребителя/цепочка не
      существует/пуста — fail loud (класс #727->#797), не тихий фоллбэк.
      `scripts/review/ai_dsh.sh`, `scripts/worker/task.sh`,
      `scripts/hands/dsh_task.sh` передают свой id.
- [x] 1.3 Инвариант `repo_invariants.py` (#11) —
      `check_provider_usage_manifest`: канонический список
      `ai-review`/`worker`/`hands` обязан иметь валидную запись в `usage`,
      ссылающуюся на непустую цепочку в `chains`. Гейтящий (CI_GATING) сразу
      — ноль нарушений на момент внедрения (манифест только что создан
      валидным). Газ объявлен в `GATING_RELEASE_CONDITION`.
- [x] 1.4 Генерируемая таблица `docs/agents/LLM-PROVIDER-USAGE.md` — по
      образцу `docs/agents/LABELS.md`: `scripts/lib/collect_provider_usage.py`
      печатает назначения (сверх трёх — видимость морды статической строкой,
      без записи в `usage`), `scripts/lib/test_provider_usage_registry.py`
      сверяет таблицу со сборщиком в CI, мутация в обе стороны.
- [x] 1.5 Тесты и мутации на 1.2/1.3/1.4 — доказательство (не просто
      прогон): снять фикс, убедиться, что гвардия/инвариант краснеют.

## Этап 2 — морда-push (НЕ в этом PR, отдельная задача)

Причина выноса — находка выше: буквальная реализация Option A design.md
требует патча `pawaca/dsh-edge` (`dsh-edge/patches/`), а не правки кода
этого репозитория в один заход, как предполагал design.md. Заводить
отдельной задачей после решения владельца по белому пятну #824 —
не строить вслепую поверх неверной посылки о «том же классе».

- [ ] 2.1 Решить с владельцем (issue #824): патчить `pawaca/dsh-edge` новым
      патчем (`dsh-edge/patches/`) с собственным сервисом
      `ctx.get('providerUsagePush')`, читающим СВОЙ env-биндинг деплоя
      dsh-edge (не `GH_DISPATCH_TOKEN` Harness DO этого репозитория — тот
      живёт в другом Worker'е) — или другой механизм.
  - [ ] 2.2 Плагин `provider-usage` (plugins-src/, по образцу
        provider-registry) — namespace настроек (chains+usage), `onChange`
        зовёт сервис 2.1; Settings UI по штатному installSettingsSection —
        рендеринг вложенного `chains`-словаря массивов не подтверждён
        (design.md «Не подтверждено» п.3) — проверить живым прогоном ПЕРЕД
        тем, как считать эту часть готовой.
  - [ ] 2.3 Contents API PUT со sha текущей версии (design.md «Не
        подтверждено» п.4) + проверка живым прогоном лимита частых записей
        (design.md «Не подтверждено» п.2).
