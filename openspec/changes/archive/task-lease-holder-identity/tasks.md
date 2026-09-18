# tasks: task-lease-holder-identity (#1190)

- [x] `current_holder()` — одно место правды для идентификатора держателя
      (`CLAIM_HOLDER` → `run:<GITHUB_RUN_ID>:<GITHUB_JOB>` → `tree:<cwd>` →
      `proc:<hostname>:<pid>`; суффикс job'а — находка ревью PR #1206: run id
      один на все job'ы прогона), `scripts/lib/claim_task.py`.
- [x] Коммит замка несёт `holder: <id>` второй строкой; `claim()` при отказе
      различает свой/чужой/неизвестный держатель (`_lock_state`,
      `_parse_holder`).
- [x] `release()` получает `holder`/`force`: без `holder` — поведение не
      меняется (scheduler.py не трогается); с `holder` — проверка владения,
      `ForeignLockError` при чужом/неизвестном.
- [x] CLI `claim_task.py release <N> [--force]`: без `--force` передаёт
      `current_holder()`, отказ `ForeignLockError` → `EXIT_BUSY` (не
      `EXIT_ERROR`); `status` печатает держателя каждого замка.
- [x] `task-branch`/`WORKER-PLAYBOOK.md`/`PROTOCOL.md` — комментарии и
      человеко-читаемый текст отказа обновлены под новый механизм (устаревший
      абзац про «идемпотентный claim по держателю опасен» уточнён, не удалён:
      причина отказа для ВАРИАНТА с логином остаётся верной).
- [x] Юнит-тесты (мок, `scripts/lib/test_claim_task.py`): свой/чужой/
      неизвестный держатель на claim и release, CLI `--force`,
      `current_holder()` приоритет источников.
- [x] Поведенческий тест на РЕАЛЬНЫХ git-рефах
      (`scripts/lib/test_claim_task_real_refs.py`): критерий готовности
      issue #1190 дословно (A берёт → B отказ с именем A → A переберёт
      идемпотентно), плюс тот же сценарий для `release()`. Доказан мутацией
      (снял фикс → красный → вернул → зелёный).
- [x] Обратная совместимость: 7 живых рефов старого формата на момент дельты
      проверены — читаются без падения (`holder is None`), не мигрируются
      принудительно.
