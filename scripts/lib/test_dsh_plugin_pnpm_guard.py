#!/usr/bin/env python3
"""Гвардия класса #83/#842: `dsh plugin add` без `pnpm` на PATH падает
("dsh: pnpm not found on PATH — install pnpm to manage profile plugins").

Живой случай (issue #842): #838 добавил вызов `dsh_mount_anthropic_oauth_pool`
(монтаж anthropic-oauth-pool через `dsh plugin add`) в ТРИ скрипта —
scripts/review/ai_dsh.sh, scripts/worker/task.sh, scripts/hands/dsh_task.sh —
но шаг `pnpm/action-setup@v6` был добавлен только в workflow'ы двух из них
(worker.yml, hands.yml); ai-review.yml остался без pnpm и падал на КАЖДОМ
прогоне с 2026-09-09T18:32Z. Класс уже закрывался один раз (#83, PR #93/#94,
hands.yml) — фикс не был распространён на новый вызов в третьем файле.

Правило этой гвардии: КАЖДЫЙ workflow, чей `run:` шаг вызывает скрипт,
упоминающий `dsh_mount_anthropic_oauth_pool` (или монтаж combo-suite,
`dsh_ensure_plugins_suite`/`dsh plugin add` — тот же класс), обязан нести
`pnpm/action-setup` где-то в файле. Не заменяет живой прогон (реального
`dsh: pnpm not found` этот тест не воспроизводит — нужна сеть/DSH), но ловит
регресс СТРУКТУРНО — до следующего живого прогона, который стоил бы гейта
целиком.

Запуск: python -m pytest scripts/lib/test_dsh_plugin_pnpm_guard.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Скрипт -> маркер, что он ЗОВЁТ dsh plugin add (прямо или через
# dsh_mount_anthropic_oauth_pool/dsh_ensure_plugins_suite в dsh-ci.sh).
PLUGIN_MOUNT_MARKER_RE = re.compile(
    r"dsh_mount_anthropic_oauth_pool|dsh_ensure_plugins_suite|dsh plugin add"
)

# scripts/**.sh, которые ЗАПУСКАЮТ dsh headless (и потому МОГУТ дойти до
# монтажа плагина) -> workflow, чей `run:` шаг их реально исполняет. Тот же
# список каналов, что уже несёт docstring dsh-ci.sh (dsh_run_with_provider_chain:
# ai-review/worker/hands, #727) — явная карта, не второй grep-поиск по всем
# workflow (repo-ci.yml тоже УПОМИНАЕТ эти пути — компиляция/линт скриптов,
# не исполнение; substring-поиск путал линт с реальным вызовом, находка
# ревью #842).
SCRIPT_TO_WORKFLOW = {
    REPO_ROOT / "scripts" / "review" / "ai_dsh.sh": WORKFLOWS_DIR / "ai-review.yml",
    REPO_ROOT / "scripts" / "worker" / "task.sh": WORKFLOWS_DIR / "worker.yml",
    REPO_ROOT / "scripts" / "hands" / "dsh_task.sh": WORKFLOWS_DIR / "hands.yml",
}


def _script_calls_plugin_mount(script: Path) -> bool:
    if not script.exists():
        return False
    text = script.read_text(encoding="utf-8")
    return bool(PLUGIN_MOUNT_MARKER_RE.search(text))


def test_workflow_actually_runs_its_mapped_script():
    """Карта SCRIPT_TO_WORKFLOW не протухла: workflow реально исполняет
    (`run:`), а не просто упоминает, путь скрипта — иначе следующая проверка
    молча проверяет не тот файл."""
    for script, wf in SCRIPT_TO_WORKFLOW.items():
        script_rel = str(script.relative_to(REPO_ROOT)).replace("\\", "/")
        wf_text = wf.read_text(encoding="utf-8")
        # "bash <необязательный $VAR/префикс и кавычка>путь" на одной строке
        # (не просто упоминание в комментарии — repo-ci.yml так делает трижды
        # для scripts/worker/task.sh, ни разу не исполняя его).
        assert re.search(rf"\bbash\b[^\n]*{re.escape(script_rel)}", wf_text), (
            f"{wf.name} больше не запускает {script_rel} через `bash ...` — "
            "карта SCRIPT_TO_WORKFLOW протухла, сверь и поправь"
        )


def test_every_workflow_invoking_plugin_mount_script_has_pnpm_setup():
    missing = []
    for script, wf in SCRIPT_TO_WORKFLOW.items():
        if not _script_calls_plugin_mount(script):
            # Скрипт больше не монтирует плагин (класс не применим) — не
            # наша забота здесь, другая гвардия/ревью заметит удаление кода.
            continue
        script_rel = str(script.relative_to(REPO_ROOT)).replace("\\", "/")
        wf_text = wf.read_text(encoding="utf-8")
        if "pnpm/action-setup" not in wf_text:
            missing.append(f"{wf.name} вызывает {script_rel} (монтирует плагин через "
                            "dsh plugin add), но не несёт шаг pnpm/action-setup — "
                            "класс #83/#842 повторится живым 'pnpm not found on PATH'")
    assert not missing, "\n".join(missing)


def test_consumer_scripts_list_is_not_stale():
    # Хотя бы один из трёх файлов реально существует и монтирует плагин —
    # иначе карта SCRIPT_TO_WORKFLOW выше сама стала стейлой незаметно.
    assert any(_script_calls_plugin_mount(s) for s in SCRIPT_TO_WORKFLOW)
