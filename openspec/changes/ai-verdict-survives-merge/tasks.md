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
