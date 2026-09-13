#!/usr/bin/env python3
"""Патчит распакованный плагин dsh-anthropic-oauth-pool: три точечных правки.

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
`UNKNOWN_MODEL`. Патч 1 (PATCH_ENSURE_PROVIDER) нейтрализует эту
self-регистрацию целиком — наша статическая регистрация остаётся
ЕДИНСТВЕННЫМ источником правды.

Второй, независимый дефект (решение владельца, #1130 доработка): плагин
трактует ОТСУТСТВИЕ `oauth.expiresAt` как «токен истёк» — `lib/pool.js`,
`createRefreshCoordinator`, условие пропуска рефреша `if (oauth.expiresAt &&
oauth.expiresAt - Date.now() > REFRESH_SKEW_MS) return account` ложно и при
отсутствующем, и при просроченном поле — то есть путает «срок неизвестен»
(долгоживущий accessToken без явного expiresAt) с «срок истёк». Раз это
проверяется на КАЖДЫЙ запрос (`lib/index.js`: `forward()` и
`discoverModels()`), долгоживущий токен без `expiresAt` рефрешится
превентивно на каждом вызове.

Патч 2 (PATCH_POOL_SKIP_CONDITION, lib/pool.js) переворачивает эту логику:
отсутствие `expiresAt` теперь означает «валиден, превентивный рефреш не
нужен» — рефреш по-прежнему происходит, когда `expiresAt` ЯВНО задан и
близок/в прошлом (короткоживущие токены ведут себя как раньше — правится
ТОЛЬКО ветка «поля нет»).

Патч 3 (PATCH_REACTIVE_REFRESH, lib/index.js) — обязательная пара к патчу 2:
без него РЕАЛЬНО истёкший (но без `expiresAt`, либо с ним) токен ловил бы
401/403 и просто уходил в cooldown на 60с без попытки восстановиться —
плагин и без него не рефрешит реактивно на 401/403, а патч 2 убирает
единственный путь превентивного рефреша для токенов без `expiresAt`. Патч 3
на 401/403 помечает аккаунт на диске как просроченный (`expiresAt` в
прошлом) и зовёт `ensureFresh()` ЕЩЁ РАЗ — тот увидит `expiresAt` в прошлом
и рефрешит по СВОЕЙ же логике (тот же `refreshToken()`/`writeAccount()`, тот
же single-flight `pending` из `pool.js`), без дублирования кода рефреша в
этом патче. Один повтор того же запроса тем же аккаунтом; неудача (сеть,
битый `refreshToken`, повторный 401/403) — прежнее поведение: cooldown 60с,
следующий аккаунт.

Патчи — точная строковая замена (exact match), не regex/sed по шаблону: если
апстрим сменил форму любого куска, замена НЕ применится и скрипт откажет
громко (return 1) — тихое несовпадение хуже падения (AGENTS.md, «Fail
loud»). Проверка ВСЕХ патчей идёт ДО записи любого файла (атомарно на
уровне вызова) — частичное применение (например патч 1 прошёл, патч 2 нет)
не оставляет плагин в наполовину пропатченном состоянии.

Использование: patch_anthropic_pool_plugin.py <распакованный каталог package/>
(ожидает package/lib/index.js и package/lib/pool.js).
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

PATCH_ENSURE_PROVIDER_OLD = """  async function ensureProvider() {
    await discoverModels()
    try { await ctx.get('credentials').set(CREDS_REF, 'managed-by-anthropic-pool') } catch {}
    const provider = { displayName: 'Anthropic OAuth Pool', apiKeyEnv: CREDS_REF, api: 'anthropic-messages', baseURL: `http://127.0.0.1:${port}`, models }
    const settings = ctx.get('settings')
    if (typeof settings.update === 'function') await settings.update('llm-pi-ai', { providers: { [PROVIDER_KEY]: provider } })
    else if (typeof settings.mutate === 'function') await settings.mutate('llm-pi-ai', [{ op: 'add', path: ['providers', PROVIDER_KEY], value: provider }])
    else throw new Error('DSH settings service cannot install the Anthropic pool provider')
  }"""

PATCH_ENSURE_PROVIDER_NEW = """  async function ensureProvider() {
    // #1097/#1130: self-регистрация через ctx.get('settings') отключена —
    // гонка с нашей статической регистрацией
    // llm-pi-ai.providers.anthropic-pool (scripts/lib/dsh-ci.sh ::
    // _dsh_patch_profile_anthropic_pool) перезаписывала models ЖИВЫМ
    // каталогом discoverModels() и роняла запрошенную модель в
    // UNKNOWN_MODEL. Регистрация теперь исключительно статическая — см.
    // docs/research/32-claude-oauth-provider.md, «Поправка 2026-09-13».
    return
  }"""

PATCH_REACTIVE_REFRESH_OLD = """        if ([401, 403].includes(response.status)) {
          account.cooldownUntil = Date.now() + 60_000; lastResponse = response; lastResponseBody = Buffer.from(await response.arrayBuffer()); continue
        }"""

PATCH_REACTIVE_REFRESH_NEW = """        if ([401, 403].includes(response.status)) {
          // #1130 (доработка, решение владельца): pool.js больше не рефрешит
          // превентивно, когда oauth.expiresAt отсутствует (см. патч
          // PATCH_POOL_SKIP_CONDITION) — реактивный рефреш здесь и есть
          // единственный путь восстановить РЕАЛЬНО протухший accessToken.
          // Помечаем запись на диске как заведомо просроченную (expiresAt в
          // прошлом) и зовём ensureFresh ЕЩЁ РАЗ — тот увидит expiresAt в
          // прошлом и рефрешит по своей же логике (тот же refreshToken()/
          // writeAccount(), тот же single-flight pending), без дублирования
          // кода рефреша здесь. Один повтор ТОГО ЖЕ запроса тем же
          // аккаунтом; неудача — прежнее поведение: cooldown 60с, next.
          let recovered = false
          try {
            await writeAccount(account.id, { id: account.id, oauth: { ...stored.oauth, expiresAt: Date.now() - 1 } })
            const refreshedStored = await ensureFresh(account.id)
            const retryResponse = await fetch(new URL(req.url || '/', API_BASE), {
              method: req.method, headers: oauthHeaders(refreshedStored.oauth.accessToken, req.headers),
              body: ['GET', 'HEAD'].includes(req.method) ? undefined : body,
              redirect: 'manual', signal: AbortSignal.timeout(10 * 60 * 1000),
            })
            updateQuotaFromHeaders(account, retryResponse.headers, retryResponse.status)
            account.lastStatus = retryResponse.status
            if (![401, 403].includes(retryResponse.status)) {
              res.statusCode = retryResponse.status
              for (const [key, value] of retryResponse.headers) {
                if (!['content-encoding', 'content-length', 'transfer-encoding', 'connection'].includes(key.toLowerCase())) res.setHeader(key, value)
              }
              res.setHeader('x-dsh-anthropic-account', account.id)
              if (retryResponse.body) Readable.fromWeb(retryResponse.body).pipe(res); else res.end()
              return
            }
            lastResponse = retryResponse; lastResponseBody = Buffer.from(await retryResponse.arrayBuffer())
            recovered = true
          } catch {}
          if (!recovered) { lastResponse = response; lastResponseBody = Buffer.from(await response.arrayBuffer()) }
          account.cooldownUntil = Date.now() + 60_000; continue
        }"""

PATCH_POOL_SKIP_CONDITION_OLD = "      if (oauth.expiresAt && oauth.expiresAt - Date.now() > REFRESH_SKEW_MS) return account"

PATCH_POOL_SKIP_CONDITION_NEW = """      // #1130 (доработка, решение владельца): отсутствующий expiresAt
      // раньше означал "срок истёк" (условие ложно и без поля, и с полем
      // в прошлом) — путал "срок неизвестен" (долгоживущий accessToken без
      // явного expiresAt) с "срок истёк", и рефреш шёл превентивно на
      // КАЖДЫЙ запрос (lib/index.js: forward()/discoverModels()). Теперь
      // отсутствие поля само по себе НЕ триггерит рефреш — только явный
      // expiresAt, близкий/в прошлом. Короткоживущие токены (expiresAt
      // задан) ведут себя как раньше — правится только ветка "поля нет".
      // Реактивное восстановление на реальный 401/403 — PATCH_REACTIVE_REFRESH
      // (lib/index.js), эта ветка обязана применяться вместе с той.
      if (!oauth.expiresAt || oauth.expiresAt - Date.now() > REFRESH_SKEW_MS) return account"""


class PatchMarkerNotFound(RuntimeError):
    """Отказ конкретного патча — сообщение уже готово к печати человеку."""


def _replace_required(content: str, old: str, new: str, label: str, path: Path) -> str:
    """Заменить OLD на NEW внутри уже прочитанного содержимого. Не находит OLD
    — громкий отказ с указанием, какой именно патч и какой файл не совпали."""
    if old not in content:
        raise PatchMarkerNotFound(
            f"PATCH_MARKER_NOT_FOUND[{label}]: форма в {path} не совпала с "
            "ожидаемой — апстрим плагина изменился, патч #1097/#1130 не "
            "применён (fail loud, не тихое несовпадение)"
        )
    return content.replace(old, new, 1)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: patch_anthropic_pool_plugin.py <распакованный каталог package/>", file=sys.stderr)
        return 2
    package_dir = Path(sys.argv[1])
    index_js = package_dir / "lib" / "index.js"
    pool_js = package_dir / "lib" / "pool.js"
    for required in (index_js, pool_js):
        if not required.is_file():
            print(f"ОШИБКА: {required} не найден — не тот каталог/форма ассета изменилась", file=sys.stderr)
            return 1

    # Все три патча проверяются на исходном содержимом ДО первой записи —
    # частичное применение (например патч 1 прошёл, патч 2 нет) не оставляет
    # плагин в наполовину пропатченном состоянии.
    try:
        index_js_content = index_js.read_text(encoding="utf-8")
        index_js_content = _replace_required(
            index_js_content, PATCH_ENSURE_PROVIDER_OLD, PATCH_ENSURE_PROVIDER_NEW,
            "ensure_provider", index_js)
        index_js_content = _replace_required(
            index_js_content, PATCH_REACTIVE_REFRESH_OLD, PATCH_REACTIVE_REFRESH_NEW,
            "reactive_refresh", index_js)
        pool_js_content = pool_js.read_text(encoding="utf-8")
        pool_js_content = _replace_required(
            pool_js_content, PATCH_POOL_SKIP_CONDITION_OLD, PATCH_POOL_SKIP_CONDITION_NEW,
            "pool_skip_condition", pool_js)
    except PatchMarkerNotFound as exc:
        print(str(exc), file=sys.stderr)
        return 1

    index_js.write_text(index_js_content, encoding="utf-8")
    pool_js.write_text(pool_js_content, encoding="utf-8")
    print(f"PATCHED_OK: {index_js}")
    print(f"PATCHED_OK: {pool_js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
