#!/usr/bin/env python3
"""Гвардия класса #83/#842: `dsh plugin add` в профиле headless требует ДВУХ
предпосылок сразу, обе установлены один раз для hands/worker (#83, PR
#93/#94) и обе были пропущены, когда #838 завёл ТОТ ЖЕ вызов
(`dsh_mount_anthropic_pool`) в третьем файле (scripts/review/ai_dsh.sh):

1. `pnpm` на PATH workflow'а (`pnpm/action-setup@v6`) — без него
   `dsh: pnpm not found on PATH`.
2. `npm_config_ignore_workspace_root_check=true` в окружении скрипта — без
   него `ERR_PNPM_ADDING_TO_ROOT` / `dsh: pnpm failed in profile directory`.

Живой случай (issue #842): ai-review.yml падал на КАЖДОМ прогоне с
2026-09-09T18:32Z сначала на (1) — PR #843 закрыл её; фикс (1) продвинул
прогон дальше и вскрыл (2) (живой прогон PR #837, workflow_dispatch
34399331120, 2026-09-09T20:10:15Z) — тот же класс, вторая недостающая
половина, закрыта здесь же.

Правило этой гвардии: КАЖДЫЙ скрипт из SCRIPT_TO_WORKFLOW, зовущий (кодом,
не комментарием — находка ревью #862: маркер `dsh_mount_anthropic_oauth_pool`
не совпадал с реальным именем `dsh_mount_anthropic_pool`, и worker/hands
матчились лишь строками комментариев/ошибок)
`dsh_mount_anthropic_pool` (или монтаж combo-suite,
`dsh_ensure_plugins_suite`/`dsh plugin add` — тот же класс), обязан нести
И `npm_config_ignore_workspace_root_check=true` сам, И вызываться из
workflow, несущего `pnpm/action-setup`. Не заменяет живой прогон (сетевые
коды ошибок pnpm этот тест не воспроизводит), но ловит регресс СТРУКТУРНО —
до следующего живого прогона, который стоил бы гейта целиком.

Запуск: python -m pytest scripts/lib/test_dsh_plugin_pnpm_guard.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Скрипт -> маркер, что он ЗОВЁТ dsh plugin add (прямо или через
# dsh_mount_anthropic_pool/dsh_ensure_plugins_suite в dsh-ci.sh). Имена —
# реальные функции dsh-ci.sh; был вписан несуществующий
# `dsh_mount_anthropic_oauth_pool`, и ветка никогда не матчила (находка
# ревью #862).
PLUGIN_MOUNT_MARKER_RE = re.compile(
    r"dsh_install_anthropic_pool|dsh_import_anthropic_accounts"
    r"|dsh_mount_anthropic_pool|dsh_ensure_plugins_suite|dsh plugin add"
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
    # Комментарий — не вызов: поясняющий блок ОБЯЗАН называть механизм,
    # который не используется (#860: ai_dsh.sh объясняет ОТСУТСТВИЕ монтажа
    # пула, и по такому комментарию гвардия снова требовала бы pnpm/action-setup
    # в ai-review.yml). Хвост от # вырезается до матчинга — та же эвристика
    # sed 's/#.*$//', что в dsh-anthropic-pool.guard.sh секции 6; целевые
    # вызовы стоят на отдельных строках, эвристика им ничего не прячет.
    return bool(PLUGIN_MOUNT_MARKER_RE.search(re.sub(r"(?m)#.*$", "", text)))


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


def test_every_plugin_mount_script_ignores_pnpm_workspace_root_check():
    missing = []
    for script in SCRIPT_TO_WORKFLOW:
        if not _script_calls_plugin_mount(script):
            continue
        script_rel = str(script.relative_to(REPO_ROOT)).replace("\\", "/")
        text = script.read_text(encoding="utf-8")
        if "npm_config_ignore_workspace_root_check=true" not in text:
            missing.append(f"{script_rel} монтирует плагин через dsh plugin add, но не "
                            "экспортирует npm_config_ignore_workspace_root_check=true — "
                            "класс #83/#842 повторится живым ERR_PNPM_ADDING_TO_ROOT")
    assert not missing, "\n".join(missing)


def test_consumer_scripts_list_is_not_stale():
    # Хотя бы один из трёх файлов реально существует и монтирует плагин —
    # иначе карта SCRIPT_TO_WORKFLOW выше сама стала стейлой незаметно.
    assert any(_script_calls_plugin_mount(s) for s in SCRIPT_TO_WORKFLOW)
