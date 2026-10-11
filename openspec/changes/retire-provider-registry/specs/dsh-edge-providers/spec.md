## Delta: namespace `llm-pi-ai` обслуживает апстрим, плагин реестра снят

### REMOVED Requirements

- **Requirement: собственный код морды обслуживает namespace `llm-pi-ai`** —
  с пина dsh-edge-v0.26.0 namespace монтирует апстрим
  (`mountPiAiProvider`, `@deepseek-ai/dsh-llm-pi-ai`); собственная реализация
  плагина `provider-registry` снята как затычка с закрывшимся окном (#1640).
- **Requirement: directory-записи готовых маршрутов (`zhipu`, `nvidia-nim`,
  `openrouter`, `deepseek`) видны кнопке «Add»** — апстрим directory не
  публикует; регресс принят явно (см. proposal.md), засев прод-маршрутов —
  `seed-provider-registry.yml` (#1330).

### MODIFIED Requirements

- **Requirement: union-протокол схемы namespace** — вместо единственного
  `openai-completions` апстримная схема несёт три значения:
  `openai-completions`, `openai-responses`, `anthropic-messages`.
  Проверка: `dsh-edge/registry-integration/check.mjs` (шаг деплоя).
