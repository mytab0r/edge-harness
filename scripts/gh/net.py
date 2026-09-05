#!/usr/bin/env python3
"""Единая точка входа в сеть GitHub для python-скриптов scripts/gh/*.

Раньше pr_blockers.py и queue.py держали по своей копии PROXY_PORTS и
gh()/gh_api() (~25 строк каждая, почти дословно совпадающих) — комментарий в
queue.py честно признавал "синхронизировать вручную, список портов один", то
есть уже назвал будущий рецидив словами (#326 находка 2). Здесь одно место
правды для обоих: порты и обёртка над `gh api`.

Список портов у bash (scripts/gh/lib.sh::GH_NET_PROXY_PORTS) и здесь —
намеренно ДВЕ отдельные копии: два runtime'а (bash/python) не делят процесс,
общий источник для обоих потребовал бы генерации файла из файла ради трёх
чисел. Если порт меняется — правится оба файла, их теперь два, не пять.

Имя репозитория скрипты не хранят вовсе: `gh api repos/{owner}/{repo}/...` —
задокументированный плейсхолдер gh CLI, сам резолвится в текущий репозиторий
(git remote в cwd, либо GH_REPO, если задан) — второе место правды на
"mytab0r/edge-harness" не заводится.
"""
import json
import os
import subprocess

PROXY_PORTS = (1084, 1083, 1085)


def gh_api(path: str):
    """gh api с автоподбором SOCKS-прокси (грабля #1, docs/agents/INFRA-GH.md).

    Если HTTPS_PROXY уже задан в окружении — используется он, без перебора.
    Иначе пробуются порты по очереди; первый успешный ответ и возвращается.
    `path` — начиная с "repos/{owner}/{repo}/..." (или без repo вовсе,
    например "rate_limit") — {owner}/{repo} подставляет сам gh.
    """
    base_env = {**os.environ, "NO_COLOR": "1"}
    candidates = [base_env.get("HTTPS_PROXY")] if base_env.get("HTTPS_PROXY") else [
        f"socks5://127.0.0.1:{port}" for port in PROXY_PORTS
    ]
    last_err = "(нет попыток)"
    for proxy in candidates:
        run_env = dict(base_env)
        if proxy:
            run_env["HTTPS_PROXY"] = proxy
        result = subprocess.run(
            # encoding="utf-8" явно: gh отдаёт UTF-8 всегда, а text=True без
            # него декодирует кодовой страницей консоли (cp1251 на Windows) —
            # падает на первой не-ASCII строке (заголовок PR, кириллица).
            ["gh", "api", path], capture_output=True, text=True, encoding="utf-8", env=run_env,
        )
        if result.returncode == 0:
            return json.loads(result.stdout) if result.stdout.strip() else None
        last_err = result.stderr.strip()
    raise SystemExit(
        f"ОШИБКА: gh api {path} не прошёл ни на одном варианте прокси "
        f"({', '.join(str(c) for c in candidates)}): {last_err}"
    )
