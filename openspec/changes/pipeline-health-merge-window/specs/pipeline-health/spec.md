# Дельта-спека: pipeline-health — окно `merge_throughput` (issue #1155)

## MODIFIED: Снимок `merge_throughput` — окно предыдущих полных суток, не «сегодня»

Было (архивная спека `pipeline-health-self-audit`): «merge throughput (PR слито за
сутки)» без указания точных границ окна — реализация читала `search/issues` с
`merged:{сегодня}..{сегодня}`, что снимок раз в сутки (сразу после полуночи UTC)
систематически занижал/обнулял.

Стало: `scripts/measure/pipeline_health.py::collect` считает `merge_throughput` за
`merge_throughput_window(today)` — полуоткрытое окно `[вчера 00:00 UTC, сегодня 00:00
UTC)`, тем же примитивом `search_merged_prs`, что уже использует `merge_health_watch.py`.
Снимок несёт границы окна явно: `merge_throughput_window_start`/`merge_throughput_window_end`
(ISO8601) — подпись значения обязана соответствовать измеренному интервалу.

Сценарий: пульс `orchestra` снимает снимок в 00:15 UTC 2026-09-12 — `merge_throughput`
считает слияния ЗА 2026-09-11 целиком (полные предыдущие сутки), не «за первые 15 минут
2026-09-12».

Требование: снимки, снятые ДО этого фикса (не несущие `merge_throughput_window_start`),
не считаются надёжными точками `merge_throughput` для построения baseline
(`health_regression.py::classify_metric`/`_is_reliable_sample`) — отброшены, а не
трактуются как 0 или как валидное наблюдение.

## Не входит

- `do_rows_read_pct` (`scripts/measure/quotas.py`) несёт родственный, но отдельный
  дефект (алиасинг снимка/сброса суточной квоты) — issue #1278, вне этого change.
- Пересчёт задним числом уже записанных строк `data/pipeline-health.jsonl` — решение
  «начать новый ряд» через структурный маркер (design.md §2), не запись на data-ветку.
