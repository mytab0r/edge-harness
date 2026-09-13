#!/usr/bin/env python3
"""Нейтрализует self-регистрацию провайдера в плагине dsh-anthropic-oauth-pool.

Класс #1097 (второй заход, #1130, живой прогон worker.yml 34753001158):
плагин регистрирует себя в llm-pi-ai через `ctx.get('settings').update(...)`
внутри `ensureProvider()` (lib/index.js). Первый заход #1097/#1099 добавил
СТАТИЧЕСКУЮ регистрацию в наш cordis.patch.yml (scripts/lib/dsh-ci.sh ::
_dsh_patch_profile_anthropic_pool) — это устранило `NO_ADAPTER`, но не убрало
саму self-регистрацию плагина: она продолжает работать АСИНХРОННО в той же
композиции и, когда `ctx.get('settings')` НА САМОМ ДЕЛЕ доступен (сервис
`@deepseek-ai/dsh-settings-file` присутствует в headless — прежнее
утверждение обратного было ошибочным, см. `docs/research/32`), гонка между
`discoverModels()` (реальный сетевой запрос `/v1/models` с реальными
аккаунтами) и первым запросом агента решает, ЧЕЙ список моделей окажется
в силе: `mergeLayers` (dsh-settings) заменяет массивы ЦЕЛИКОМ, не поэлементно
— более поздняя запись (плагина) вытесняет нашу статическую, и если реальный
дискавери не содержит буквального id "claude-sonnet-4-5", результат —
`UNKNOWN_MODEL`.

Фикс — нейтрализовать САМУ self-регистрацию: наша статическая регистрация
остаётся ЕДИНСТВЕННЫМ источником правды, плагину нечего с ней перегонять.
`discoverModels()`/`credentials.set(...)` тоже становятся не нужны — их
единственный потребитель (`provider` объект, идущий в `settings.update`)
удаляется вместе с самим вызовом.

Патч — точная строковая замена (exact match), не regex/sed по шаблону: если
апстрим сменил форму `ensureProvider()`, замена НЕ применится и скрипт
откажет громко (return 1) — тихое несовпадение хуже падения (AGENTS.md,
«Fail loud»).

Использование: patch_anthropic_pool_plugin.py <путь к lib/index.js>
"""
from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import sys

OLD = """  async function ensureProvider() {
    await discoverModels()
    try { await ctx.get('credentials').set(CREDS_REF, 'managed-by-anthropic-pool') } catch {}
    const provider = { displayName: 'Anthropic OAuth Pool', apiKeyEnv: CREDS_REF, api: 'anthropic-messages', baseURL: `http://127.0.0.1:${port}`, models }
    const settings = ctx.get('settings')
    if (typeof settings.update === 'function') await settings.update('llm-pi-ai', { providers: { [PROVIDER_KEY]: provider } })
    else if (typeof settings.mutate === 'function') await settings.mutate('llm-pi-ai', [{ op: 'add', path: ['providers', PROVIDER_KEY], value: provider }])
    else throw new Error('DSH settings service cannot install the Anthropic pool provider')
  }"""

NEW = """  async function ensureProvider() {
    // #1097/#1130: self-регистрация через ctx.get('settings') отключена —
    // гонка с нашей статической регистрацией
    // llm-pi-ai.providers.anthropic-pool (scripts/lib/dsh-ci.sh ::
    // _dsh_patch_profile_anthropic_pool) перезаписывала models ЖИВЫМ
    // каталогом discoverModels() и роняла запрошенную модель в
    // UNKNOWN_MODEL. Регистрация теперь исключительно статическая — см.
    // docs/research/32-claude-oauth-provider.md, «Дополнение».
    return
  }"""


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: patch_anthropic_pool_plugin.py <lib/index.js>", file=sys.stderr)
        return 2
    path = sys.argv[1]
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if OLD not in content:
        print(
            "PATCH_MARKER_NOT_FOUND: форма ensureProvider() в "
            f"{path} не совпала с ожидаемой — апстрим плагина изменился, "
            "патч #1097/#1130 не применён (fail loud, не тихое несовпадение)",
            file=sys.stderr,
        )
        return 1
    content = content.replace(OLD, NEW, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"PATCHED_OK: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
