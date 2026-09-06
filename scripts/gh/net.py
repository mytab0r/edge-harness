#!/usr/bin/env python3
"""Единая точка входа в сеть GitHub для python-скриптов scripts/gh/*.

Раньше pr_blockers.py и queue.py держали по своей копии gh()/gh_api()
(~25 строк каждая, почти дословно совпадающих) — комментарий в queue.py
честно признавал "синхронизировать вручную", то есть уже назвал будущий
рецидив словами (#326 находка 2). Здесь одно место правды для обоих:
обёртка над `gh api`.

Имя репозитория скрипты не хранят вовсе: `gh api repos/{owner}/{repo}/...` —
задокументированный плейсхолдер gh CLI, сам резолвится в текущий репозиторий
(git remote в cwd, либо GH_REPO, если задан) — второе место правды на
"mytab0r/edge-harness" не заводится.

Сеть — прямой доступ к api.github.com, как в CI и в облаке (см. docs/agents/INFRA-GH.md
про сетевые настройки конкретной среды).
"""
import json
import os
import subprocess


def gh_api(path: str):
    """gh api с явной UTF-8 кодировкой вывода.

    `path` — начиная с "repos/{owner}/{repo}/..." (или без repo вовсе,
    например "rate_limit") — {owner}/{repo} подставляет сам gh.
    """
    result = subprocess.run(
        # encoding="utf-8" явно: gh отдаёт UTF-8 всегда, а text=True без
        # него декодирует кодовой страницей консоли (cp1251 на Windows) —
        # падает на первой не-ASCII строке (заголовок PR, кириллица).
        ["gh", "api", path], capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise SystemExit(f"ОШИБКА: gh api {path} не прошёл: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None
