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
