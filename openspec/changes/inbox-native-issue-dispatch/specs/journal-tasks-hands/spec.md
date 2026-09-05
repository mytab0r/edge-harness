# Дельта-спека: журнал (journal-tasks-hands) — инбокс без нового секрета (#20)

## MODIFIED: Инбокс владельца (#20)

~~34. Директива и правка доков (`doc_edit`) → issue под узким `GH_ISSUES_TOKEN`
    (только Issues:RW, ADR 0011) с метками `task`+`source:inbox` — у каждой
    директивы (включая императивы о доках) есть issue-след в пуле, kind
    сообщения уезжает в тело issue. Не заданный токен, сеть, 5xx/429 —
    повторяемая ошибка: сообщение возвращается в `new` до
    `LIMITS.messageMaxAttempts` попыток, затем честный `failed` с ошибкой.
    Прочие 4xx — `failed` сразу. Газ после устранения причины —
    `POST /api/messages/process {"retry_failed": true}` (обнуляет попытки).
    Вызов GitHub ограничен `LIMITS.messageIssueFetchTimeoutMs` — заведомо
    меньше порога ватчдога. Публичный текст issue маскируется тем же классом
    паттернов секретов, что и остальные наружные тексты, ДО усечения
    заголовка; факт маскирования — в `result.secrets_redacted`.~~

34. Директива и правка доков (`doc_edit`) → `repository_dispatch`
    (`event_type: inbox-issue`) под `GH_DISPATCH_TOKEN` — тем же секретом,
    что несёт остальные dispatch'и морды (ADR 0008), без отдельного секрета
    (ADR 0013, заменяет ADR 0011). Job `.github/workflows/inbox-issue.yml`
    создаёт issue штатным `github.token` (`permissions: issues: write`) с
    метками `task`+`source:inbox` — у каждой директивы (включая императивы о
    доках) есть issue-след в пуле, kind сообщения уезжает в тело issue.

    HTTP 204 от `dispatches` доказывает только приём события, не созданную
    issue (`docs/research/21-github-actions.md`) — сообщение остаётся
    `processing` до явного подтверждения job'а: `POST
    /api/messages/issue-created` (Bearer `HANDS_TOKEN` — тот же канал, что
    heartbeat) с `{message_id, claimed_ts, issue_number, issue_url}` (issue
    создана) либо `{message_id, claimed_ts, error}` (job сам сообщает об
    отказе). `claimed_ts` — эхо значения из `client_payload` dispatch'а;
    подтверждение — CAS по этому значению (тот же инвариант, что у
    `#finishMessage`): запоздавшее подтверждение проходки, которую ватчдог
    уже увёл дальше (reclaim → повторный dispatch с новым `claimed_ts`), не
    совпадает и не перезаписывает чужой результат — issue не задваивается
    молча.

    Не заданный `GH_DISPATCH_TOKEN`/`GH_REPO`, сетевая ошибка dispatch'а,
    5xx/429 от GitHub на сам dispatch, либо явный `error` от job'а —
    повторяемая ошибка: сообщение возвращается в `new` до
    `LIMITS.messageMaxAttempts` попыток, затем честный `failed` с ошибкой.
    Прочие 4xx на dispatch — `failed` сразу (штурм бесполезного повторения
    не нужен). Job, который не запустился вовсе или упал ДО подтверждения —
    тот же необнаружимый по 204 класс — ловится тем же ватчдогом (п. 36), что
    и любая другая зависшая `processing`-проходка инбокса: не новый
    механизм. Газ после устранения причины —
    `POST /api/messages/process {"retry_failed": true}` (обнуляет попытки).

    Dispatch ограничен `LIMITS.messageIssueDispatchTimeoutMs` — заведомо
    меньше порога ватчдога (гвардится тестом). Публичный текст issue
    (title/body в `client_payload`, видимом в API/логах Actions) маскируется
    тем же классом паттернов секретов, что и остальные наружные тексты
    (`dsh-ci.sh::redact`), причём ДО усечения заголовка и ДО отправки job'у
    (у job'а нет доступа к `redact()`); в `client_payload` уходит усечённый
    заголовок (≤80 символов — ниже 256-потолка issues API, с которым иначе
    столкнётся job). Факт маскирования (`result.secrets_redacted`)
    пересчитывается в момент подтверждения из уже сохранённого текста
    сообщения — job его не видит и не решает.

Сценарий: директива с настроенным `GH_DISPATCH_TOKEN` → dispatch принят
(204) → сообщение остаётся `processing` (НЕ `done`) → job подтверждает
`issue-created` с `issue_number`/`issue_url` → сообщение `done`.

Сценарий: job отвечает `issue-created` с устаревшим `claimed_ts` (ватчдог уже
увёл сообщение дальше, новый `claimed_ts`) → `accepted: false`, статус и
результат сообщения не меняются — чужая (новая) проходка не потеряна.

Сценарий: job сам сообщает `error` через `issue-created` — тот же кап
попыток, что у сетевой ошибки dispatch'а: две первых ошибки возвращают
сообщение в `new`, третья (кап) — честный `failed`.

Требование: секрета `GH_ISSUES_TOKEN` нет нигде в кодовой базе — типах
окружения, `deploy-worker.yml`, `.dev.vars.example`, `wrangler.jsonc`;
`scripts/lib/test_dispatch_token_usage.py` подтверждает, что
`inbox-issue.yml` не читает `secrets.GH_DISPATCH_TOKEN` (значение остаётся
секретом только воркера, дёргает его исключительно DO).
