#!/usr/bin/env python3
"""Обязательный локальный гейт гвардий перед `git push` (issue #1280).

## Класс дефекта

PR #893, коммит `a14deb87ef2f05fd81566310482b57d6cff6218c`: обязательный чек
`test` покраснел в 21:33:57Z на гвардии класса #749 (рукописная регистрация
гвардии в `repo-ci.yml` вместо файла каталога), а живой вердикт ai-ревью с
той же находкой прозой опубликован только в 21:46:16Z — то есть механизм
`scripts/ci/guards/*.sh` уже стоял, уже был обязателен и уже сработал, а
исполнитель отправил работу, ни разу не спросив его. Тот же класс повторился
в PR #733/#789/#823/#893 подряд. Напоминание текстом не лечит: исполнитель
уже получает AGENTS.md/PROTOCOL.md/WORKER-PLAYBOOK.md дословно в каждом
задании (~27 000 токенов, `scripts/worker/task.sh`) и всё равно не запускает
уже существующую, уже обязательную проверку перед пушем. Решение — механизм
(git-хук `pre-push`, невозможно нарушить прогоном мимо), не двадцать восьмая
тысяча токенов инструкции.

## Носитель

`.githooks/pre-push` зовёт этот файл. `core.hooksPath=.githooks` уже
проставляется `scripts/git/task-branch` безусловно — и на пути автономного
воркера (`GITHUB_ACTIONS=true`, одноразовый чекаут CI), и на пути локальных
рабочих деревьев (`.claude/worktrees/<N>-slug>`), без единой правки этого
PR: новый файл каталога `.githooks/` подхватывается автоматически везде,
где хук уже настроен.

## Два режима, одна причина расхождения — измерено, не предположено

- `full` (автономный воркер, `GITHUB_ACTIONS=true`): полный каталог
  `scripts/ci/run_guards.sh`, без единого исключения — ровно то же самое,
  что и обязательный чек `test`, только выполненное РАНЬШЕ пуша, до того как
  цена ошибки выросла до целого круга доводки (ai-ревью + повторный запуск
  воркера). Чекаут воркера одноразовый (issue #332 docstring
  `scripts/worker/task.sh`) — общего `.git` с другими рабочими деревьями
  нет, поэтому мутирующим сетевым гвардиям (см. ниже) не за что зацепиться.
- `local` (интерактивный агент/человек в `.claude/worktrees/<N>-slug>`):
  тот же каталог, но с `GUARD_CATALOG_SKIP` (см. `scripts/ci/run_guards.sh`)
  на две гвардии. Причина не в скорости — измерено 2026-09-15: единичный
  локальный прогон `scripts/ci/run_guards.sh` (без исключений) перевёл
  `git rev-parse --is-shallow-repository` false -> true, т.е. замусорил
  ОБЩИЙ `.git` каталог ВСЕХ рабочих деревьев задачи (ловушка #1228,
  AGENTS.md) через `decision_numbering.py::fetch_refs`, вызванный из
  `decision-doc-numbering-guard.sh` и `invariant-numbering-guard.sh`. 1 из 1
  воспроизведённых локальных прогонов — не ощущение, факт. Обе гвардии
  по-прежнему обязательны для слияния — они гоняются в CI (`test`) без
  исключений, только не автоматически на каждом локальном `git push`.

## Третье состояние (issue #1096, носитель `scripts/lib/check_result.py`)

«Гейт не смог запуститься» (bash/скрипт отсутствуют) — это НЕ «прошло»: если
инфраструктура гейта сама сломана, у нас нет оснований утверждать, что код
безопасен для пуша. Возвращаем `check_result.unknown(...)` и блокируем пуш
той же командой, что и настоящее нарушение (см. `main()`), с тем же
аварийным выходом.

## Аварийный выход (правило «тормоз без газа», AGENTS.md)

`GUARD_GATE_SKIP_ACK="<причина>"` перед `git push` пропускает ТОЛЬКО этот
локальный гейт, с печатью причины в stderr (тот же паттерн, что
`--no-task-ack`/`--confirm-not-duplicate`/`--not-process-ack` у
`scripts/gh/issue-create` — явное решение, а не тихий пропуск). Это не дыра
назад: обязательный серверный чек `test` (branch protection, не читает эту
переменную, ветку без него слить нельзя) по-прежнему гоняет ПОЛНЫЙ каталог
после пуша — снятый локальный гейт лишь возвращает цену ошибки к тому же
уровню, что был ДО этого PR (узнаёшь о нарушении из CI, не до пуша), не
открывает способ слить нарушение мимо CI.

## Живая находка при первом догфудинге (2026-09-15) — утечка GIT_* в дочерние гвардии

Первый же реальный `git push` из рабочего дерева этой задачи (worktree одного
общего репозитория с `D:/Claude/edge-harness`) покрасил
`data-branch-writer-guard` ложно: git сам подставляет `GIT_DIR`/`GIT_WORK_TREE`/
`GIT_INDEX_FILE`/`GIT_PREFIX` в окружение хука ПЕРЕД его запуском (стандартная
особенность git-хуков, не баг этого репозитория) — `GIT_DIR` указывал на общий
`.git/worktrees/<эта задача>` ЭТОГО репозитория. `os.environ.copy()` без
очистки протаскивал `GIT_DIR` дальше в `bash scripts/ci/run_guards.sh` и в
каждый дочерний `git` внутри тестов гвардий (`subprocess.run(["git", "-C",
seed, "checkout", ...])`) — `-C` меняет РАБОЧИЙ каталог, но не отменяет
`GIT_DIR` из окружения, поэтому git резолвил репозиторий НЕ в свежий temp-клон
теста, а в общий `.git` этой задачи — тест `test_data_branch_writer.py`
столкнулся с «'main' уже используется рабочим деревом D:/Claude/edge-harness»,
хотя тестируемый код к этому дереву отношения не имеет. Фикс — `_clean_git_env`
убирает ВСЕ переменные `GIT_*` из окружения дочернего процесса перед
`bash scripts/ci/run_guards.sh`: гвардии сами открывают репозитории явно
(`-C`/`cd`/фикстурные temp-репо), полагаться на унаследованный `GIT_DIR` им не
нужно нигде в каталоге (проверено прогоном полного каталога после фикса).

## Тестируемость

`GUARD_GATE_REPO_ROOT` — переопределение корня репозитория (по умолчанию
вычисляется от расположения этого файла). Поведенческий тест
(`scripts/lib/test_pre_push_guard_gate.py`) наводит на синтетический
temp-репозиторий со своим `scripts/ci/run_guards.sh`/`scripts/ci/guards/`,
не гоняет боевой каталог (81 файл, минуты, сеть) на каждый прогон pytest.

Запуск тестов: python -m pytest scripts/lib/test_pre_push_guard_gate.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import os
import shutil
import subprocess
import sys

_CR_SPEC = importlib.util.spec_from_file_location(
    "check_result", Path(__file__).resolve().parent / "check_result.py")
check_result = importlib.util.module_from_spec(_CR_SPEC)
_CR_SPEC.loader.exec_module(check_result)

# Гвардии-исключения ТОЛЬКО режима local — см. докстринг модуля и комментарий
# scripts/ci/run_guards.sh::GUARD_CATALOG_SKIP. Список данных, не эвристика:
# менять его — значит осознанно решить за/против конкретной гвардии, а не
# подгонять регэксп под изменившееся имя файла.
LOCAL_MODE_SKIP = ("decision-doc-numbering-guard", "invariant-numbering-guard")


def _repo_root() -> Path:
    override = os.environ.get("GUARD_GATE_REPO_ROOT")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parent.parent.parent


def _mode() -> str:
    return "full" if os.environ.get("GITHUB_ACTIONS") == "true" else "local"


def _clean_git_env(env: dict) -> dict:
    """Убирает GIT_* из окружения дочернего процесса (см. докстринг модуля,
    находка 2026-09-15): git сам подставляет GIT_DIR/GIT_WORK_TREE/
    GIT_INDEX_FILE/GIT_PREFIX в окружение любого хука ДО его запуска — эти
    переменные переживают `-C другой-каталог` во ВЛОЖЕННЫХ git-командах,
    которые гвардии/их тесты запускают сами (temp-репозитории, worktree),
    и подменяют собой репозиторий, который они на самом деле открывают."""
    return {k: v for k, v in env.items() if not k.startswith("GIT_")}


def run_gate(repo_root: Path, mode: str):
    """Возвращает `check_result.CheckResult`. Не решает, блокировать ли пуш —
    это дело `main()` (третье состояние формируется здесь, интерпретация
    «блокировать/нет» — на уровне вызывающего, как и у остальных модулей,
    использующих `check_result`)."""
    run_guards = repo_root / "scripts" / "ci" / "run_guards.sh"
    if not run_guards.exists():
        return check_result.unknown(
            f"{run_guards} отсутствует — прогон каталога гвардий невозможен")
    bash = shutil.which("bash")
    if bash is None:
        return check_result.unknown(
            "bash не найден в PATH — прогон каталога гвардий невозможен")

    env = _clean_git_env(os.environ.copy())
    if mode == "local":
        env["GUARD_CATALOG_SKIP"] = " ".join(LOCAL_MODE_SKIP)

    try:
        proc = subprocess.run([bash, str(run_guards)], cwd=str(repo_root), env=env)
    except OSError as exc:
        return check_result.unknown(f"запуск {run_guards} провалился: {exc}")

    if proc.returncode == 0:
        return check_result.ok()
    return check_result.violation(
        [f"scripts/ci/run_guards.sh завершился с кодом {proc.returncode} "
         f"(режим {mode}) — конкретная упавшая гвардия названа в выводе выше "
         f"строкой '::error::гвардия каталога ... провалилась'"])


def main() -> int:
    ack = os.environ.get("GUARD_GATE_SKIP_ACK", "").strip()
    if ack:
        print(
            f"ПРЕДУПРЕЖДЕНИЕ: обязательный локальный гейт гвардий (issue #1280) "
            f"пропущен по GUARD_GATE_SKIP_ACK: {ack}. Обязательный серверный чек "
            f"'test' по-прежнему прогонит полный каталог после пуша.",
            file=sys.stderr,
        )
        return 0

    repo_root = _repo_root()
    mode = _mode()
    result = run_gate(repo_root, mode)
    emoji = check_result.status_emoji(result.status)

    if result.status == check_result.STATUS_OK:
        print(f"{emoji} guard-gate({mode}): каталог гвардий чист — пуш разрешён")
        return 0

    if result.status == check_result.STATUS_UNKNOWN:
        print(
            f"{emoji} guard-gate({mode}): гейт НЕ СМОГ запуститься — {result.reason}. "
            f"Пуш заблокирован (третье состояние, issue #1096 — 'не смог посмотреть' "
            f"не значит 'прошло'). Почини окружение и повтори git push, либо осознанно "
            f"пропусти разово: GUARD_GATE_SKIP_ACK=\"<причина>\" git push ...",
            file=sys.stderr,
        )
        return 1

    print(
        f"{emoji} guard-gate({mode}): {result.violations[0]}. "
        f"Почини гвардию и повтори git push, либо осознанно пропусти разово: "
        f"GUARD_GATE_SKIP_ACK=\"<причина>\" git push ...",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
