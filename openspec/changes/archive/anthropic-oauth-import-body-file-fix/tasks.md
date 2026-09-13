Задача: #840.

# Tasks: anthropic-oauth-import-body-file-fix

- [x] 1. Перенести `scripts/lib/anthropic_oauth_import.py` +
      `scripts/lib/test_anthropic_oauth_import.py` дословно в репозиторий;
      подключить канонический bootstrap `console_utf8.
      BOOTSTRAP_BLOCK_SAME_DIR` (файл лежит в `scripts/lib`) вместо
      ad-hoc `sys.stdout.reconfigure`. Проверка:
      `python -m pytest scripts/lib/test_anthropic_oauth_import.py -q` —
      19 passed.
- [x] 2. Закрыть класс #786 в `scripts/lib/provider_secrets_import.py`:
      `set_secret`/`set_variable` не передают `--body-file` в argv `gh`
      (значение только через `input=`). Обновить `test_set_secret_value_
      never_in_argv` (было `assert "--body-file" in args`, стало `assert
      "--body-file" not in args`) и добавить симметричный
      `test_set_variable_value_never_in_argv`.
- [x] 3. Гвардия по исходнику `scripts/lib/test_gh_body_file_guard.py`:
      сканирует `.py`/`.sh`/бесрасширенные bash-файлы `scripts/` на
      `gh secret set`/`gh variable set` с `--body-file` в argv/командной
      строке. Доказана мутацией: временный возврат `--body-file` в
      `set_secret` красит тест; ревёрт — снова зелено (лог мутации —
      комментарий к issue #840 или PR).
- [x] 4. Рунбук `docs/runbooks/refresh-anthropic-pool.md`: команда,
      источник данных (krouter-бэкап/одиночный credentials.json), почему
      истёкший `accessToken` не проблема, что делать при протухшем
      `refreshToken`. Ссылка добавлена в `docs/INDEX.md` (иначе гвардия
      `test_docs_index_guard.py` красит CI).
- [x] 5. Оба новых теста подключены к обязательному CI через каталог
      гвардий `scripts/ci/guards/` — файлы `anthropic-oauth-import-guard.sh`
      и `gh-body-file-guard.sh`, перебираются `scripts/ci/run_guards.sh`
      (#749). Изначально заводились отдельными рукописными шагами
      `.github/workflows/repo-ci.yml`; ребейз на main с механизмом #749
      сделал рукописный шаг гвардии классом, который красит
      `ci_guard_registration_guard.py`, поэтому регистрация перенесена в
      каталог (новый файл там — единственная регистрация, workflow не
      тронут ни строкой). Канарейка осиротевших тестов
      (`test_orphan_test_guard.py`) считает файлы каталога псевдо-шагами с
      их содержимым — покрытие засчитывается.
- [x] 6. Дельта-спека: `specs/anthropic-oauth-import/spec.md` (новая
      капабилити) + `specs/provider-secrets-import/spec.md`
      (ADDED-требование к существующей капабилити).

## Закрывающая проверка (после мержа)

- `python -m pytest scripts/lib/test_anthropic_oauth_import.py
  scripts/lib/test_gh_body_file_guard.py
  scripts/lib/test_provider_secrets_import.py
  scripts/lib/test_console_utf8_guard.py
  scripts/lib/test_orphan_test_guard.py
  scripts/lib/test_docs_index_guard.py -q` — все зелёные на `main`.
