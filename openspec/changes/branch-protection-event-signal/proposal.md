# branch-protection-event-signal: ослабление защиты main кричит само, без administration

Задача: #370 (родитель #341). Дельта-спека: `specs/journal-tasks-hands/spec.md`
— новый подраздел «Наблюдение за защитой main (#370)» в разделе «Конвейер
(оркестратор)».

## Класс закрываемой ошибки

Silent-wrong недоступной защиты. 2026-09-06 обнаружилось, что
`enforce_admins` был выключен: админский токен сливал PR мимо всех
обязательных проверок (`test`, `contract`) — этим и воспользовался
инцидент, доказанный мутацией (после включения `enforce_admins` GitHub
отвечает `405` на попытку слить мимо проверок). Настройку включили, но
ничто не сторожило её откат.

Инвариант 6 (`scripts/orchestra/repo_invariants.py::check_branch_protection_drift`,
PR #249, не смёржен) написан на ОПРОС: `GET /repos/{o}/{r}/branches/main/
protection`. Этот путь тупиковый: эндпойнт требует у токена право
`administration`, которого структурно нет в перечне permissions,
доступных `GITHUB_TOKEN` в GitHub Actions (подтверждено в
`docs/research/21-github-actions.md`; # 370 зафиксировал развилку —
новый админский секрет или ручной запуск, — которую владелец не принял:
проект сокращает число секретов, а не плодит).

## Решение

Не опрашивать состояние — реагировать на штатное событие GitHub Actions
`branch_protection_rule` (`created`/`edited`/`deleted`). Оно срабатывает
САМИМ фактом изменения защиты и несёт изменённое состояние прямо в payload
(`rule`, для `edited` — ещё и `changes`) — читать настройки через REST
не нужно вовсе, значит не нужно и право `administration`. Проверено
живьём (не по документации по памяти): существование события и форма
`rule`/`changes` подтверждены каноническими примерами payload'ов GitHub
(`raw.githubusercontent.com/octokit/webhooks/main/payload-examples/
api.github.com/branch_protection_rule/{created,edited,deleted}.payload.json`,
2026-09-06).

1. Новый workflow `.github/workflows/branch-protection-watch.yml` —
   триггер `branch_protection_rule`, вся логика — один шаг, вызывающий
   `scripts/orchestra/branch_protection_watch.py`.
2. `branch_protection_watch.py` (чистые решения + тонкая проводка, по
   образцу `pulse_guard.py`/`upstream_drift.py`):
   - `deleted` — всегда критично (защита снята целиком).
   - `created`/`edited` — текущее состояние `rule` сравнивается с
     `EXPECTED_*` (`admin_enforced`, `required_status_checks`,
     `strict_required_status_checks_policy`, `allow_force_pushes_
     enforcement_level`, `allow_deletions_enforcement_level`); отсутствие
     поля в payload не проверяется (не гадаем на незнакомой форме), лишний
     ОБЯЗАТЕЛЬНЫЙ контекст сверх ожидаемых — не нарушение (надмножество
     `EXPECTED_STATUS_CHECK_CONTEXTS` — усиление, не ослабление).
   - Правило не для `main` (`rule.name`) — пропускается без сети.
3. Сигнал — общий канал предохранителя конвейера: `pulse_guard.escalate`
   (issue `WATCHDOG_ISSUE` #120 + Telegram), второй канал для того же
   класса «поломка инфраструктуры» не заводится.
4. Текст сигнала называет ПОСЛЕДСТВИЕ, а не факт (что стало возможно), и
   несёт готовую команду восстановления — конкретный `gh api` под
   конкретное нарушение (`enforce_admins` → `POST .../protection/
   enforce_admins`; контексты/`strict` → `PATCH .../protection/
   required_status_checks`; force-pushes/deletions → `DELETE .../
   protection/allow_force_pushes|allow_deletions`; `deleted` → полный
   `PUT .../protection` с восстановлением всего состояния).

## Единственное место правды на ожидаемое состояние

`EXPECTED_*` в `branch_protection_watch.py` — временная копия
одноимённых констант `repo_invariants.py::EXPECTED_*` (PR #249, не в
main на момент этого change'а): по духу второй копии не заводим, но и не
блокируемся на чужом открытом PR под `ai:changes-requested`. Когда #249
сольётся — заменить на `from repo_invariants import EXPECTED_*` (имена
выбраны совпадающими нарочно, правка в одну строку). Значения обоих
списков констант — то же состояние, что уже описано прозой в AGENTS.md
(«Защита main: `enforce_admins=true`, обязательные `test` и `contract`,
strict»).

## Что вне рамок

- Сам инвариант 6 (полный аудит текущего состояния через `GET .../
  protection`) остаётся нерешённым куском #370: этот change закрывает
  ТОЛЬКО «увидеть момент отката» (событие), не «периодически перепроверить
  состояние без повода» (плановый аудит расхождения, если протухла запись
  где-то мимо событий Actions — например правило поменяли до того, как в
  репозитории появился этот workflow, или GitHub временно не доставил
  вебхук). Один из вариантов критерия готовности #370 (секрет с
  `administration` или ручной запуск) остаётся открытым для ЭТОГО
  остаточного назначения, если владелец сочтёт его нужным.
- Инварианты 1–5 (`repo_invariants.py`, PR #249) не трогаются — другой
  раздел, другая задача.
- Аналогичные события для смежных настроек репозитория (права Actions,
  удаление веток, ротация секретов) — не в объёме, названы владельцу
  отдельно, не реализуются здесь.
