#!/bin/bash
# DSH Web: конфигурируем дополнительные LLM провайдеры через env-переменные.
# Использование: source dsh-web-setup-providers.sh перед `dsh web`

set -euo pipefail

DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
PROFILE="${DSH_PROFILE:-web}"
PROFILE_DIR="$DSH_HOME/profiles/$PROFILE"
SETTINGS_FILE="$PROFILE_DIR/settings.yaml"

# Если settings.yaml ещё не создан, создаём скелет
mkdir -p "$PROFILE_DIR"
if [ ! -f "$SETTINGS_FILE" ]; then
    cat > "$SETTINGS_FILE" <<'EOF'
llm-pi-ai:
  providers: {}
EOF
fi

# Функция для добавления OpenAI-compat провайдера
add_openai_provider() {
    local provider_id=$1
    local provider_name=$2
    local base_url=$3
    local api_key=$4
    local model=$5

    # ponytail: yq если доступен, иначе sed с ограничениями (не полный парсинг)
    if grep -q "^  $provider_id:" "$SETTINGS_FILE"; then
        echo "✓ Провайдер $provider_id уже в конфиге"
        return
    fi

    # Используем yq если доступен (надежный YAML парсер)
    if command -v yq &> /dev/null; then
        yq eval ".llm-pi-ai.providers.$provider_id = {
            name: \"$provider_name\",
            protocol: \"openai-compat\",
            baseUrl: \"$base_url\",
            apiKey: \"$api_key\",
            models: [{id: \"$model\", name: \"$model\"}]
        }" -i "$SETTINGS_FILE"
        echo "✓ Провайдер $provider_id добавлен (yq)"
    else
        # Fallback: простое добавление в конец (работает, но не идеально)
        cat >> "$SETTINGS_FILE" <<EOF
  $provider_id:
    name: "$provider_name"
    protocol: openai-compat
    baseUrl: "$base_url"
    apiKey: "$api_key"
    models:
      - id: "$model"
        name: "$model"
EOF
        echo "✓ Провайдер $provider_id добавлен (fallback sed)"
        echo "  ⚠ Совет: установите yq для надежного парсинга YAML"
        echo "    apt install yq  # Debian/Ubuntu"
        echo "    brew install yq # macOS"
    fi
}

# NVIDIA NeMo провайдер (если переменные заданы)
if [ -n "${NVIDIA_BASE_URL:-}" ] && [ -n "${NVIDIA_API_KEY:-}" ]; then
    add_openai_provider \
        "nvidia-nemo" \
        "NVIDIA NeMo" \
        "$NVIDIA_BASE_URL" \
        "$NVIDIA_API_KEY" \
        "${NVIDIA_MODEL:-nemotron-ultra-550b}"

    echo "📍 NVIDIA NeMo провайдер готов к использованию"
    echo "   Переменные: NVIDIA_BASE_URL, NVIDIA_API_KEY, NVIDIA_MODEL"
fi

# Другие провайдеры (расширяемо)
if [ -n "${CUSTOM_PROVIDER_ID:-}" ] && [ -n "${CUSTOM_BASE_URL:-}" ] && [ -n "${CUSTOM_API_KEY:-}" ]; then
    add_openai_provider \
        "$CUSTOM_PROVIDER_ID" \
        "${CUSTOM_PROVIDER_NAME:-Custom}" \
        "$CUSTOM_BASE_URL" \
        "$CUSTOM_API_KEY" \
        "${CUSTOM_MODEL:-gpt-4}"
fi

echo "✓ Провайдеры сконфигурированы в $SETTINGS_FILE"
echo "  Проверка: dsh --profile $PROFILE --dump-config | grep llm-pi-ai"
