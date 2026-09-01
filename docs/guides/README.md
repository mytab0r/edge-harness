# Руководства

Практические инструкции для работы с edge-harness.

## Провайдеры LLM

- [Добавление собственных LLM провайдеров](add-custom-llm-providers.md) — NVIDIA NeMo, Anthropic Claude и другие OpenAI-compatible эндпоинты

Быстрый старт для NVIDIA NeMo:
```bash
export NVIDIA_BASE_URL="https://api.nvidianemo.com/v1"
export NVIDIA_API_KEY="nvapi-xxx"
export NVIDIA_MODEL="nemotron-ultra-550b"

source scripts/dsh-web-setup-providers.sh
dsh web --trusted-host <dsh-web.tailnet>
```

Провайдер автоматом добавится в Settings → Models и будет доступен для выбора в диалоге.
