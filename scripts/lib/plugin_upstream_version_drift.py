#!/usr/bin/env python3
"""Гвардия класса #507: plugins-src/*/package.json объявляет devDependency/
peerDependency/dependency на @deepseek-ai/<pkg>@<версия>, а апстрим, ЗАПИНОВАННЫЙ
dsh-edge/upstream.json, реально несёт этот же пакет на ДРУГОЙ версии.

Живой случай (issue #507/#806): после бампа пина upstream.json до 0.11.1
(#505) апстримный apps/dsh-edge/standalone/package.json поднял ВСЕ
@deepseek-ai/dsh-* до 0.1.2-rc.1, а plugins-src/provider-registry/package.json
остался на 0.1.1-rc.2 (dsh-settings, dsh-llm, dsh-llm-deepseek,
dsh-anonymous-user-id, dsh-credentials) — рантайм резолвит имя пакета в
СВОЮ, уже установленную в дереве апстрима версию (0.1.2-rc.1) независимо от
того, что написано в devDependency нашего плагина; расхождение молчит до
следующей сборки/typecheck ИЛИ, как в этом случае, до реального разрыва
экспортов (`@deepseek-ai/dsh-settings` сняла `installSettingsSection` именно
между этими версиями — SyntaxError на живом деплое, 9 подряд красных
прогонов deploy-dsh-edge.yml).

Источник правды по «реальной» версии — apps/dsh-edge/standalone/package.json
АПСТРИМНОГО репозитория на коммите dsh-edge/upstream.json (repo+sha): тот же
файл, что цитирует #507 (docs research), получен ОДНИМ сетевым запросом к
raw.githubusercontent.com (публичный репозиторий, без токена — проверено
живым запросом при разработке этой гвардии).

Проверяются ТОЛЬКО пакеты, которые упстрим объявляет на верхнем уровне
(dependencies/devDependencies standalone-сборки) — транзитивные-only пакеты
(если такие появятся у плагина) не наша забота: у нас нет способа узнать их
версию без полного разрешения графа зависимостей апстрима.

Запуск: python scripts/lib/plugin_upstream_version_drift.py
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import sys
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_SRC_DIR = REPO_ROOT / "plugins-src"
UPSTREAM_JSON_PATH = REPO_ROOT / "dsh-edge" / "upstream.json"
DEP_FIELDS = ("dependencies", "devDependencies", "peerDependencies")
UPSTREAM_STANDALONE_PATH = "apps/dsh-edge/standalone/package.json"
FETCH_TIMEOUT_SECONDS = 20


def collect_plugin_deepseek_deps(plugins_src_dir: Path):
    """[(имя каталога плагина, имя пакета, объявленная версия), ...] по всем
    plugins-src/*/package.json, по всем трём видам зависимостей."""
    found = []
    for pkg_path in sorted(plugins_src_dir.glob("*/package.json")):
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
        plugin_name = pkg_path.parent.name
        for field in DEP_FIELDS:
            block = data.get(field)
            if not isinstance(block, dict):
                continue
            for name, version in block.items():
                if isinstance(name, str) and name.startswith("@deepseek-ai/"):
                    found.append((plugin_name, name, version))
    return found


def find_drift(plugin_deps, upstream_deps: dict):
    """Список текстов нарушений — declared != pinned для пакетов, которые
    апстрим объявляет на верхнем уровне. Пакеты вне upstream_deps пропускаются
    молча (не наша проверка, см. докстринг модуля)."""
    problems = []
    for plugin_name, package_name, declared in plugin_deps:
        pinned = upstream_deps.get(package_name)
        if pinned is None:
            continue
        if pinned != declared:
            problems.append(
                f"plugins-src/{plugin_name}/package.json: {package_name}@{declared} "
                f"разошёлся с пином апстрима dsh-edge/upstream.json ({package_name}@{pinned})"
            )
    return problems


def load_upstream_pin(upstream_json_path: Path):
    data = json.loads(upstream_json_path.read_text(encoding="utf-8"))
    repo = data.get("repo")
    sha = data.get("sha")
    if not isinstance(repo, str) or not repo:
        raise RuntimeError(f"{upstream_json_path}: repo обязателен")
    if not isinstance(sha, str) or not sha:
        raise RuntimeError(f"{upstream_json_path}: sha обязателен")
    return repo, sha


def fetch_upstream_standalone_deps(repo: str, sha: str) -> dict:
    url = f"https://raw.githubusercontent.com/{repo}/{sha}/{UPSTREAM_STANDALONE_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.URLError as error:
        raise RuntimeError(f"не удалось скачать {url}: {error}") from error
    data = json.loads(raw.decode("utf-8"))
    deps = {}
    deps.update(data.get("dependencies") or {})
    deps.update(data.get("devDependencies") or {})
    return deps


def main() -> int:
    try:
        repo, sha = load_upstream_pin(UPSTREAM_JSON_PATH)
    except (RuntimeError, json.JSONDecodeError, OSError) as error:
        print(f"::error::plugin-upstream-version-drift: {UPSTREAM_JSON_PATH} нечитаем — {error}")
        return 1

    try:
        upstream_deps = fetch_upstream_standalone_deps(repo, sha)
    except RuntimeError as error:
        print(f"::error::plugin-upstream-version-drift: не удалось получить пин апстрима ({repo}@{sha}) — {error}")
        return 1

    plugin_deps = collect_plugin_deepseek_deps(PLUGINS_SRC_DIR)
    problems = find_drift(plugin_deps, upstream_deps)
    if problems:
        for problem in problems:
            print(f"::error::plugin-upstream-version-drift: {problem}")
        print(
            f"plugin-upstream-version-drift: {len(problems)} расхождение(й) с пином "
            f"{repo}@{sha} (класс #507) — обнови версию в plugins-src/*/package.json"
        )
        return 1

    print(f"plugin-upstream-version-drift: {len(plugin_deps)} @deepseek-ai/* зависимостей "
          f"plugins-src сверены с пином {repo}@{sha} — расхождений нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
