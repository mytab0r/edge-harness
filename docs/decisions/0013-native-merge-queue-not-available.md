# ADR 0013. Нативная GitHub Merge Queue недоступна на этом репозитории — своя очередь остаётся

- **Дата:** 2026-09-05
- **Статус:** принято (факт, не выбор вкуса)
- **Смежное:** [GitHub Actions — лимиты и границы](../research/21-github-actions.md),
  `scripts/orchestra/scheduler.py` (`merge_queue`, `update_remaining_pulls`,
  `update_branch_or_report`; `merge_loop` — событийный триггер из
  незамердженного [#330](https://github.com/mytab0r/edge-harness/pull/330),
  в текущем `main` его ещё нет), задача
  [#252](https://github.com/mytab0r/edge-harness/issues/252) (автоподтягивание
  веток жжёт AI-ревью), заявка на этот вопрос — задача
  [#339](https://github.com/mytab0r/edge-harness/issues/339).

## Контекст

Ровно месяц своей реализации очереди слияний (`merge_queue()` в
`scripts/orchestra/scheduler.py`; событийный триггер `merge_loop()` живёт
только в незамердженном PR #330, в текущем `main` его нет) — и постоянные
грабли: гонки update-branch с AI-ревью (#208), сожжённый
бюджет ревью при массовом автоподтягивании (#252), гонка `concurrency` с
обязательной проверкой `contract` (#189). Естественный вопрос: почему не
штатная GitHub Merge Queue, которая как раз это и решает нативно.

Собственник репозитория — аккаунт пользователя `mytab0r`, не организация;
репозиторий публичный, план — Free.

## Проверка (факт, не по памяти)

**1. Документация GitHub, дословно** (`docs.github.com/en/repositories/
configuring-branches-and-merges-in-your-repository/configuring-pull-request-
merges/managing-a-merge-queue`):

> "Pull request merge queues are available in any public repository owned by
> an organization, or in private repositories owned by organizations using
> GitHub Enterprise Cloud."

Ключевое слово — **owned by an organization**. Публичность репозитория
условие не отменяет: нужен именно владелец-организация.

**2. Живая проверка через REST API** — попытка создать ruleset с правилом
`merge_queue` на `mytab0r/edge-harness`:

```
$ gh api --method POST repos/mytab0r/edge-harness/rulesets --input -
{
  "name": "test-merge-queue-probe",
  "target": "branch",
  "enforcement": "disabled",
  "conditions": { "ref_name": { "include": ["refs/heads/main"], "exclude": [] } },
  "rules": [ { "type": "merge_queue", "parameters": { ... } } ]
}

→ HTTP 422
{"message":"Validation Failed","errors":["Invalid rule 'merge_queue': "],
 "documentation_url":"https://docs.github.com/rest/repos/rules#create-a-repository-ruleset"}
```

Ответ идентичен и при пустых `parameters`, и при заполненных — значит дело не
в форме параметров. Контрольные прогоны для интерпретации ошибки:

- `{"type":"deletion"}` (заведомо валидный тип, без бизнес-ограничений) →
  **201**, ruleset создан (тестовый ruleset `test-deletion-probe`, id
  `22347849`, `enforcement: disabled`, удалён сразу после проверки командой
  `gh api --method DELETE repos/mytab0r/edge-harness/rulesets/22347849`).
- `{"type":"totally_bogus_rule_xyz"}` (несуществующий тип) → **422**, но
  другое сообщение: `"Invalid property /rules/0: data matches no possible
  input"` — это ошибка схемы (тип не распознан вообще).

Разница сообщений показывает: `merge_queue` **распознан схемой** как валидный
тип правила (иначе была бы ошибка «no possible input», как у богус-типа), но
отклонён отдельной бизнес-проверкой с пустой деталью — ровно поведение
фичи-гейта по плану/типу владельца, а не по форме запроса. Это независимое
подтверждение цитаты из документации, а не просто повтор того же источника.

## Решение

Не переводить на нативную GitHub Merge Queue. Условие невыполнимо без смены
владельца репозитория с пользовательского аккаунта на организацию — а это
решение по объёму несопоставимо с задачей и не в её рамках. Своя очередь в
`scripts/orchestra/scheduler.py` остаётся единственным путём слияния.

## Вернуться, если

Репозиторий `mytab0r/edge-harness` будет передан во владение организации
**и** останется публичным (или организация будет на GitHub Enterprise Cloud
для приватного случая). Оба условия — организация-владелец и (публичность
или Enterprise Cloud) — должны выполниться одновременно; частичное выполнение
(например, просто создание organization без переноса репозитория) вопрос не
закрывает. Перенос владения — решение владельца по причинам, лежащим вне
инженерной задачи (биллинг, права участников, публичная видимость проекта);
инициировать его должен владелец, а не агент.

## Побочный эффект проверки

Троекратные грабли своей очереди (#208, #252, #189) остаются актуальной
проблемой независимо от вывода этой ADR: раз нативного пути нет, их лечение —
доработка `scripts/orchestra/scheduler.py`, а не замена реализации.
