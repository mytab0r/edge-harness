#!/usr/bin/env bash
# Транспорт DSH для AI-ревью (#18): установка по пинам из lib, GLM-патч
# профиля, прогон headless. Ничего содержательного здесь не решается.
#
# Доверенная граница (trust-zone задачи #18): этому скрипту НЕ передаётся
# GitHub-токен — шаг workflow не имеет ни GH_TOKEN, ни git-креденшелов
# (checkout с persist-credentials: false). Агент физически не может
# запостить комментарий/метку/пуш: его единственный выход — файл ответа,
# который разбирает доверенный шаг verdict (ai_review.py verdict).
# DEEPSEEK_API_KEY нужен самому DSH для вызова модели; DSH вырезает env
# *TOKEN*/*KEY*/*SECRET* из model-shell вызовов — агент и его не видит
# (проверено живым прогоном 2026-08-30, см. worker.yml).
#
# Ретрай на временный RATE_LIMIT провайдера (#419, механизм теперь общий с
# worker/hands — #422, вынесен в dsh_run_with_retry в lib/dsh-ci.sh): живой
# случай — прогон worker.yml 34007508064 упал с «dsh: RATE_LIMIT: Rate limit
# reached for requests» из-за квоты, съеденной параллельными ai-review. Job
# живёт до 6 часов (docs/research/21-github-actions.md) — ждать есть чем, но
# не бесконечно и не всегда: docs/runbooks/switch-llm-provider.md различает
# два признака RATE_LIMIT в stderr dsh —
#   «RATE_LIMIT: Weekly/Monthly Limit Exhausted…» — недельная/месячная
#     квота, сброс через дни; ждать внутри одного прогона бессмысленно —
#     падаем сразу, как настоящую ошибку (failure_reason=quota_exhausted).
#   «RATE_LIMIT: …» без этой формы (живой случай — «Rate limit reached for
#     requests») — короткое окно, снимается ожиданием: повторяем с
#     экспоненциальной паузой и общим бюджетом (failure_reason=
#     rate_limit_retry_budget_exceeded, если бюджет кончился раньше успеха).
# Любая другая ошибка (нет строки RATE_LIMIT вовсе — ключ/модель/битый
# запрос/сеть) падает немедленно, как и раньше: это не временное состояние.
# failure_reason — единственный НОВЫЙ канал наружу, читает ai-review.yml
# (шаг «Ответ ревью») и передаёт в ai_review.py verdict --failure-reason:
# различает в тексте вердикта «возможности нет» (лимит) от «возможность
# есть, но сломана» (реальная ошибка) — правило AGENTS.md.
#
# Имена ручек ретрая (AI_REVIEW_RATE_LIMIT_*) — публичный контракт этого
# скрипта (используется dsh-clients.smoke.sh), поэтому остаются как есть и
# просто транслируются в универсальные ручки dsh_run_with_retry ниже —
# смена имени сломала бы внешний вызывающий тест, а не только внутренний код.
#
# Цепочка провайдеров (#727): quota_exhausted и повторяемый транспортный
# отказ (HTTP_404/EMPTY_RESPONSE, тот же класс, что RATE_LIMIT выше) больше
# не роняют ревью — dsh_run_with_provider_chain (lib/dsh-ci.sh) пробует
# следующего провайдера из vars.DSH_PROVIDER_CHAIN САМ, без ручной смены
# vars.DEEPSEEK_*/секрета. Ошибка контракта вердикта (модель ответила не по
# формату) сюда не попадает вовсе — это решает ai_review.py::parse_verdict
# выше по стеку, цепочка её не видит.
#
# Использование: AI_WORK=<каталог с prompt.md> bash scripts/review/ai_dsh.sh
# Результат: $AI_WORK/answer.txt (ответ последней попытки), $AI_WORK/
# stderr.txt (последней попытки), $AI_WORK/dsh_rc.txt (код возврата dsh
# последней попытки — единственный сигнал, различающий «транспорт упал» от
# «дсш вернул текст»; смотри verdict в ai_review.py), $AI_WORK/
# failure_reason.txt (пусто на успехе/обычном транспортном отказе;
# quota_exhausted | rate_limit_retry_budget_exceeded | all_providers_exhausted
# иначе), $AI_WORK/chain_provider.txt (имя провайдера, ответившего успехом,
# пусто на отказе), $AI_WORK/chain_reset_hint.txt (даты сброса опробованных
# провайдеров, «имя: дата; …» — пусто, если ни один не назвал дату).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Пины DSH, integrity, GLM-патч профиля — единственное место правды в lib.
# shellcheck source=scripts/lib/dsh-ci.sh
source "$SCRIPT_DIR/../lib/dsh-ci.sh"

AI_WORK="${AI_WORK:?AI_WORK не задан (каталог с prompt.md и answer.txt)}"
DSH_TIMEOUT_SECS="${DSH_TIMEOUT_SECS:-3600}"   # 60 минут на КАЖДУЮ попытку
# Бюджет ретрая временного RATE_LIMIT — суммарно, не на попытку. По
# умолчанию 30 минут: короткое окно провайдера обычно снимается секундами-
# минутами, а не часами (в отличие от Weekly/Monthly, для которого ретрая
# нет вовсе, см. выше) — и job-слот Free-плана (20 одновременных,
# docs/research/21-github-actions.md) не стоит занимать надолго ожиданием
# без надежды на разумный успех.
AI_REVIEW_RATE_LIMIT_MAX_WAIT_SECS="${AI_REVIEW_RATE_LIMIT_MAX_WAIT_SECS:-1800}"
AI_REVIEW_RATE_LIMIT_INITIAL_DELAY_SECS="${AI_REVIEW_RATE_LIMIT_INITIAL_DELAY_SECS:-30}"
AI_REVIEW_RATE_LIMIT_MAX_DELAY_SECS="${AI_REVIEW_RATE_LIMIT_MAX_DELAY_SECS:-300}"
[ -f "$AI_WORK/prompt.md" ] || { echo "::error::нет $AI_WORK/prompt.md — шаг gather не отработал" >&2; exit 1; }
# Одно место правды — vars.DSH_PROVIDER_CHAIN репозитория (#727): зашитого
# списка провайдеров в коде нет, dsh_patch_profile/DEEPSEEK_* выставляются
# ПОСЛЕ, отдельно на каждую попытку внутри dsh_run_with_provider_chain.
dsh_require_provider_chain || exit 1

: >"$AI_WORK/answer.txt"; : >"$AI_WORK/stderr.txt"; : >"$AI_WORK/failure_reason.txt"

dsh_install "$AI_WORK/pkgs"
dsh --version || true
# Suite ротации учёток (#215, dsh-combo-router+anthropic-oauth-pool) здесь
# НАМЕРЕННО не подключается: этот шаг всегда идёт через
# dsh_run_with_provider_chain (#727 ниже), которая сама зовёт
# dsh_patch_profile на КАЖДУЮ попытку — суть цепочки в том, что
# DSH_CHAIN_PROVIDER/реестр подтверждённых моделей (#737) знают ТОЧНО, какого
# провайдера пробуют. combo/auto suite решает тот же вопрос («кого пробовать
# дальше») внутри себя и в обход этих проверок — комбинация не поддержана
# конструктивно (design.md dsh-in-job, «Стык suite и цепочки провайдеров»).
# dsh_require_provider_chain ниже уже откажет громко, если vars.PLUGINS_SUITE_URL
# всё же попадёт в env этого шага — но ai-review.yml её сюда не прокидывает:
# suite остаётся уделом hands.yml (#797: worker.yml тоже подключён к цепочке,
# там suite сегодня и так неактивен — vars.PLUGINS_SUITE_URL пуста).

# cwd = pr-head (дерево PR — ДАННЫЕ агента; доверенный код лежит в main-чекауте
# воркспейса) и не меняется до конца прогона — контракт dsh.
DSH_RATE_LIMIT_MAX_WAIT_SECS="$AI_REVIEW_RATE_LIMIT_MAX_WAIT_SECS" \
DSH_RATE_LIMIT_INITIAL_DELAY_SECS="$AI_REVIEW_RATE_LIMIT_INITIAL_DELAY_SECS" \
DSH_RATE_LIMIT_MAX_DELAY_SECS="$AI_REVIEW_RATE_LIMIT_MAX_DELAY_SECS" \
  dsh_run_with_provider_chain "$AI_WORK/answer.txt" "$AI_WORK/stderr.txt" "$(cat "$AI_WORK/prompt.md")"
rc=$DSH_RUN_RC
if [ -n "$DSH_RUN_FAILURE_REASON" ]; then
  printf '%s' "$DSH_RUN_FAILURE_REASON" >"$AI_WORK/failure_reason.txt"
fi
printf '%s' "$DSH_CHAIN_PROVIDER" >"$AI_WORK/chain_provider.txt"
printf '%s' "$DSH_CHAIN_RESET_HINT" >"$AI_WORK/chain_reset_hint.txt"

printf '%s' "$rc" >"$AI_WORK/dsh_rc.txt"

# rc≠0 НЕ роняет ЭТОТ шаг: судьбу решает доверенный verdict-шаг. Но rc и
# failure_reason едут дальше НЕзамаскированным сигналом — verdict обязан
# отличить «транспорт упал (rc≠0)» от «дсш вернул текст не по контракту
# (rc=0)», иначе ошибка провайдера превращается в ложное обвинение модели
# (silent-wrong), и дальше — «лимита нет вовсе» от «лимит есть, но
# сломано что-то другое» (правило AGENTS.md, #419).
# Хвост stderr — для диагностики в логе, ОБЯЗАТЕЛЬНО через redact: ошибки
# клиента модели — самое вероятное место, куда в публичный лог мог бы уехать
# производный DEEPSEEK_API_KEY (GitHub маскирует только точное совпадение секрета).
echo "--- хвост stderr DSH (последняя попытка) ---"
tail -c 2000 "$AI_WORK/stderr.txt" | redact || true
exit 0
