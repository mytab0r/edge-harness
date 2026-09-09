# tasks.md: ai-ok-bootstrap-fingerprint-clarify (#828)

## 1. Установить root cause

Исполнитель: разработчик. Проверить каждого из трёх кандидатов
(`check_pr.py::ai_verdict_keep`, `ai_review.py::notify_head_moved`/
`cmd_verdict`, `review_labels.py::diff_unchanged`/`diff_fingerprint`/
`should_run_ai_review`) по коду и по живым таймлайнам PR #818/#819/#826.
Приёмка: file:line причины снятия метки, подтверждённый воспроизведением на
реальных данных GitHub API (не пересказом).

- [x] Сделано: причина — `check_pr.py::ai_verdict_keep`
  (`scripts/review/check_pr.py:178-187`), вызывается на каждом пуше
  (`main()`, строка ~470); `diff_fingerprint` уже трёхточечный (#740), не
  виноват. Настоящая причина глубже: `stored_fp = None`, потому что
  `review_labels.latest_ai_comment` не находит доверенного комментария на
  PR вообще (bootstrap-комментарий от `mytab0r`, не `github-actions[bot]`).

## 2. Регресс-тест прод-формы

Исполнитель: разработчик. Тест на буквальном тексте комментария PR #818,
доказывающий сохранение текущего (корректного) поведения; мутация
(наивный «фикс», трактующий `stored_fp is None` как «сохранить») красит
тест. Приёмка: `python -m pytest scripts/review/test_check_pr.py -q`
зелёный, мутация доказана и отменена.

- [x] Сделано: `scripts/review/test_check_pr.py::
  test_bootstrap_comment_pr818_is_not_trusted_verdict`,
  `test_ai_ok_from_bootstrap_comment_never_survives_next_push_even_unchanged_diff`.

## 3. Документация находки

Исполнитель: разработчик. `scripts/lib/review_labels.py` (докстринг рядом с
`TRUSTED_VERDICT_LOGIN`) и `docs/agents/LABELS.md` (строка `ai:ok`) называют
футган и правильный газ («гейт медленный» → `gh workflow run ai-review.yml
-f pr=N -f force=true`, не ручная подделка комментария). Приёмка: текст
ссылается на #828 и живые PR #818/#819/#826.

- [x] Сделано.

## 4. Задача пула и PR

Исполнитель: разработчик. Issue с меткой `task`+`area:process`, PR через
`scripts/git/task-branch`. Приёмка: PR ссылается на issue #828, все три
набора тестов (`test_check_pr`, `test_ai_review`, `test_review_labels`)
прогнаны.

- [x] Сделано: issue #828, эта ветка.
