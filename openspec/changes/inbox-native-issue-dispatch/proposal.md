# inbox-native-issue-dispatch: инбокс создаёт issues без нового секрета (#20)

Задача: #20. Заменяет решение ADR 0011. ADR: [0013](../../../docs/decisions/0013-inbox-dispatch-no-new-secret.md).
Дельта-спека: [specs/journal-tasks-hands/spec.md](specs/journal-tasks-hands/spec.md).

## Зачем

ADR 0011 решил создавать issue для директивы инбокса под третьим узким
секретом `GH_ISSUES_TOKEN`, вызывая GitHub Issues API прямо из DO. Секрет
так и не был заведён владельцем (проверено 2026-09-06: `gh secret list` пуст
по нему, issues с меткой `source:inbox` — ноль) — каждая директива честно
ждала с `issues_not_configured`. Владелец решил: секрет заводить не нужно —
нужную возможность (issue создаёт сущность с правом на Issues) в репозитории
уже даёт `orchestra.yml` (`permissions: issues: write`, штатный
`github.token`), без единого нового секрета.

Альтернатива, которую ADR 0011 отверг («директива → `POST /api/tasks` →
issue создаёт job»), была отвергнута по доводам, относящимся конкретно к
очереди `/api/tasks`/`hands.yml` (немедленный запуск DSH headless; `hands.yml`
намеренно лишён прав на Issues, ADR 0006) — не к классу «репозиторный
dispatch в свой лёгкий job». Эта дельта реализует именно такой маршрут.

## Что делается

- `#createIssue` (DO) переименован в `#dispatchIssueCreation`: вместо
  прямого вызова GitHub Issues API шлёт `repository_dispatch`
  (`event_type: inbox-issue`) под уже существующим `GH_DISPATCH_TOKEN`, с
  `client_payload: {message_id, claimed_ts, title, body}` (title/body уже
  замаскированы и усечены ДО отправки).
- Новый job `.github/workflows/inbox-issue.yml`: создаёт issue штатным
  `github.token` (`permissions: issues: write`, единственное разрешение,
  никакого чекаута), метки `task`+`source:inbox`, затем callback в DO.
- **204 от `dispatches` — не доказательство созданной issue** (тот же класс,
  что документирован для пульса оркестратора). Замыкание петли: новый
  маршрут `POST /api/messages/issue-created` (Bearer `HANDS_TOKEN`, тот же
  канал, что heartbeat) — job подтверждает исход явно: `{issue_number,
  issue_url}` при успехе, `{error}` при отказе. До подтверждения сообщение
  остаётся `processing`; job, который не подтвердил (упал, не запустился),
  ловится тем же ватчдогом (`messageStuckProcessingMs`), что и любая другая
  зависшая проходка инбокса.
- `claimed_ts` (момент атомарного захвата) эхо-возвращается job'ом в
  callback'е; подтверждение — CAS по этому значению (тот же инвариант, что у
  `#finishMessage`): запоздавшее подтверждение проходки, которую ватчдог уже
  увёл дальше, не перезаписывает чужой результат.
- Секрет `GH_ISSUES_TOKEN` убран отовсюду: `harness.ts`, типы окружения,
  `deploy-worker.yml`, `.dev.vars.example`, `wrangler.jsonc`. Ошибка
  `issues_not_configured` заменена на `dispatch_not_configured` (тот же
  секрет и код ошибки, что у остальных dispatch'ей морды).
- Гвардия `scripts/lib/test_dispatch_token_usage.py::EXPECTED_WORKFLOWS`
  расширена на `inbox-issue.yml` (не читает `GH_DISPATCH_TOKEN` — секрет
  остаётся только в воркере).
- ADR 0011 помечен ЗАМЕНЁННЫМ, новый ADR 0013 — с явным разбором, почему
  прежние доводы против «job создаёт issue» не относятся к этому маршруту.

## Что вне рамок

- PR #362 (задача #304, автодовод `failed`-директив после появления
  `GH_ISSUES_TOKEN`) потерял смысл вместе с секретом — закрывается без
  слияния, отдельное действие вне кода этой дельты.
- Триаж-UI морды, автоответ в чате — по-прежнему фаза 2 задачи #20, не
  трогается.
- Прямая доставка вебхуком Telegram — отдельное решение, не трогается.
