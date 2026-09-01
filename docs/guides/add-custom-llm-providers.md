# Добавление собственных LLM провайдеров в dsh-web

Если у вас есть доступ к альтернативным моделям (NVIDIA NeMo, Anthropic, OpenAI и т.д.) и вы хотите использовать их в веб-интерфейсе DSH вместо DeepSeek GLM, вы можете добавить провайдера через конфигурацию.

## Быстрый старт: NVIDIA NeMo Ultra 550B

```bash
# 1. Задайте переменные окружения перед запуском web
export NVIDIA_BASE_URL="https://your-nvidia-endpoint/v1"
export NVIDIA_API_KEY="your-api-key"
export NVIDIA_MODEL="nemotron-ultra-550b"

# 2. Запустите скрипт конфигурирования
source scripts/dsh-web-setup-providers.sh

# 3. Запустите dsh web как обычно (провайдер автоматом добавится в Settings)
dsh web --trusted-host <dsh-web.tailnet>
```

## Как это работает

1. **Скрипт** `scripts/dsh-web-setup-providers.sh` создаёт конфиг в `~/.dsh/profiles/web/settings.yaml`
2. **DSH** автоматом загружает этот конфиг при старте web-профиля
3. **Провайдер** появляется в Settings → Models → пикере провайдеров
4. **Модель** становится доступна для выбора в диалоге

## Поддерживаемые провайдеры

Любой OpenAI-compatible эндпоинт. Примеры:

### NVIDIA NeMo (базовое использование)
```bash
export NVIDIA_BASE_URL="https://api.nvidianemo.com/v1"
export NVIDIA_API_KEY="nvapi-xxx"
export NVIDIA_MODEL="nemotron-ultra-550b"
source scripts/dsh-web-setup-providers.sh
```

### Anthropic Claude
```bash
export NVIDIA_BASE_URL="https://api.anthropic.com"  # если используют OpenAI-compat API
export NVIDIA_API_KEY="sk-ant-xxx"
export NVIDIA_MODEL="claude-3-5-sonnet"
source scripts/dsh-web-setup-providers.sh
```

### Кастомный провайдер
```bash
export CUSTOM_PROVIDER_ID="my-provider"
export CUSTOM_PROVIDER_NAME="My LLM"
export CUSTOM_BASE_URL="https://my-llm-api.com/v1"
export CUSTOM_API_KEY="key-xxx"
export CUSTOM_MODEL="my-model-v1"
source scripts/dsh-web-setup-providers.sh
```

## Механика: как работает llm-pi-ai

DSH использует плагин `llm-pi-ai` — универсальный пул провайдеров. Конфиг хранится в:

```
~/.dsh/profiles/<профиль>/settings.yaml
```

Структура для NVIDIA:
```yaml
llm-pi-ai:
  providers:
    nvidia-nemo:
      name: "NVIDIA NeMo"
      protocol: openai-compat
      baseUrl: "https://api.nvidianemo.com/v1"
      apiKey: "nvapi-xxx"
      models:
        - id: "nemotron-ultra-550b"
          name: "NeMo Ultra 550B"
```

Скрипт автоматом добавляет эту конфигурацию, вам не нужно редактировать YAML вручную.

## Паритет с раннером

После добавления провайдера в веб:

1. Вы можете выбрать его как дефолтную модель через Settings → Models
2. Используйте GET `/api/config` (морда) для получения конфига
3. Раннер подхватит эту модель через `PUT /api/config` (см. [ADR 0009](../decisions/0009-unified-model-config.md))

Это означает: выбрали NVIDIA в веб-UI → раннер будет использовать её в следующем запуске. Паритет работает для любого провайдера, не только DeepSeek.

## Проверка

```bash
# Убедитесь, что провайдер добавлен
dsh --profile web --dump-config | grep -A 20 llm-pi-ai

# Результат должен содержать ваш провайдер
# llm-pi-ai:
#   providers:
#     nvidia-nemo:
#       ...
```

## Ограничения и альтернативы

- **Кнопки добавления провайдеров в UI**: в текущей версии rc.2 могут быть задизейблены или не работать как ожидается. Используйте скрипт как обход.
- **Постоянное хранение**: конфиг сохраняется в кэше GH Actions при сохранении `~/.dsh`, поэтому переживает перезагрузку сессии.
- **Переменные окружения**: если нужно перезаписать конфиг, просто запустите скрипт заново с другими переменными.

## Развитие

Возможные улучшения (см. [ADR 0009 todos](../decisions/0009-unified-model-config.md)):
- [ ] Разблокировать кнопки в UI для добавления провайдеров напрямую (без скрипта)
- [ ] Интегрировать скрипт в workflow `web-desktop.yml` по умолчанию
- [ ] Web-UI Settings → "Add custom provider" форма
