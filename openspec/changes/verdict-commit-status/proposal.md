# verdict-commit-status: вердикты ревью — Commit Status API параллельно меткам

Задача: #345. Кандидат разобран в
[`docs/research/23-platform-native-vs-custom.md`](../../../docs/research/23-platform-native-vs-custom.md#2-метки-вердикты-reviewok-aiok--commit-status-api-не-берём-сейчас-но-это-реальный-кандидат)
(п.2, PR #344 — на момент постановки этой задачи ещё не слит в main,
ссылка красная, пока не сольётся; тот же принцип форвард-ссылки, что у
`docs/agents/INFRA-CF.md` в `docs/agents/INFRA-GH.md`, см. issue #341):
нативный `allow_auto_merge` (включён на репозитории)
проверяет required status checks, не метки. Оба гейта ревью
(`review:ok`/`review:changes-requested`/`review:large` и
`ai:ok`/`ai:changes-requested`/`ai:failed`) сейчас видны GitHub только как
метки — auto-merge их не видит, слияние целиком проверяет
`scheduler.py::merge_queue`, опрашивая PR раз в 15 минут. Это и есть корень
класса гонок #208/#252: решение о готовности PR существует только внутри
опроса, а не в состоянии, которое видит сам GitHub.

## Решение

1. `scripts/lib/review_labels.py` — одно место правды дополнено: константы
   контекстов (`STATUS_REVIEW = "harness/review"`,
   `STATUS_AI_REVIEW = "harness/ai-review"`), `post_commit_status` (обёртка
   `POST /repos/{repo}/statuses/{sha}` через тот же `run_gh_func`, что и у
   меток), `review_status_state`/`ai_status_state` (состояние статуса —
   строго та же переменная вердикта, что уже определяет метку — второй
   источник истины не заводится), `run_target_url` (ссылка на прогон
   Actions в `target_url`, `None` вне Actions).
2. `scripts/review/check_pr.py` — сразу после простановки метки-вердикта
   публикует `harness/review` тем же вердиктом (`review_status_state`).
3. `scripts/review/ai_review.py::cmd_verdict` — сразу после простановки
   `ai:*`-метки публикует `harness/ai-review` тем же вердиктом
   (`ai_status_state`): `approve` → `success`, `rework` → `failure`,
   `error` → `pending` (см. «Решение по `error`» ниже).
4. `.github/workflows/pr-review.yml` и `.github/workflows/ai-review.yml`
   (job `verdict`) — добавлено разрешение `statuses: write`.
5. Метки НЕ убираются этой задачей — статусы идут параллельно, переходный
   период. Удаление меток и перевод `scheduler.py`/`merge_label_gate` на
   чтение статусов вместо меток — отдельная задача после подтверждения,
   что статусы стабильно проставляются на живых PR.
6. Добавление `harness/review`/`harness/ai-review` в
   `required_status_checks.contexts` branch protection — делает владелец
   вручную (команда — в отчёте задачи #345), агент protection не трогает.

## Решение по `error` (сбой провайдера AI-ревью)

`ai:failed` может означать два разных факта: модель ответила не по
контракту (реальный вердикт о коде) или транспорт/провайдер не дозвонился
вовсе (`ai_review.transport_failed`). Метка одна и та же для обоих случаев,
но у сбоя транспорта уже есть отдельный газ — автоповтор по таймеру
(`scheduler.py::trigger_ai_review`, #196), не зависящий от того, что стоит
на PR. Commit status `failure` держал бы `harness/ai-review` красным
навсегда до следующего пуша человеком — required status check сам себя не
пересчитывает, в отличие от метки, которую снимает следующий прогон
конвейера. Выбрано `pending`: точно описывает факт «решение ещё не
вынесено», не открывает `allow_auto_merge` (только `success` открывает) и
не блокирует слияние навечно на инфраструктурном сбое, который не связан с
качеством кода.

## Спека

- `openspec/changes/verdict-commit-status/specs/journal-tasks-hands/spec.md` —
  добавляет пункт о втором канале вердикта параллельно п.15
  `openspec/specs/journal-tasks-hands.md`.

## Что вне рамок

- Удаление меток-вердиктов — отдельная задача.
- Перевод `scheduler.py::merge_label_gate`/`merge_queue` на чтение статусов
  вместо меток — отдельная задача (после подтверждения статусов живым
  прогоном).
- Изменение branch protection (добавление контекстов в
  `required_status_checks`) — решение и действие владельца, не этой задачи.
- Нативная Merge Queue — недоступна (ADR 0012), не пересматривается здесь.
