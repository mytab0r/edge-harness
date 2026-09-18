#!/usr/bin/env bash
# DSH headless для оркестратора сообщений.
# Читает промпт из $AI_WORK/prompt.txt, вызывает DSH, пишет ответ в $AI_WORK/answer.txt.
# Аналог scripts/review/ai_dsh.sh, но для произвольных сообщений (не PR-ревью).

set -euo pipefail

AI_WORK="${AI_WORK:-$RUNNER_TEMP/orchestrator-message}"
PROMPT_FILE="$AI_WORK/prompt.txt"
ANSWER_FILE="$AI_WORK/answer.txt"
FAILURE_FILE="$AI_WORK/failure_reason.txt"

# Очищаем failure_reason на старте (как в ai_dsh.sh)
: > "$FAILURE_FILE"

if [ ! -f "$PROMPT_FILE" ]; then
    echo "prompt.txt не найден в $AI_WORK" >&2
    exit 1
fi

# Профиль DSH для headless — используем существующий headless профиль или создаём временный
DSH_PROFILE="orchestrator-headless"
PROFILE_DIR="$HOME/.dsh/profiles/$DSH_PROFILE"

mkdir -p "$PROFILE_DIR"

# cordis.patch.yml для headless: модель из цепочки провайдеров, без интерактивных аппрувов
cat > "$PROFILE_DIR/cordis.patch.yml" <<'EOP'
agent-default-model:
  provider: combo
  model: auto
llm-combo:
  maxTokens: 8192
EOP

# DSH headless: one-shot, exit 0 только при turn/end completed
# Аппрувы fail-closed — задача оркестратора не требует аппрувов
cd "$GITHUB_WORKSPACE"

# Вызов DSH через dsh_run_with_provider_chain (как в ai_dsh.sh)
# Передаём промпт как stdin
dsh_run_with_provider_chain \
  --profile "$DSH_PROFILE" \
  --headless \
  --prompt-file "$PROMPT_FILE" \
  --timeout-secs "${DSH_TIMEOUT_SECS:-1800}" \
  2>&1 | tee "$AI_WORK/dsh_output.txt" | tail -20

# Код возврата dsh — сигнал «транспорт упал»
DSH_RC=${PIPESTATUS[0]}
echo "$DSH_RC" > "$AI_WORK/dsh_rc.txt"

if [ "$DSH_RC" -ne 0 ]; then
    echo "DSH завершился с кодом $DSH_RC" >&2
    # Не пишем failure_reason — это транспортная ошибка, verdict её обработает
    exit 0
fi

# Ответ модели — последняя часть вывода (после turn/end)
# Для надёжности берём весь вывод dsh_output.txt как ответ
cp "$AI_WORK/dsh_output.txt" "$ANSWER_FILE"

echo "Ответ записан в $ANSWER_FILE"
exit 0