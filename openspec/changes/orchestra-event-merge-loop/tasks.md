# Задачи: orchestra-event-merge-loop (#297)

- [x] `.github/workflows/pr-review.yml` — шаг `gh workflow run orchestra.yml
      --ref main` в конце job `review` (после `check_pr.py`), `permissions:
      actions: write` добавлено на уровень файла.
- [x] `.github/workflows/ai-review.yml` — тот же шаг в конце job `verdict`
      (после `ai_review.py verdict`), `permissions: actions: write`
      добавлено на уровень job'а `verdict`.
- [x] `scripts/orchestra/scheduler.py`:
      - `merge_queue()` возвращает дополнительно `(merged_number, updated)` —
        сигнал для цикла, был ли мерж/обновление ветки в этом проходе;
      - новая `merge_loop()` — цикл проходов `merge_queue`, потолок
        `MERGE_LOOP_MAX_MERGES`, таймаут `MERGE_LOOP_TIMEOUT_SECONDS`, пауза
        `MERGE_LOOP_POLL_SECONDS`, слот `update_branch` сбрасывается КАЖДЫЙ
        проход (было — один раз на весь прогон);
      - `main()` зовёт `merge_loop()` вместо `merge_queue()`;
      - удалена мёртвая константа `ONE_MERGE_PER_RUN` (не читалась нигде).
- [x] Тесты (`scripts/orchestra/test_scheduler.py`), доказаны мутацией:
      - 5 существующих тестов `merge_queue` behind-ветки — обновлены под
        4-элементный возврат;
      - 4 теста main()-уровня — монкипатчат `merge_loop` вместо
        `merge_queue` (main теперь зовёт цикл, не одиночный проход);
      - `test_merge_queue_clean_state_without_ai_ok_not_merged` — гейт
        слияния не обходится в clean-состоянии без обоих вердиктов;
      - `test_merge_loop_merges_multiple_prs_in_one_run` — цикл сливает
        несколько PR за прогон;
      - `test_merge_loop_stops_at_max_merges_cap` — потолок останавливает
        цикл (с предохранителем от зависания в самом фейке);
      - `test_merge_loop_stops_immediately_without_progress` — проход без
        прогресса не даёт цикл продолжать;
      - `test_merge_loop_waits_after_branch_update_before_retry` — пауза
        между проходами, где обновилась ветка, но не слилось.
- [x] Дельта-спека `openspec/changes/orchestra-event-merge-loop/`.
- [ ] Живая проверка после мержа (не юнит-тест): `gh workflow run
      orchestra.yml` из `GH_TOKEN` внутри job'а pr-review/ai-review реально
      создаёт новый run `orchestra.yml` — событие проверяется на первом же
      PR/пуше ПОСЛЕ слияния этой дельты в main (workflow-файл обязан быть на
      default branch, чтобы диспатч сработал, `docs/research/21`).
