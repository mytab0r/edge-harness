#!/usr/bin/env python3
"""Засев реестра провайдеров морды (namespace `llm-pi-ai`) из манифеста
использования — задача #1330.

Зачем. Кнопки «Add»/«Add custom provider» в Settings → Models ожили ещё в #378
(серверный плагин `provider-registry` монтирует namespace и публикует
directory-записи), но реестр так и остался ПУСТЫМ: восемь реальных маршрутов
конвейера живут в `config/provider-usage.json` и в морду не попадают. Владелец
видит в пикере один штатный `deepseek-official` и не может ни свериться с
живым списком моделей провайдера, ни собрать комбо — нечего собирать.

Направление строго ОДНОСТОРОННЕЕ: манифест → морда. Это ПРОЕКЦИЯ, а не второе
место правды. Обратный путь (морда → раннер, замена vars.DSH_PROVIDER_CHAIN) —
задача #735, и она заблокирована дефектом схемы: приоритет МЕЖДУ провайдерами
реестр выразить не может вовсе (`providers` — z.dict, порядка нет; #1329).
Поэтому засев переносит НАБОР и МОДЕЛИ (их порядок внутри провайдера сохраняется,
`models` — массив), но НЕ приоритет. Это названо вслух и здесь, и в выводе.

Транспорт — тот же RPC, которым пишет сам UI, без единого нового серверного
роута (контракт перепроверен по `dsh-edge/registry-integration/check.mjs`):
  POST /api/auth/login      accessKey=...            → кука владельца
  POST /api/settings.mutate {ns, ops:[{op,path,value}]}
  POST /api/credentials.set {ref, value}
  POST /api/llm.providers   {}                       → сверка
Маршрут `deepseek-official` зарезервирован плагином (запись отказывается) — не
трогаем его вовсе, а не ловим отказ постфактум.

Секреты. Значение ключа не печатается НИКОГДА, ни в ошибке, ни в diff'е: наружу
идёт только ИМЯ credential-ref. Репозиторий публичный, и маскирование GitHub
ловит лишь точное совпадение (AGENTS.md, «Секреты»).
"""
# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

MANIFEST_DEFAULT = Path(__file__).resolve().parent.parent.parent / "config" / "provider-usage.json"
NAMESPACE = "llm-pi-ai"
PROTOCOL = "openai-completions"
# Зарезервирован плагином морды: запись в него отказывается с «занят штатным».
RESERVED_ROUTES = frozenset({"deepseek-official"})
# Клиентский ROUTE_PATTERN плагина (plugins-src/provider-registry/server/index.js:90).
ROUTE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
DEFAULT_CONTEXT_WINDOW = 131072


class SeedError(RuntimeError):
    """Отказ, который обязан быть громким и назвать причину."""


def route_id(display_name: str) -> str:
    """Имя провайдера манифеста → route-id, пригодный и ключом настроек, и
    основой имени креда. Схема плагина требует нижний регистр и дефисы."""
    slug = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")
    if not ROUTE_PATTERN.match(slug):
        raise SeedError(
            f"имя провайдера '{display_name}' не сводится к валидному route-id "
            f"(получилось '{slug}', требуется {ROUTE_PATTERN.pattern}) — "
            "переименуй запись в манифесте")
    return slug


def entry_models(entry: dict) -> list[dict]:
    """Модели записи манифеста в форме `modelProfile` плагина. Поддержаны обе
    формы манифеста: legacy `model` (одна) и `models` (список строк или
    объектов) — то же разведение, что у dsh_entry_model_candidates в
    scripts/lib/dsh-ci.sh, одно понимание формата на две реализации."""
    raw = entry.get("models")
    if raw is None:
        single = entry.get("model")
        raw = [single] if single else []
    ceiling = int(entry.get("max_output_tokens") or DEFAULT_CONTEXT_WINDOW)
    out: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            model_id, max_tokens = item, ceiling
        else:
            model_id = item.get("id")
            max_tokens = int(item.get("max_output_tokens") or ceiling)
        if not model_id:
            raise SeedError(f"у провайдера '{entry.get('name')}' запись модели без id")
        out.append({
            "id": model_id,
            "name": model_id,
            # contextWindow плагин требует >=1; манифест его не несёт, поэтому
            # берём тот же консервативный потолок, что и maxTokens — занижение
            # безопасно (меньше окно), завышение соврало бы модели.
            "contextWindow": max_tokens,
            "maxTokens": max_tokens,
        })
    if not out:
        raise SeedError(f"у провайдера '{entry.get('name')}' нет ни одной модели")
    return out


def manifest_profiles(manifest: dict, chain_name: str | None = None) -> dict[str, dict]:
    """Манифест → {route_id: профиль llm-pi-ai}. Порядок цепочки НЕ переносится
    (#1329): в реестре его выразить нечем."""
    chains = manifest.get("chains") or {}
    if chain_name is None:
        if len(chains) != 1:
            raise SeedError(
                f"в манифесте {len(chains)} цепочек ({', '.join(sorted(chains))}) — "
                "какую засевать, скажи явно через --chain")
        chain_name = next(iter(chains))
    chain = chains.get(chain_name)
    if not chain:
        raise SeedError(f"цепочки '{chain_name}' нет в манифесте")

    profiles: dict[str, dict] = {}
    for entry in chain:
        name = entry.get("name")
        if not name:
            raise SeedError("запись цепочки без поля name")
        route = route_id(name)
        if route in RESERVED_ROUTES:
            print(f"  ⏭️  {name}: маршрут '{route}' зарезервирован мордой — пропускаю")
            continue
        base_url = entry.get("base_url")
        if not base_url or not re.match(r"^https?://\S+$", base_url):
            raise SeedError(f"у провайдера '{name}' нет корректного base_url")
        secret_env = entry.get("secret_env")
        if not secret_env:
            raise SeedError(f"у провайдера '{name}' нет secret_env — имя credential-ref обязательно")
        profiles[route] = {
            "displayName": name,
            "api": PROTOCOL,
            "baseURL": base_url,
            "apiKeyEnv": secret_env,
            "models": entry_models(entry),
        }
    return profiles


class MordaRpc:
    """RPC-конверт морды: POST /api/<method>. Кука владельца берётся тем же
    обменом access-ключа, что уже делает scripts/lib/dsh-edge-session.sh."""

    def __init__(self, origin: str, access_key: str, timeout: int = 30) -> None:
        self.origin = origin.rstrip("/")
        self.access_key = access_key
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def login(self) -> None:
        body = urllib.parse.urlencode({"accessKey": self.access_key}).encode()
        req = urllib.request.Request(
            f"{self.origin}/api/auth/login", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            # 303 — штатный ответ логина; opener сам идёт по редиректу.
            self.opener.open(req, timeout=self.timeout).read()
        except urllib.error.HTTPError as error:
            raise SeedError(
                f"логин в морду отказан (HTTP {error.code}) — проверь "
                "DSH_EDGE_ACCESS_KEY; значение ключа здесь намеренно не печатается") from error
        except OSError as error:
            raise SeedError(f"морда недоступна по {self.origin}: {error}") from error

    def call(self, method: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.origin}/api/{method}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            raw = self.opener.open(req, timeout=self.timeout).read().decode()
        except urllib.error.HTTPError as error:
            raise SeedError(f"RPC {method}: HTTP {error.code} {error.read().decode()[:300]}") from error
        except OSError as error:
            raise SeedError(f"RPC {method}: сеть — {error}") from error
        envelope = json.loads(raw or "{}")
        result = envelope.get("result") or {}
        if not result.get("ok"):
            message = (result.get("error") or {}).get("message") or raw[:300]
            raise SeedError(f"RPC {method} отказал: {message}")
        return result.get("value") or {}


def plan(current: dict, desired: dict[str, dict]) -> list[tuple[str, dict]]:
    """Что реально надо записать. Совпадающие профили не трогаем — от этого
    зависит идемпотентность (повторный прогон обязан быть no-op)."""
    return [(route, profile) for route, profile in sorted(desired.items())
            if current.get(route) != profile]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Засев реестра морды из манифеста (#1330)")
    parser.add_argument("--manifest", default=str(MANIFEST_DEFAULT))
    parser.add_argument("--chain", default=None, help="имя цепочки манифеста")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать план, не трогая морду и не требуя access-ключа")
    args = parser.parse_args(argv[1:])

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    desired = manifest_profiles(manifest, args.chain)
    print(f"Манифест: {len(desired)} маршрутов → {', '.join(sorted(desired))}")
    print("⚠️  Приоритет между провайдерами НЕ переносится — в схеме реестра "
          "его выразить нечем (#1329). Переносятся набор и модели.")

    if args.dry_run:
        for route, profile in sorted(desired.items()):
            models = ", ".join(m["id"] for m in profile["models"])
            print(f"  {route:<16} {profile['baseURL']:<46} ключ={profile['apiKeyEnv']}")
            print(f"  {'':<16} модели: {models}")
        return 0

    origin = os.environ.get("DSH_EDGE_URL", "").strip()
    access_key = os.environ.get("DSH_EDGE_ACCESS_KEY", "").strip()
    if not origin or not access_key:
        print("::error::DSH_EDGE_URL/DSH_EDGE_ACCESS_KEY не заданы — засев реестра "
              "невозможен. Это настройка окружения (vars.DSH_EDGE_URL / "
              "secrets.DSH_EDGE_ACCESS_KEY), а не сбой сети. Для проверки формы "
              "без морды используй --dry-run.", file=sys.stderr)
        return 2

    rpc = MordaRpc(origin, access_key)
    rpc.login()

    described = rpc.call("settings.describe", {})
    namespaces = {view.get("ns") for view in described.get("namespaces") or []}
    if NAMESPACE not in namespaces:
        raise SeedError(
            f"namespace '{NAMESPACE}' не смонтирован мордой (видны: "
            f"{', '.join(sorted(n for n in namespaces if n))}) — плагин "
            "provider-registry не установлен или не стартовал; засевать некуда")

    settings = described.get("settings") or {}
    current = ((settings.get(NAMESPACE) or {}).get("providers")) or {}
    todo = plan(current, desired)
    if not todo:
        print(f"✅ Реестр уже совпадает с манифестом ({len(desired)} маршрутов) — "
              "ничего не меняю (идемпотентно)")
    for route, profile in todo:
        rpc.call("settings.mutate", {
            "ns": NAMESPACE,
            "ops": [{"op": "set", "path": ["providers", route], "value": profile}],
        })
        key_value = os.environ.get(profile["apiKeyEnv"], "")
        if key_value:
            rpc.call("credentials.set", {"ref": profile["apiKeyEnv"], "value": key_value})
            key_note = f"ключ {profile['apiKeyEnv']} положен"
        else:
            # Не тихий пропуск: маршрут появится, но неактивным — говорим прямо.
            key_note = (f"::warning::{profile['apiKeyEnv']} нет в окружении — "
                        f"маршрут {route} записан БЕЗ ключа и останется неактивным")
        print(f"  ✍️  {route}: профиль записан ({len(profile['models'])} моделей); {key_note}")

    rows = rpc.call("llm.providers", {}).get("providers") or []
    active = {row.get("provider") for row in rows if row.get("active")}
    missing = sorted(set(desired) - {row.get("provider") for row in rows})
    if missing:
        raise SeedError(f"после записи в directory морды нет маршрутов: {', '.join(missing)}")
    inactive = sorted(set(desired) - active)
    print(f"Итог: в морде {len(desired)} маршрутов манифеста, активны "
          f"{len(desired) - len(inactive)}"
          + (f", без ключа {', '.join(inactive)}" if inactive else ""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SeedError as failure:
        print(f"::error::засев реестра: {failure}", file=sys.stderr)
        sys.exit(1)
