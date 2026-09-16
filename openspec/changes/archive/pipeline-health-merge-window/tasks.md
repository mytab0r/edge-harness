# Tasks

- [x] `pipeline_health.py::merge_throughput_window` — окно `[вчера 00:00, сегодня 00:00)`
  UTC, задокументированный выбор против скользящих 24ч (design.md §1).
- [x] `collect()` использует `search_merged_prs` с этим окном вместо
  `merged:{today}..{today}`; `build_snapshot` несёт `merge_throughput_window_start/_end`
  в самом снимке.
- [x] Поведенческий тест на прод-форме, воспроизводящий живой инцидент (снимок 00:15 UTC,
  16 слияний за предыдущие сутки, не 0) — `test_collect_merge_throughput_survives_snapshot_shortly_after_midnight`.
- [x] Доказательство мутацией, исполняемое (`MUTATION-PROOF`,
  `scripts/lib/mutation_recipe_guard.py`) — вернуть окно `{today}..{today}`, тест краснеет.
- [x] `health_regression.py::_is_reliable_sample` — легаси-строки `merge_throughput` без
  `merge_throughput_window_start` исключены из построения baseline (решение по старой
  истории, design.md §2).
- [x] Таблица соседних метрик (design.md §3) — единственный повторный случай того же
  класса (`do_rows_read_pct`) заведён отдельно, issue #1278.
- [x] Ряд «было/стало» за 5 суток (design.md §4).
