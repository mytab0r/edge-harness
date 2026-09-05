# Задачи: ai-verdict-survives-merge (#252)

- [x] `scripts/lib/review_labels.py` — `diff_fingerprint`, `diff_unchanged`,
      `header_facts`/`FACT_RE` (перенесены из `ai_review.py`, поле `diff`
      добавлено), `latest_ai_comment`.
- [x] `scripts/review/ai_review.py` — `build_comment(..., diff_fp=None)`
      пишет поле `diff:` в шапку; `header_facts`/`FACT_RE` — алиасы на
      `review_labels` (одно место правды); `cmd_verdict` считает и передаёт
      отпечаток.
- [x] `scripts/review/check_pr.py` — `ai_verdict_keep`, сверка отпечатка перед
      снятием `ai:*`-меток; докстринг пункта 4 переписан под новое поведение.
- [x] Тесты (прод-форма PR #292/#253, `gh api compare/...`, сужено до
      filename/status/sha):
      `scripts/lib/fixtures_pr292_merge_diff.json`,
      `scripts/lib/fixtures_pr253_edit_diff.json`,
      `scripts/lib/test_review_labels.py` (fingerprint/unchanged/header/latest_ai_comment),
      `scripts/review/test_check_pr.py` (`ai_verdict_keep`, обе стороны + мутация),
      `scripts/review/test_ai_review.py` (`build_comment` с полем diff).
- [x] `docs/agents/LABELS.md` — строки `ai:ok`/`ai:changes-requested`/
      `ai:failed`: условие снятия уточнено («новый пуш с изменённым диффом»,
      не любой пуш), ссылки переведены на форму `file::symbol`.
- [x] Дельта-спека `openspec/changes/ai-verdict-survives-merge/`.

## Правка после вердикта AI-ревью PR #294 (три находки)

- [x] `scripts/lib/review_labels.py` — `list_pr_files` (общая пагинация
      `pulls/{n}/files`, переиспользуется `check_pr.py` и `ai_review.py`),
      `should_run_ai_review` (различает `ai:failed` от `ai:ok`/
      `ai:changes-requested`, газ #196 не отнимается).
- [x] `scripts/review/ai_review.py` — подкоманда `should-run`
      (`cmd_should_run`), `cmd_gather`/`cmd_verdict` переведены на
      `review_labels.list_pr_files`.
- [x] `scripts/review/check_pr.py` — `main()` переведён на
      `review_labels.list_pr_files`.
- [x] `.github/workflows/ai-review.yml` — новый шаг `fingerprint` (job
      `review`) перед чекаутом `pr-head`/`gather`/DSH: `go=false` при
      неизменном диффе и окончательном вердикте, останавливает job до
      дорогой работы, не только не переставляет метку.
- [x] Тесты (прод-форма, доказаны мутацией):
      `scripts/lib/fixtures_pr_over_100_files.json` (синтетическая
      фикстура — реальных PR с >100 файлами в репозитории нет, склеены две
      настоящие страницы `pulls/{n}/files` PR #278 и #10),
      `scripts/lib/test_review_labels.py` (`list_pr_files` пагинация,
      `should_run_ai_review` все ветки + мутация),
      `scripts/review/test_check_pr.py`/`test_ai_review.py` (пагинация не
      теряет хвост в `added`).
- [x] `openspec/changes/ai-verdict-survives-merge/proposal.md` — раздел
      «Что вне рамок» и `docs/agents/LABELS.md` приведены в соответствие с
      реальным механизмом (workflow_run, не метка), ссылки в форме
      `file::symbol`.

## Правка после вердикта AI-ревью PR #294 (четыре находки)

- [x] `scripts/lib/review_labels.py::latest_ai_comment` /
      `scripts/review/file_tasks.py::latest_review_comment` — дыра
      безопасности: комментарий с шапкой `reviewer:` принимался от ЛЮБОГО
      автора (репозиторий публичный, `diff:` вычислим кем угодно из
      публичного `pulls/{n}/files`). Фильтр по автору
      (`_is_trusted_verdict_author`, `github-actions[bot]`/`Bot`), тест
      воспроизводит атаку и доказан мутацией (снят фильтр — тест краснеет).
- [x] `docs/agents/LABELS.md`, строка `ai:failed` — фраза «при неизменном
      диффе новый пуш не перезапускает» была противоположна коду
      (`should_run_ai_review` для `ai:failed` не пропускает никогда);
      исправлено на «перезапускает, таймер #196 — независимый путь».
- [x] `proposal.md` — критерий направления 1 переформулирован: метрика —
      число ДОРОГИХ прогонов (дошедших до шага gather), не общее число
      стартов `ai-review.yml` (`workflow_run`-триггер не тронут); добавлен
      `gh api`-запрос для подсчёта.
- [x] `scripts/lib/test_review_labels.py::test_diff_fingerprint_misses_tail_edit_without_pagination_pr294` —
      тавтологичное сравнение `diff_fingerprint(page1) == diff_fingerprint(page1)`
      (одинаковый вход) снято; защита от регресса «прод-код читает только
      первую страницу» — grep-гвардии в `test_check_pr.py`/`test_ai_review.py`.

## Правка после вердикта AI-ревью PR #294 (две находки)

- [x] `scripts/review/ai_review.py::cmd_verdict` — гонка: между сверкой
      головы PR и чтением файлов (`review_labels.list_pr_files`) проходит
      сетевой вызов, в который автор успевает запушить новый коммит; без
      повторной сверки вердикт применялся бы к нерецензированному диффу, а
      #252 делал бы протухший `ai:*` вечным вместо снятия на следующем пуше.
      Голова сверяется ЕЩЁ РАЗ сразу после `list_pr_files`, до применения
      метки/`apply_large_ok`/комментария; заодно закрыт устаревший `added`
      (считался бы по файлам уехавшей головы). Тест-гвардия порядка вызовов
      (голова → файлы → голова ещё раз) и невозможности применения вердикта
      при расхождении — `scripts/review/test_ai_review.py`
      (`test_cmd_verdict_order_head_then_files_then_head_again`,
      `test_cmd_verdict_race_head_moves_during_file_read_skips_verdict`,
      `test_cmd_verdict_race_mutation_guard_without_second_head_check`),
      доказана мутацией (снята повторная сверка — оба целевых теста
      краснеют).
- [x] Критерий приёмки 5 issue #252 («реальная история коммитов PR #173»)
      дожат прод-фикстурой: `scripts/lib/fixtures_pr173_merge_diff.json`
      (`gh api compare/main...<sha>` для merge-коммита `425d8382e6`,
      2026-09-01T22:01:09Z — таймстемп, названный самим критерием),
      `scripts/lib/test_review_labels.py::test_diff_fingerprint_unchanged_across_clean_merge_from_main_pr173_acceptance_criterion_5`,
      `scripts/review/test_check_pr.py::test_ai_verdict_keep_true_after_clean_merge_pr173_acceptance_criterion_5`.
