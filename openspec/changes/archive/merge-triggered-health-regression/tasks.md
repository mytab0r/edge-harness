# Tasks: merge-triggered-health-regression

Разбивка реализации design.md. Пороги (окна, минимальные выборки) —
константы `scripts/orchestra/merge_health_watch.py`, честно помечены в
design.md «Не подтверждено» как проверенные на одном реальном инциденте, не
выведенные статистически из множества.

## Метрика — time-boxed success-rate (design.md §1)

- [x] `scripts/measure/pipeline_health.py::search_merged_prs` — часовая
  (не суточная) выборка слитых PR через `search/issues`, переиспользуется
  детектором для окна атрибуции.
- [x] Тесты прод-формой (`total_count`/`items`/`pull_request.merged_at`),
  проверка часового формата `merged:` квалификатора.

## Детектор регрессии, привязанный к слиянию (design.md §2)

- [x] `scripts/orchestra/merge_health_watch.py::evaluate` — rolling
  recent/baseline, три честных исхода, пороги regression/fire — импорт из
  `health_regression.py`, не копия чисел.
- [x] `suspects_from_search` — все PR окна атрибуции, честный флаг
  `truncated` при обрезанной странице Search API.
- [x] `render_report`/`render_body` — числа названы явно, «корреляция, не
  причинность» — текстом, не гаданием.
- [x] Тесты: мутация порога `>=`/`>` (regression vs fire на границе),
  честный `insufficient_data`, суспекты сортированы хронологически.

## Доказательство на реальной истории (design.md §4)

- [x] Фикстура — дословный вывод `gh api .../worker.yml/runs` за
  2026-09-08..11 (87 прогонов) и `search/issues` за ту же ночь (6 PR),
  снято 2026-09-11.
- [x] Тест по часам после слияния PR #878: `insufficient_data` (0-1ч),
  `ok` (2-4ч), `fire` (5-6ч) — подтверждает обнаружение в пределах часов.

## Реакция (design.md §5)

- [x] `run_watch` — эскалация тем же каналом, что остальной конвейер
  (`pulse_guard.escalate`), задача `task`+`area:process` через уже
  существующий `pool_issue.create_pool_issue` (не второй канал, не новая
  метка).
- [x] Дедуп по отпечатку `merge-health:worker_success_rate:<час>` — повторный
  `fire` в тот же час комментирует существующую задачу, не дублирует.
- [x] Тесты проводки: `ok` не создаёт задачу и не эскалирует; `fire`
  заводит новую задачу при первом обнаружении; повторный `fire` того же часа
  комментирует существующую.

## Проводка (design.md, «Тормоз без газа», «Решение — механизм, не текст»)

- [x] Отдельный workflow `.github/workflows/merge-health-watch.yml` (часовой
  cron `17 * * * *`, `workflow_dispatch`) — не правка `orchestra.yml`
  (чужая территория на момент подачи, issue #967). Только `github.token`,
  без `GH_PIPELINE_PAT` — ни одного git push.
- [x] Гвардия каталога `scripts/ci/guards/merge-health-watch-guard.sh`
  (регистрация как данные, класс #771 — без правки `repo-ci.yml`).
- [x] Реестр `scripts/lib/test_dispatch_token_usage.py::EXPECTED_WORKFLOWS`
  — новый workflow объявлен явно, класс его использования токенов назван.
- [x] Инвентарь `docs/agents/INFRA-GH.md` — строка нового workflow
  (`test_infra_gh_inventory.py` зелёный).
- [x] `docs/agents/LABELS.md` — строка `area:process` дополнена: второй,
  автоматический источник простановки метки назван явно (`test_label_registry.py`
  зелёный).
- [x] `openspec/changes/merge-triggered-health-regression/specs/pipeline-health/spec.md`
  — дельта-спека (ADDED requirements) поверх архивной `pipeline-health-self-audit`.

## Вне рамок этого прохода (названо, не реализовано)

- Подбор точных порогов по множеству независимых инцидентов — история этой
  часовой метрики физически не существует дольше одного инцидента на момент
  подачи (design.md, «Не подтверждено», п.1).
- Эвристика исключения «невиновных» подозреваемых по анализу диффа —
  рассмотрена и сознательно не реализована (design.md §3, «Не подтверждено», п.2).
- Правка `scripts/orchestra/scheduler.py` (адресный `workflow_dispatch`
  воркера) — чужая территория; тир-1 приоритет `area:process` даёт
  эквивалентную по порядку скорость пикапа без неё (design.md §5).
