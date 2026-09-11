#!/usr/bin/env python3
"""Экспорт Anthropic OAuth-учётки Claude Code в секрет GitHub для dsh-anthropic-oauth-pool.

Читает credentials-файл Claude Code (по умолчанию ~/.claude/.credentials.json), берёт из
него объект `claudeAiOauth` и кладёт его в секрет `ANTHROPIC_OAUTH_<slot>` в формате, который
плагин `dsh-anthropic-pool add` читает напрямую: `{"claudeAiOauth": {...}}` (см. плагин
accounts.js: обязательны `accessToken` и `refreshToken`, иначе «Source has no usable
claudeAiOauth credentials»).

Инварианты (тот же приём, что provider_secrets_import.py):
  - значения токенов НИКОГДА не печатаются и не проходят через argv — gh secret set
    читает значение из stdin, когда --body/--body-file не заданы; флаг --body-file
    в gh 2.85 отсутствует (unknown flag, #786) — не передаём его вовсе;
  - в отчёте только несекретные признаки: subscriptionType, срок, наличие полей (bool);
  - дефолт — сухой прогон; запись секрета только по --apply;
  - fail loud: нет claudeAiOauth / нет accessToken|refreshToken — падаем с внятным текстом,
    а не пишем битый секрет (плагин потом молча не смонтирует аккаунт).

  python3 scripts/lib/anthropic_oauth_import.py --slot 1 --apply
  python3 scripts/lib/anthropic_oauth_import.py --slot 2 --source /путь/credentials.json --apply
"""
from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import argparse
import json
import os
import subprocess
import sys

REPO_DEFAULT = "mytab0r/edge-harness"
SECRET_PREFIX = "ANTHROPIC_OAUTH_"
# Плагин требует минимум эти два поля (dsh-anthropic-oauth-pool accounts.js).
REQUIRED_OAUTH_FIELDS = ("accessToken", "refreshToken")


class LoudError(RuntimeError):
    """Осознанный отказ с текстом для человека — не молчаливый битый секрет."""


def default_source() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def load_oauth(source: Path) -> dict:
    """Достаёт объект claudeAiOauth из credentials-файла Claude Code. Fail loud."""
    if not source.exists():
        raise LoudError(
            f"источник не найден: {source}. Укажи путь к credentials.json второго "
            f"аккаунта через --source (плагину нужен файл вида "
            f'{{"claudeAiOauth": {{"accessToken": ..., "refreshToken": ...}}}}).'
        )
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise LoudError(f"не читается/не парсится {source}: {exc}") from exc

    oauth = data.get("claudeAiOauth") or data.get("oauth")
    if not isinstance(oauth, dict):
        raise LoudError(
            f"в {source} нет объекта claudeAiOauth (ключи верхнего уровня: "
            f"{sorted(data.keys())}). Это не credentials-файл Claude Code."
        )
    missing = [f for f in REQUIRED_OAUTH_FIELDS if not oauth.get(f)]
    if missing:
        raise LoudError(
            f"claudeAiOauth в {source} без обязательных полей: {missing}. "
            f"Плагин отклонит такую учётку («no usable claudeAiOauth credentials»)."
        )
    return oauth


def krouter_conn_to_oauth(conn: dict) -> dict:
    """Аккаунт claude из krouter (providerConnections) → формат плагина claudeAiOauth.

    krouter хранит OAuth-учётку как {accessToken, refreshToken, expiresAt, scope, ...};
    плагину нужен {accessToken, refreshToken, expiresAt, scopes[]}. accessToken из
    двухнедельного бэкапа обычно истёк — плагин рефрешит по refreshToken на использовании,
    поэтому обязателен именно refreshToken (его и проверяем ниже, как плагин).
    """
    oauth = {
        "accessToken": conn.get("accessToken", ""),
        "refreshToken": conn.get("refreshToken", ""),
    }
    if conn.get("expiresAt"):
        oauth["expiresAt"] = conn["expiresAt"]
    scope = conn.get("scope")
    if isinstance(scope, str) and scope:
        oauth["scopes"] = scope.split()
    # subscriptionType плагину не обязателен; проставляем нейтральное, если krouter не дал.
    oauth["subscriptionType"] = conn.get("subscriptionType", "pro")
    return oauth


def krouter_claude_accounts(backup: Path) -> list[dict]:
    """Живые OAuth-учётки Claude из krouter-бэкапа, по возрастанию priority (1 — первый).

    Фильтр: provider==claude, authType==oauth, isActive, не забанен, есть оба токена.
    Возвращает список dict'ов вида {"name":..., "oauth": {...}} — name несекретный (для отчёта).
    """
    if not backup.exists():
        raise LoudError(f"krouter-бэкап не найден: {backup}")
    try:
        data = json.loads(backup.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise LoudError(f"не читается/не парсится {backup}: {exc}") from exc

    conns = data.get("providerConnections")
    if not isinstance(conns, list):
        raise LoudError(
            f"в {backup} нет providerConnections — это не krouter-бэкап "
            f"(ключи верхнего уровня: {sorted(data.keys())[:10]})."
        )
    out: list[dict] = []
    for conn in conns:
        if not isinstance(conn, dict):
            continue
        if conn.get("provider") != "claude" or conn.get("authType") != "oauth":
            continue
        if not conn.get("isActive") or conn.get("isPermanentlyBanned"):
            continue
        oauth = krouter_conn_to_oauth(conn)
        if not oauth["accessToken"] or not oauth["refreshToken"]:
            continue
        out.append({"name": conn.get("name") or "?", "oauth": oauth,
                    "priority": conn.get("priority", 999)})
    out.sort(key=lambda a: a["priority"])
    if not out:
        raise LoudError(
            f"в {backup} нет живых OAuth-учёток Claude (provider=claude, authType=oauth, "
            f"isActive, с обоими токенами)."
        )
    return out


def build_secret_payload(oauth: dict) -> str:
    """Ровно то, что читает `dsh-anthropic-pool add`: {"claudeAiOauth": {...}}.

    Заворачиваем ТОЛЬКО claudeAiOauth — лишнее (mcpOAuth и пр.) в секрет не тянем.
    """
    return json.dumps({"claudeAiOauth": oauth}, ensure_ascii=False)


def secret_name(slot: int) -> str:
    return f"{SECRET_PREFIX}{slot}"


def set_secret(repo: str, name: str, value: str) -> None:
    """Значение — ТОЛЬКО через stdin, никогда через argv/лог.

    gh secret set читает значение из stdin, когда не задан --body/--body-file. Флаг
    --body-file в gh 2.85 отсутствует (unknown flag) — поэтому не передаём его вовсе.
    """
    result = subprocess.run(
        ["gh", "secret", "set", name, "--repo", repo],
        input=value, text=True, capture_output=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise LoudError(f"gh secret set {name} упал: {result.stderr.strip()}")


def _safe_expiry(oauth: dict) -> str:
    """expiresAt (ms) в человекочитаемый вид. Не секрет — помогает узнать валидность."""
    raw = oauth.get("expiresAt")
    if not isinstance(raw, (int, float)):
        return "неизвестно"
    try:
        import datetime as _dt
        return _dt.datetime.utcfromtimestamp(raw / 1000).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        return f"raw={raw}"


def report(source: Path, oauth: dict, name: str, applied: bool) -> None:
    """Только несекретные признаки. accessToken/refreshToken НЕ печатаем никогда."""
    print(f"источник:        {source}")
    print(f"аккаунт:         subscriptionType={oauth.get('subscriptionType', '?')}, "
          f"tier={oauth.get('rateLimitTier', '?')}")
    print(f"scopes:          {oauth.get('scopes', [])}")
    print(f"accessToken:     {'есть' if oauth.get('accessToken') else 'НЕТ'} (значение не печатается)")
    print(f"refreshToken:    {'есть' if oauth.get('refreshToken') else 'НЕТ'} (значение не печатается)")
    print(f"access истекает: {_safe_expiry(oauth)}")
    print(f"секрет:          {name}")
    print(f"действие:        {'ЗАПИСАН' if applied else 'сухой прогон (для записи добавь --apply)'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-krouter", type=Path, default=None, metavar="BACKUP",
                        help="krouter-бэкап: экспортнуть ВСЕ OAuth-учётки Claude по слотам "
                             "1..N (по priority). Тот же источник, что API-ключи.")
    parser.add_argument("--slot", type=int, default=None,
                        help="номер аккаунта в пуле → секрет ANTHROPIC_OAUTH_<slot> (для одиночного --source)")
    parser.add_argument("--source", type=Path, default=None,
                        help="одиночный credentials.json (по умолчанию ~/.claude/.credentials.json)")
    parser.add_argument("--max-slots", type=int, default=2,
                        help="сколько учёток брать из krouter-бэкапа (по умолчанию %(default)s)")
    parser.add_argument("--repo", default=REPO_DEFAULT, help="owner/repo (по умолчанию %(default)s)")
    parser.add_argument("--apply", action="store_true", help="реально записать секрет (иначе сухой прогон)")
    args = parser.parse_args(argv)

    try:
        if args.from_krouter is not None:
            return _run_krouter(args)
        return _run_single(args)
    except LoudError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1


def _run_krouter(args) -> int:
    accounts = krouter_claude_accounts(args.from_krouter)[: args.max_slots]
    print(f"источник: {args.from_krouter} — найдено OAuth-учёток Claude: {len(accounts)} "
          f"(беру до {args.max_slots})")
    for slot, acc in enumerate(accounts, start=1):
        name = secret_name(slot)
        if args.apply:
            set_secret(args.repo, name, build_secret_payload(acc["oauth"]))
        print(f"  слот {slot}: {acc['name']} (priority {acc['priority']}) → {name} "
              f"[{'ЗАПИСАН' if args.apply else 'сухой прогон'}], "
              f"accessToken/refreshToken есть, значения не печатаются")
    if not args.apply:
        print("сухой прогон — для записи добавь --apply")
    return 0


def _run_single(args) -> int:
    if args.slot is None or args.slot < 1:
        print("для одиночного режима нужен --slot (положительное целое)", file=sys.stderr)
        return 2
    source = args.source or default_source()
    oauth = load_oauth(source)
    name = secret_name(args.slot)
    if args.apply:
        set_secret(args.repo, name, build_secret_payload(oauth))
    report(source, oauth, name, applied=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
