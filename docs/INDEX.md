# Карта документации

Если ты агент и тебе поставили задачу в этом репозитории — начни отсюда, а не с поиска по коду.
Ниже перечислено всё, что уже исследовано. Переисследовать это не нужно; если факт
устарел — исправь файл, а не заводи параллельную версию.

## Как устроена документация

| Каталог | Что там | Когда писать |
|---|---|---|
| `docs/research/` | Установленные факты о внешних системах: DSH, Cloudflare, GitHub | Когда выяснил что-то о чужой системе |
| `docs/decisions/` | ADR — принятые решения с причиной и альтернативами | Когда сделан выбор между вариантами |
| `docs/runbooks/` | Операционные процедуры владельца: пошагово, с проверкой видимого результата и откатом | Когда действие делается руками и повторяется |
| `openspec/specs/` | Источник правды: как система обязана себя вести | Когда меняется поведение |
| `openspec/changes/` | Активные изменения: proposal, design, tasks, дельта-спеки — состав и критерий завершения см. [протокол openspec](agents/OPENSPEC-PROTOCOL.md) | Когда начинаешь нетривиальную работу |

Ссылки между документами — обычные относительные markdown-ссылки. Они кликаются и в GitHub,
и в Obsidian, и в любом редакторе. Не используй `[[wiki-links]]`: в GitHub они не работают.

## Исследования

- [00. Контекст и цель](research/00-context.md) — какую задачу решаем, откуда она взялась,
  что было до неё.
- [10. Архитектура DSH](research/10-dsh-architecture.md) — монорепо, концепция швов,
  agent loop, события, профили, что требует Node. **Главный документ про ядро.**
- [11. dsh-edge](research/11-dsh-edge.md) — как DSH уже портировали на Cloudflare Workers:
  что заменили, что не работает, что забрать себе.
- [12. dsh-edge session API](research/12-dsh-edge-session-api.md) — auth кукой владельца,
  RPC-конверт и список методов, форма канонических событий, шов импорта транскрипта
  раннера (патч 0004, #119), архитектура памяти (раннер vs облако, причина 403 задачи
  #133 установлена и закрыта — отсутствовавший `User-Agent`, не блок egress-IP).
- [20. Cloudflare Free](research/20-cloudflare-free.md) — что реально доступно бесплатно.
  **Читать до любого решения об архитектуре на CF**: там лимит 10 ms CPU, который меняет всё.
- [21. GitHub Actions](research/21-github-actions.md) — лимиты, бесплатность на публичных
  репозиториях и границы Terms of Service.
- [22. Тестовый стек CF](research/22-cf-testing-toolchain.md) — vitest-plugin: изоляция
  хранилища, WebSocket в тестовой петле, rowsWritten, плейсхолдеры DO SQLite.
- [23. Нативное vs своё](research/23-platform-native-vs-custom.md) — где мы написали
  своё, а платформа (GitHub/Cloudflare) даёт готовое: что взять, что не берём и почему
  (вердикты-как-статусы и auto-merge — канал статусов уже взят #345, остаток по
  required checks/auto-merge; Cron Trigger как страховка alarm, #693; git-ref-замок).
- [24. Git: ребейз и миграция схемы](research/24-git-rebasing-and-schema-migrations.md) — при переходе
  на новую схему хранения (каталог файлов вместо рукописной регистрации) ребейз старой
  ветки проходит без конфликта, но новая схема не применяется; требуется транслятор.
- [25. git grep -E: диалект регулярок](research/25-git-grep-regex-dialect.md) — паттерн
  «Класс закрыт» проверяется POSIX ERE, а пишут его на PCRE: `(?:…)` падает текстом, не
  называющим причину, а `\d` и ленивые квантификаторы НЕ падают и молча считают не то.
  Замер с заменами; `\w`/`\s`/`\b` при этом работают и трогать их не надо.
- [27. Вебхук Telegram: код ответа — это «повтори/не повторяй»](research/27-telegram-webhook-delivery.md) —
  Telegram доставляет апдейты одного чата по порядку и ретраит любой не-2xx, поэтому один
  апдейт, которому ретраи не помогут (фото без подписи, чужой чат), затыкает канал владельца
  целиком вместе с кнопками решений. Плюс: чем `pending_update_count` отличает стоящую
  очередь от протухшего `last_error_message`.
- [30. Отвергнутые варианты](research/30-rejected-alternatives.md) — **обязательно к прочтению
  перед любым предложением по архитектуре.** Отвергнутые варианты с причиной отказа и
  условием, при котором к ним стоит вернуться.
- [31. «Руки» персистентно или эфемерно](research/31-hands-layer-tradeoffs.md) — развилка
  по деньгам между текущим Actions, GitHub Codespaces, Cloudflare Containers и
  self-hosted runner'ом; какие живые механизмы (детектор зависания, предохранитель)
  каждый вариант убирает, а какие остаются в любом случае.
- [32. Claude OAuth access-токен как провайдер](research/32-claude-oauth-provider.md) —
  `llm-pi-ai` детектит `sk-ant-oat` в `apiKey` и сам переключает на Bearer + беты, без
  loopback-прокси; плагин `dsh-anthropic-oauth-pool` нужен только ради ротации/cooldown
  нескольких аккаунтов, не ради самого моста.
- [99. Известные неизвестные](research/99-open-questions.md) — вопросы, на которые ответа нет
  и не будет из документации. Не тратить время на поиск; выяснять замером.

## Решения

- [0001. Push-модель вместо туннеля](decisions/0001-push-model-not-tunnel.md)
- [0002. Агент-цикл в раннере, не в Durable Object](decisions/0002-loop-in-runner-not-do.md)
- [0003. Задержка старта job'а: вердикт по замерам](decisions/0003-dispatch-latency-verdict.md)
- [0004. API-контракт](decisions/0004-api-contract.md)
- [0005. Замер хвоста задержки: кампания, а не сеанс](decisions/0005-dispatch-tail-campaign.md)
- [0006. Аренда задачи: git-ref замок](decisions/0006-task-lease-git-ref-lock.md)
- [0007. AI-ревью как второй гейт: своя метка ai:*, двойное условие слияния](decisions/0007-ai-review-gate.md)
- [0008. Узкий GH_DISPATCH_TOKEN: dispatch-токен морды отделён от PAT конвейера](decisions/0008-narrow-dispatch-token.md)
- [0009. Заказ установки плагина: RPC морды (session.prompt), не POST /api/tasks](decisions/0009-plugin-order-via-morde-rpc.md)
- [0010. Интеграции внешних систем: REST-инструменты своим edge-плагином, не MCP-серверы](decisions/0010-integrations-edge-plugin-rest-tools.md)
- [0011. Инбокс создаёт issues под собственным узким GH_ISSUES_TOKEN](decisions/0011-inbox-issues-token.md) — ЗАМЕНЕНО ADR 0015
- [0012. Событийный триггер оркестратора и цикл слияний за один прогон](decisions/0012-orchestra-event-trigger-merge-loop.md)
- [0013. Нативная GitHub Merge Queue недоступна на этом репозитории — своя очередь остаётся](decisions/0013-native-merge-queue-not-available.md)
- [0014. Инлайн-кнопки решения владельца в Telegram: узкий webhook-секрет, не второй канал записи](decisions/0014-telegram-inline-buttons.md)
- [0015. Инбокс создаёт issues репозиторным dispatch'ем в свой job — без нового секрета](decisions/0015-inbox-dispatch-no-new-secret.md)
- [0016. Конкуренция за код не устраняется приёмом регистрации: границы параллельной работы](decisions/0016-parallel-work-on-shared-code-boundaries.md)
- [0017. Второй гейт морды dsh-edge: e2e-смоук на PR против локального воркера](decisions/0017-dsh-edge-pr-smoke-local-worker.md)
- [0018. Алерт «пульс не бьётся» живёт в DO (Cloudflare), не в GitHub Actions](decisions/0018-pulse-alert-lives-in-do.md)
- [0020. Номер ADR/research-документа не назначается вручную без арбитра](decisions/0020-decision-doc-numbering-guard.md)
- [0021. Триаж застрявшего в конфликте PR: шесть измеримых величин](decisions/0021-stalled-pr-merge-conflict-triage.md)
- [0022. Связывание контекста между каналами: три точечных приёма вместо общего носителя](decisions/0022-cross-channel-context-transfer.md)
- [0023. Рецепт мутации — исполняемый opt-in формат, не текстовый линт](decisions/0023-mutation-recipe-execution-guard.md)
- [0024. Триаж застрявшего PR: метод ADR 0021 не покрывал очередь — четыре дефекта и правки](decisions/0024-stalled-pr-triage-method-fix.md)
- [0025. Классификация классов дефектов — в источнике, не постфактум-кластеризацией](decisions/0025-defect-class-source-classification.md)
- [0026. Заявления агента о своей работе — машинная проверка исполнения, не критик-прочтение](decisions/0026-verify-agent-claims-machine-check.md)
- [0027. Немигрируемая сессия изолируется, а не роняет морду](decisions/0027-quarantine-unmigratable-session.md)
- [0028. Соответствие «категория сигнала → тема Telegram» живёт на data-ветке](decisions/0028-telegram-topics-map-on-data-branch.md)

## Операционные процедуры

- [Переключение LLM-провайдера](runbooks/switch-llm-provider.md) — раннеры и штатный
  `deepseek-official` сидят на одном слоте деплоя, смена там = замена. В морде (Settings →
  Models) второй провайдер добавляется без передеплоя через плагин `provider-registry`
  (#378). Шаги, проверка видимого результата, откат, и почему потолок ответа зашит под GLM.
- [Обновить учётки пула Claude из krouter-экспорта](runbooks/refresh-anthropic-pool.md) —
  `dsh-anthropic-oauth-pool` (#838), секреты `ANTHROPIC_OAUTH_1/2`, импорт из
  krouter-бэкапа или одиночного credentials.json (`scripts/lib/anthropic_oauth_import.py`),
  почему истёкший accessToken из бэкапа — это нормально (пул рефрешит по refreshToken).

## Мультиагентная работа

- [Протокол совместной работы агентов](agents/PROTOCOL.md) — пул задач в Issues,
  ветки `agent/N-*`, оркестратор слияний, канал белых пятен. Enforced защитой ветки
  и workflow'ом `orchestra`, не договорённостью.
- [Протокол openspec](agents/OPENSPEC-PROTOCOL.md) — когда `openspec/changes/<id>/`
  обязателен, из каких файлов состоит, что считается завершением (два независимых
  условия), кто и чем архивирует, что происходит с дельта-спекой после (и что пока
  не происходит — механизма слияния в источник правды сейчас нет).
- [Playbook автономного воркера](agents/WORKER-PLAYBOOK.md) — правила работы без
  человека: эскалация, конвейер, механика DSH. Кормится в промпт автономного
  воркера (workflow `worker`, `scripts/worker/task.sh`).
- [Инфраструктура GitHub](agents/INFRA-GH.md) — инвентарь workflow, роли токенов,
  секреты/vars, защита ветки, лимиты, инструменты `scripts/gh/*`, известные грабли
  (GraphQL, heredoc) и границы того, что агент не делает сам.
- [Реестр меток](agents/LABELS.md) — тормоза конвейера и газы их снятия: это место правды,
  а не примеры.
- [Реестр использования LLM-провайдеров](agents/LLM-PROVIDER-USAGE.md) — кто (ai-review/
  worker/hands/морда) какой именованной цепочкой пользуется, генерируется из
  `config/provider-usage.json` (openspec/changes/llm-provider-usage-manifest, #823).
- [Roadmap потоков работ](agents/ROADMAP.md) — живая карта параллельных потоков и
  зависимостей (mermaid-гант); детали задач — в пуле Issues.
- [Инфраструктура Cloudflare](agents/INFRA-CF.md) — что доступно токену, чем
  узнать состояние (`scripts/cf/`), что нельзя трогать. Читать до любой работы
  с CF API/wrangler.

## Быстрые ответы на частые вопросы

**«Почему не Cloudflare Containers, там же git и python?»** — Workers Paid, $5/мес.
См. [отвергнутое, п.4](research/30-rejected-alternatives.md).

**«Почему не держать WebSocket из job'а в Durable Object?»** — запрещено GitHub Terms и
съедает 85% дневного бюджета CF Free. См. [ADR 0001](decisions/0001-push-model-not-tunnel.md).

**«Почему агент-цикл не в Durable Object?»** — 10 ms CPU на invocation на Free.
См. [ADR 0002](decisions/0002-loop-in-runner-not-do.md).

**«Кто маршрутизирует между провайдерами?»** — свои плагины DSH, и только они. Решение
владельца 2026-09-01: внешний питоновский роутер в этой цепочке не используется вовсе,
его поведение (failover по квотам, cooldown, приоритет моделей) воспроизводится своим
плагином — наш, переносим и адаптируем мы, не апстрим. Разделение труда: `llm-pi-ai` —
реестр провайдеров с кредами и UI (пул **выбора**, своего fallback у него нет; задача —
#378), а виртуальная модель `combo/auto` поверх подключённых провайдеров — наш плагин
маршрутизации; исходные артефакты владельца `dsh-combo-suite-0.1.0.tgz` (внутри
combo-router) и `dsh-anthropic-oauth-pool-0.1.0.tgz` — состав и местоположение
зафиксированы в #215, перенос и адаптация — #216.
См. [архитектуру DSH](research/10-dsh-architecture.md).

**«Сколько ждать старта рук?»** — неизвестно, GitHub этого не публикует. Эмпирика чужих
замеров: секунды при тёплом пуле, десятки при холодном, изредка часы в очереди.
См. [известные неизвестные](research/99-open-questions.md).

**«Почему своя очередь слияний, а не GitHub Merge Queue?»** — недоступна: она требует
владения репозиторием организацией, а не пользователем (задача #339, живая проверка
`gh api`). См. таблицу возможностей в [research/21](research/21-github-actions.md#возможности-платформы-которые-мы-не-используем-аудит-2026-09-05).
