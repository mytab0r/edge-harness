# wip-gate-honest-zero: WIP-гейт не врёт про «доработки нет» (#948, #951)

Задачи: #948, #951. Дельта-спека:
[specs/journal-tasks-hands/spec.md](specs/journal-tasks-hands/spec.md).

## Зачем

Живой инцидент 2026-09-11 (watchdog-issue #120): предохранитель WIP-лимита
(`wip-limit-dispatch`, #464) объявил `⏸️ ... 25 ≥ лимита 12` в 10:02:44Z, а
25 минут спустя — `✅ ... 0 < 12`, хотя независимый пересчёт тем же
критерием в тот же день дал 27, не 0. Гейт открыл диспатч новых задач на
ложном нуле — ровно тот сценарий, от которого сам гейт заведён.

Корень: `review_labels.list_pages` (и три задублированные копии того же
цикла — `pulse_guard.all_issue_comments`/`open_ci_failure_issues`/
`ci_failure_created_since`, `contract_check._all_open_pulls`) молча
трактовали не-list ответ GitHub API (тело вторичного рейт-лимита, пустое
тело — прод-форма подтверждена логом инцидента) как честную короткую
страницу. `scheduler.open_pulls` шёл через эту функцию — искажённый снимок
читался `wip_gate` как «доработки нет».

Ревью PR #950 (три прохода) добавило к этому три требования из того же
класса «гейт не врёт о своём исходе», описанные ниже.

## Что делается

1. **Пагинация fail loud.** `list_pages` поднимает `RuntimeError` на любом
   не-list ответе; честная пустая страница (`chunk == []`) продолжает
   работать как раньше. Четыре задублированных цикла сведены в один. Гвардия
   класса — `scripts/lib/test_silent_empty_page_guard.py` (AST-разбор
   узора, не текстовый grep). Один живой инстанс того же дефекта
   (`scripts/review/file_tasks.py::_pages`) сознательно не тронут в этом
   change — параллельная работа по `scripts/review/*`, см. отдельную задачу
   в беклоге (issue #975), allowlist гвардии называет причину явно.

2. **Инвариант 16** (`repo_invariants.check_wip_gate_false_zero`) —
   наблюдательный сторож рецидива того же наблюдаемого симптома независимо
   от причины: независимый пересчёт `pr_needs_rework` по свежему снимку
   открытых PR против последнего маркера гейта в #120, в freshness-окне.
   Формула ловит и переворот решения допуска, и буквальный «ложный ноль»
   (claimed=0, actual>0 при совпавшем решении) — см. дельта-спеку.

3. **Гейт прод-записи `prod_writes_allowed` распространён на два транспорта
   в обход** (находки ai-review PR #950, второй и третий проходы):
   - `scripts/lib/claim_task.py` — общий модуль claim/release, используемый
     МНОГИМИ каналами (worker task.sh, hands, `scheduler.py`), из которых
     только `scheduler.py` обязан писать исключительно в CI. Решено
     инъекцией: `claim_task.set_write_guard(predicate)` — единственный
     подписчик, `scheduler.py`, подключает СВОЙ уже существующий предикат
     (`_guard_raw_subprocess_write`) один раз при загрузке модуля; остальные
     каналы хук не трогают и пишут как раньше (локальный claim — их штатный
     режим, не обход гейта).
   - `scripts/orchestra/upstream_drift.py::attempt_auto_bump` — сырой
     `git push`/`scripts/git/pr-create` в обход `gh()`, той же природы, что
     `scheduler.update_branch`'ов ORCHESTRA_PAT-путь.

4. **Отказ гейта наблюдаем, не молчит успехом** (находка третьего прохода
   ревью): до этой правки `claim_task.gh()`/`pulse_guard.gh()` при отказе
   гейта тихо возвращали `None`, а `release()`/`release_full()`/
   `collect_stale()`/`escalate()` не проверяли это и рапортовали успех
   («замок снят», «след оставлен») — ровно тот локальный DRY-RUN-сценарий,
   ради которого гейт заведён, врал в отчёте. Оба `gh()` теперь бросают
   `WriteGateSkipped` (наследник `RuntimeError`, отдельный класс от сбоя
   сервера) при отказе гейта; перечисленные функции ловят его и честно
   пишут «пропущено (DRY-RUN)».

5. **Новый автономный механизм — `dispatch_ai_review_rework`.** Адресный
   диспатч `worker.yml` на доводку PR с меткой `ai:changes-requested` (по
   образцу уже существующего `dispatch_conflict_rework`, issue #474):
   снятие assignee и замка задачи через тот же `claim_task`, запуск
   `worker.yml` с `input[task]`, бюджет попыток на отпечаток диффа
   (`AI_REWORK_MAX_ATTEMPTS`) с эскалацией владельцу при честном
   исчерпании — инфра-отказ самого прогона (`FAILURE_CONCLUSIONS`)
   автоповторяет диспатч без эскалации и без зачёта бюджета (#1027),
   дедуп по отпечатку через маркер в комментариях PR. Conflict-PR исключены
   (их ведёт `dispatch_conflict_rework`, второго диспетчера на тот же PR за
   пульс это не даёт).

## Вне рамок этого change

- Центральный `openspec/specs/journal-tasks-hands.md` не документирует
  `prod_writes_allowed`/DRY-RUN-режим планировщика и `dispatch_conflict_rework`
  вовсе — оба введены раньше этого change без своей дельта-спеки. Закрывать
  этот более широкий пробел документации здесь означало бы открывать новый
  фронт вне рамок задачи; дельта-спека этого change документирует только
  то, что меняет ИМЕННО он (см. `docs/agents/OPENSPEC-PROTOCOL.md`,
  «момент переноса в центральный файл — до или вместе с архивацией», не
  предмет этого change).
- Пятый живой инстанс того же класса «не-list ответ трактуется как честная
  короткая страница» (`scripts/review/file_tasks.py::_pages`) — issue #975,
  причина отсрочки названа в самом allowlist гвардии.

## Проверено

- `python -m pytest scripts/lib/test_claim_task.py scripts/lib/test_review_labels.py
  scripts/lib/test_silent_empty_page_guard.py scripts/orchestra/test_pulse_guard.py
  scripts/orchestra/test_repo_invariants.py scripts/orchestra/test_upstream_drift.py
  scripts/orchestra/test_contract_check.py scripts/orchestra/test_stall_detector.py
  scripts/orchestra/test_waiting_owner_guard.py scripts/orchestra/test_scheduler.py -q`
  — зелёные (число теста в прозе не фиксируем — протухает при следующем ребейзе).
- Мутация 1: убери `raise WriteGateSkipped` в `claim_task.gh()`/`pulse_guard.gh()`
  (верни `return None`) — `test_write_guard_gates_release_end_to_end`,
  `test_release_full_assignee_removal_is_dry_run_via_write_guard`,
  `test_collect_stale_expired_lock_is_dry_run_via_write_guard`,
  `test_gh_dry_run_skips_write_call_outside_ci`,
  `test_escalate_reports_comment_skipped_not_left_when_write_gated` краснеют.
- Мутация 2: убери ветку `literal_false_zero` в `check_wip_gate_false_zero`
  (верни `if claimed_closes_gate == actual_closes_gate: return []`) —
  `test_check_wip_gate_false_zero_flags_literal_zero_below_limit` краснеет.
- Мутация 3 (из предыдущих проходов ревью, не переделана): снятие fail-loud
  в `list_pages` красит и прицельные тесты, и класс-гвардию.
