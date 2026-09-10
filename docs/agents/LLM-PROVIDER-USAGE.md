# Реестр использования LLM-провайдеров — кто чем пользуется

Один файл, единственное место видимости, какой потребитель какой именованной
цепочкой провайдеров пользуется. Заведён задачей #823
(`openspec/changes/llm-provider-usage-manifest`) по прямому мотиву: до этого
change «кто чем пользуется» решалось тремя независимыми копиями чтения
`vars.DSH_PROVIDER_CHAIN` (`scripts/review/ai_dsh.sh`, `scripts/worker/task.sh`,
`scripts/hands/dsh_task.sh`) без единого места, где это видно разом —
разрыв между ними уже стрелял (воркер получил failover на 72 задачи позже
ревью, #727 -> #797, proposal.md «Problem»).

Источник правды на СОДЕРЖИМОЕ — [`config/provider-usage.json`](../../config/provider-usage.json)
(вне `scripts/`, `.github/workflows/`, `docs/agents/` — гвардия класса #153 эти
пути не сканирует). Штатный путь его правки — Settings морды dsh-edge
(design.md, «Механизм пуска») — **пока не подключён** (Этап 2
`openspec/changes/llm-provider-usage-manifest/tasks.md`, см. находку про
white-spot #824); до его подключения файл правится тем же способом, что раньше
правился `vars.DSH_PROVIDER_CHAIN` — вручную, коммитом в этот репозиторий.

Таблица ниже **не хардкожена**: её печатает
[`scripts/lib/collect_provider_usage.py`](../../scripts/lib/collect_provider_usage.py)
чтением `config/provider-usage.json`. Гвардия
[`scripts/lib/test_provider_usage_registry.py`](../../scripts/lib/test_provider_usage_registry.py)
сверяет вывод сборщика со строками этой таблицы на каждый CI-прогон
(`.github/workflows/repo-ci.yml`) и совпадает с CI-инвариантом 11
(`scripts/orchestra/repo_invariants.py::check_provider_usage_manifest`,
гейтящий): «у потребителя нет валидного назначения» падает и там, и там одним
и тем же критерием.

## Реестр

| Потребитель | Цепочка | Провайдеров | Механизм |
|---|---|---|---|
| `ai-review` | `default-chain` | 9 | dsh_run_with_provider_chain (scripts/review/ai_dsh.sh) — полный failover |
| `worker` | `default-chain` | 9 | dsh_run_with_provider_chain (scripts/worker/task.sh) — полный failover |
| `hands` | `default-chain` | 9 | dsh_run_with_provider_chain (scripts/hands/dsh_task.sh) — полный failover (#805) |
| `morda` | (вне манифеста) | — | 1 слот адаптера через Settings -> Models (plugins-src/provider-registry, #378) — failover туда не помещается, вне манифеста принципиально (docs/runbooks/switch-llm-provider.md, «Морда — вне цепочки принципиально») |

С #857 (`openspec/changes/provider-quota-gating`) запись `GLM` переставлена
из хвоста цепочки (где её держал #848 из-за живой квоты на момент замера
латентности) в начало, за ней добавлена запись `ZAI` (второй z.ai-аккаунт)
— безопасно только вместе с персистентным квота-гейтом
(`scripts/lib/dsh-ci.sh::dsh_provider_quota_gate_skip`,
`vars.DSH_PROVIDER_QUOTA_UNTIL`): пока квота этих двух записей не сброшена,
чейн-раннер пропускает их ДО попытки, не тратя вызов на заведомо
исчерпанный провайдер. Запись `ZAI` не подтверждена реестром #737 (см.
`$comment` записи в `config/provider-usage.json` — конкретные id моделей
литералом не приводятся здесь: класс #153, этот файл конкатенируется прямо
в промпт агента) — безопасный no-op до живой сверки `/v1/models`. Остаток
цепочки (OpenRouter/Ollama/NVIDIA-NIM) и его порядок не менялись —
унаследованы от #848 (discovery по измеренной латентности).

Обнови эту таблицу той же командой, которой её сгенерировал сборщик —
гвардия сверит буквально:

```
python scripts/lib/collect_provider_usage.py
```

## Честный потолок

Тот же потолок, что уже назван в [`LABELS.md`](LABELS.md): гвардия проверяет
ФОРМУ записи («у потребителя есть непустая цепочка с известным именем»), не то,
что цепочка реально переживёт квоту в проде — это подтверждается только живым
прогоном (например `scripts/lib/test/dsh-provider-chain.smoke.sh`), не текстом
реестра. `morda` — видимость, а не назначение: у неё принципиально нет
цепочки (один слот адаптера), гвардия эту строку не проверяет на валидность
цепочки, только на присутствие самой строки.

## Как читает потребитель

`scripts/lib/dsh-ci.sh::dsh_require_provider_chain "<id>"` резолвит цепочку из
`config/provider-usage.json` по `<id>` ПЕРЕД валидацией: манифеста нет вовсе —
фоллбэк на `vars.DSH_PROVIDER_CHAIN` как раньше (переходный период); манифест
есть, но у `<id>` нет валидной записи — падает громко (класс #727 -> #797:
«нет валидного назначения» обязано быть видно на прогоне, не только здесь).
