# Дельта-спека: журнал (journal-tasks-hands) — вердикты ревью вторым каналом, Commit Status API (#345)

## ADDED: Конвейер (оркестратор)

19. Оба вердикта ревью публикуются вторым каналом — GitHub Commit Status
    API (`POST /repos/{repo}/statuses/{sha}`), параллельно меткам п.15/17,
    не вместо них. Контексты: `harness/review` (гейт 1,
    `scripts/review/check_pr.py`) и `harness/ai-review` (гейт 2,
    `scripts/review/ai_review.py::cmd_verdict`). Второго источника истины
    нет: состояние статуса вычисляется из ТОЙ ЖЕ переменной вердикта, что
    уже определяет метку, в момент, когда метка ставится
    (`scripts/lib/review_labels.py::review_status_state`/`ai_status_state`).

    Состояния: гейт 1 — `success` только при `review:ok`, `failure` при
    `review:changes-requested`/`review:large` (тот же порог, что
    `merge_label_gate`). Гейт 2 — `approve` → `success`, `rework` →
    `failure`, `error` → `pending` (не `failure`: сбой провайдера/транспорта
    — не вердикт о коде, у него отдельный газ, автоповтор по таймеру п.
    ai-review #196; required status check не пересчитывается сам, `failure`
    держал бы его красным до нового пуша человеком).

    Цель — нативный `allow_auto_merge` (включён на репозитории): он
    проверяет required status checks, не метки, которые п.15/17 использует
    сейчас. Метки остаются единственным местом ПРИНЯТИЯ решения о слиянии
    (`merge_label_gate`, `scheduler.py::merge_queue`) до отдельной задачи —
    эта дельта только зеркалит решение вторым каналом, не переключает гейт
    слияния на статусы.

    Добавление `harness/review`/`harness/ai-review` в
    `required_status_checks.contexts` branch protection — действие
    владельца репозитория после подтверждения статусов живым прогоном, не
    автоматика этой дельты.
