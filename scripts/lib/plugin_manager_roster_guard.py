#!/usr/bin/env python3
"""Гвардия класса #806: плагин добавлен/удалён/изменён в dsh-edge/plugins.json,
а @edge-harness/dsh-plugin-manager (несущий ПОЛНЫЙ вшитый ростер, plugins-src/
plugin-manager/build.mjs — `const roster = manifest.plugins.map(...)`, бандл
window.__ModuleLoader__.load) не пересобран и не перевыпущен конвейером #80.

Живой случай (issue #806, коммит 0a85ddf5, PR #412): плагин agents-tasks
добавлен в dsh-edge/plugins.json (5 -> 6 записей), а source plugin-manager
остался на релизе plugins-plugin-manager-v0.1.9 — деплой падал ДЕВЯТЬ прогонов
подряд на шаге «Скачать плагины и сверить sha256» deploy-dsh-edge.yml:
"Ростер манифеста в релизе plugin-manager-0.1.9.tgz разошёлся с
dsh-edge/plugins.json — пересобери и перевыпусти плагин (конвейер #80)".
Это ТРЕТИЙ такой случай подряд — гвардия ловит его на PR, не постфактум на
деплое main.

Механизм: сравнение ростера (id/server/client, в порядке объявления — та же
чувствительность к порядку, что у живой проверки deploy-dsh-edge.yml,
`jq -S '[.plugins[]|{id,server,client}]'`) между БАЗОВОЙ версией
dsh-edge/plugins.json (pull_request.base.sha — GitHub Contents API,
`gh api repos/<repo>/contents/dsh-edge/plugins.json?ref=<sha>`, один сетевой
вызов) и текущей (рабочее дерево PR). Если ростер изменился, а source записи
plugin-manager (release/asset/sha256) — нет, это и есть класс #806: тарбол,
на который всё ещё ссылается манифест, вшил СТАРЫЙ состав.

Диффовая (не глобально-инвариантная) проверка — намеренно: манифест main
сегодня уже в этом состоянии (пока #806 не закрыт перевыпуском), и гвардия,
проверяющая ТЕКУЩЕЕ состояние безусловно, красила бы CI ЛЮБОГО чужого PR до
следующего форжа plugin-manager (класс «тормоз без газа», AGENTS.md) — вместо
этого гвардия смотрит, ВНЁС ли именно ЭТОТ PR несогласованное изменение.

Запуск: python scripts/lib/plugin_manager_roster_guard.py (только в
pull_request-контексте GitHub Actions — $GITHUB_EVENT_NAME/$GITHUB_EVENT_PATH,
штатные переменные раннера, без дополнительной проводки, тот же приём, что
run_guards.sh уже применяет к $GITHUB_EVENT_NAME). Вне pull_request (push,
workflow_dispatch, schedule, локальный запуск агента) — заявленный no-op с
объяснением, не тихий пропуск.
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import base64
import json
import os
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "dsh-edge" / "plugins.json"
PLUGIN_MANAGER_PACKAGE = "@edge-harness/dsh-plugin-manager"


def roster_projection(manifest: dict) -> list:
    """[{id, server, client}, ...] в порядке объявления — проекция, которую
    сверяет живой шаг deploy-dsh-edge.yml (`jq -S '[.plugins[]|{id,server,
    client}]'`, порядок массива jq -S не переставляет)."""
    return [
        {"id": p.get("id"), "server": p.get("server"), "client": p.get("client")}
        for p in manifest.get("plugins", [])
    ]


def plugin_manager_source(manifest: dict):
    """source {release, asset, sha256} записи plugin-manager, либо None, если
    записи нет вовсе (манифест до появления plugin-manager — теоретический
    случай, но не наше дело гадать форму до появления записи)."""
    for plugin in manifest.get("plugins", []):
        if plugin.get("package") == PLUGIN_MANAGER_PACKAGE:
            return plugin.get("source")
    return None


def roster_bump_violation(old_manifest: dict, new_manifest: dict):
    """Текст нарушения класса #806, либо None, если PR его не вносит."""
    if roster_projection(old_manifest) == roster_projection(new_manifest):
        return None
    if plugin_manager_source(old_manifest) != plugin_manager_source(new_manifest):
        return None
    return (
        "dsh-edge/plugins.json: состав/флаги плагинов изменились в этом PR, а "
        f"source записи {PLUGIN_MANAGER_PACKAGE} (release/asset/sha256) — нет. "
        "plugin-manager вшивает ПОЛНЫЙ ростер в свой релизный бандл "
        "(plugins-src/plugin-manager/build.mjs, константа MANIFEST) — любое "
        "изменение состава требует пересборки и перевыпуска plugin-manager "
        "конвейером #80 (plugin-forge.yml с plugin_path=plugins-src/"
        "plugin-manager), иначе деплой упадёт на шаге «Скачать плагины и "
        "сверить sha256» (класс #806: живой прецедент — 9 подряд красных "
        "прогонов deploy-dsh-edge.yml с 2026-09-08)."
    )


def fetch_base_manifest(repo: str, base_sha: str):
    """Манифест на базовом коммите PR через GitHub Contents API. None, если
    файла на базе не было (PR добавляет dsh-edge/plugins.json впервые) —
    штатная пустота. Любой другой отказ gh — исключение (fail loud, не
    silent-wrong: сетевой сбой не имеет права молча читаться как "PR не
    трогал ростер")."""
    # --method GET обязателен: `gh api` с `-f` без явного --method шлёт POST
    # (проверено живым запросом при разработке этой гвардии — без --method
    # GET тот же вызов отвечал 404, будто файла на базовом коммите не было,
    # хотя он есть; --method GET и `?ref=` в URL дают одинаковый результат).
    result = subprocess.run(
        [
            "gh", "api",
            f"repos/{repo}/contents/dsh-edge/plugins.json",
            "-f", f"ref={base_sha}",
            "--method", "GET",
            "-q", ".content",
        ],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    if result.returncode != 0:
        stderr = result.stderr or ""
        if "404" in stderr or "Not Found" in stderr:
            return None
        raise RuntimeError(f"gh api contents dsh-edge/plugins.json@{base_sha} упал: {stderr.strip()}")
    raw = base64.b64decode(result.stdout.strip() or "")
    return json.loads(raw.decode("utf-8"))


def main() -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME")
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_name != "pull_request" or not event_path or not Path(event_path).is_file():
        print(
            "plugin-manager-roster-guard: событие не pull_request (или без "
            "GITHUB_EVENT_PATH) — диффовая проверка неприменима вне PR, пропуск"
        )
        return 0

    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    base_sha = (event.get("pull_request") or {}).get("base", {}).get("sha")
    if not base_sha:
        print("::error::plugin-manager-roster-guard: pull_request без pull_request.base.sha — контракт события GitHub нарушен")
        return 1

    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("::error::plugin-manager-roster-guard: GITHUB_REPOSITORY не задан раннером")
        return 1

    try:
        old_manifest = fetch_base_manifest(repo, base_sha)
    except (RuntimeError, subprocess.SubprocessError) as error:
        print(f"::error::plugin-manager-roster-guard: не удалось прочитать базовую версию dsh-edge/plugins.json — {error}")
        return 1

    if old_manifest is None:
        print("plugin-manager-roster-guard: dsh-edge/plugins.json — новый файл в этом PR, сравнивать не с чем, пропуск")
        return 0

    new_manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    violation = roster_bump_violation(old_manifest, new_manifest)
    if violation is not None:
        print(f"::error::{violation}")
        return 1

    print("plugin-manager-roster-guard: состав не менялся либо plugin-manager пересобран вместе с ним — согласовано")
    return 0


if __name__ == "__main__":
    sys.exit(main())
