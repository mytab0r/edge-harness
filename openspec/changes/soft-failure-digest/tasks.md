# Задачи: soft-failure-digest (#1121)

- [x] Модуль `scripts/orchestra/soft_failure_digest.py`: канал A (аннотации
      Checks API) + канал B (условные шаги, Jobs API), дедуп по открытым
      issues `soft-failure`, суточный потолок, гейт цикла.
- [x] Тесты `scripts/orchestra/test_soft_failure_digest.py` — прод-форма
      фикстур (реальные аннотации `NO_ADAPTER`/`UNKNOWN_MODEL`, реальный
      multi-suite check-runs ответ), мутационное доказательство фильтра
      `check_suite_id`.
- [x] Шаг «Дайджест мягких отказов (#1121)» в `.github/workflows/
      orchestra.yml` (continue-on-error, тот же приём, что соседние гвардии
      пульса).
- [x] Метка `soft-failure` в `docs/agents/LABELS.md` (гвардия
      `test_label_registry.py` зелёная).
- [x] Живой прогон на реальных данных — не менее пяти находок из #1121,
      результат приложен в PR.
- [x] Пагинация `fetch_runs` (класс #308) — orchestra.yml/ai-review.yml
      несут >100 прогонов/сутки, однострочный запрос молча терял их целиком
      (найдено первым же живым прогоном, до мержа).
