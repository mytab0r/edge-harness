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

Замер стоимости шага на реальном каталоге (2026-09-15, 82 файла каталога):
full — 81 c в CI (шаг «Каталог гвардий» job `test`, run 34909630823 на head
этого PR, 23:41:13Z → 23:42:34Z) и 76 c суммой по гвардиям на Linux-раннере
воркера с тёплым pip-кэшем; local — те же ~72 c (скип двух гвардий экономит
2,3 + 2,2 c — их цена не причина исключения, причина ловушка #1228 ниже).
Порядок цены — меньше одной минуты, на порядок дешевле одного круга доводки
(полный повторный запуск воркера + живой вызов модели ревью); поэтому
`full` остаётся БЕЗ отбора, а порог пересмотра решения числовой: если
полный прогон каталога превысит 600 c, отбор становится обязательным.
(Windows-хост владельца не замерен — не подтверждено; там же выполняется
интерактивный local.)

- `full` (автономный воркер, `GITHUB_ACTIONS=true`): полный каталог
  `scripts/ci/run_guards.sh`, без единого исключения — ровно то же самое,
  что и обязательный чек `test`, только выполненное РАНЬШЕ пуша, до того как
  цена ошибки выросла до целого круга доводки (ai-ревью + повторный запуск
  воркера). Чекаут воркера одноразовый (issue #332 docstring
  `scripts/worker/task.sh`) — общего `.git` с другими рабочими деревьями
  нет, поэтому мутирующим сетевым гвардиям (см. ниже) не за что зацепиться.
  Внешний `GUARD_CATALOG_SKIP` из окружения вызывающего здесь ЗАМАЛЧИВАЕТСЯ
  (переменная изымается из окружения дочернего процесса): иначе
  `GUARD_CATALOG_SKIP="..." git push` молча ужал бы полный каталог — гейт,
  который можно тихо ужать переменной, не гейт.
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

«Гейт не смог запуститься» (bash/скрипт отсутствуют, каталог недоступен,
подвисшая гвардия пережила таймаут) — это НЕ «прошло»: если инфраструктура
гейта сама сломана, у нас нет оснований утверждать, что код безопасен для
пуша. Возвращаем `check_result.unknown(...)` и блокируем пуш той же
командой, что и настоящее нарушение (см. `main()`), с тем же аварийным
выходом.

## «Упала гвардия» ≠ «окружение не даёт прогнать» (находка ai-ревью PR #1285)

Локальная зелёность каталога зависит от внешнего окружения: 7 из 82 гвардий
напрямую вызывают `gh` или читают `GH_TOKEN` (замер grep'ом по каталогу,
2026-09-15 — среди них `declared-deps-guard` с его GraphQL-запросом), без
аутентификации/сети они краснеют не из-за кода исполнителя. Протухший PAT
или офлайн-момент без различения давал бы на каждом пушу красный с текстом
«Почини гвардию», который врёт: гвардия починена, слепа машина. Поэтому
перед прогоном каталога гейт проверяет окружение (`gh auth status`,
таймаут 30 c) и при отказе возвращает ТРЕТЬЕ состояние с фактом («gh не
аутентифицирован, код N» / «gh CLI не найден» / «не ответил за 30 c»), а не
красную гвардию: прогон, заранее недостоверный, не выполняется вовсе.
Красный каталог при ПРОЙДЕННОМ префлайте — достоверный `violation`. Остаток,
который префлайт не покрывает и честно не называет покрытым: сетевые сбои
конкретного внешнего сервиса и недоступные записи вне репозитория
(живой случай 2026-09-15: тест `provider-latency-model-discovery-guard`
краснел на записи в `$GITHUB_STEP_SUMMARY` из окружения, где этот путь
не записываем) — против недостоверного красного в таких случаях остаётся
аварийный выход ниже, это названная граница, не молчаливая.

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
не гоняет боевой каталог (82 файла, минуты, сеть) на каждый прогон pytest.

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

# Потолок прогона каталога. Обоснование числом (замер 2026-09-15, см.
# докстринг «Два режима»): полный каталог — 81 c в CI / 76 c посчитано по
# гвардиям локально; 900 c = порядок на запас (холодный pip-кэш, медленная
# машина). Без потолка подвисшая гвардия подвешивала бы пуш навсегда
# (находка ai-ревью PR #1285).
GUARD_CATALOG_TIMEOUT_SECONDS = 900

# Потолок префлайта окружения: `gh auth status` — один вызов API; 30 c —
# порядок на запас медленной сети, не минутный.
GH_PREFLIGHT_TIMEOUT_SECONDS = 30


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


def _gh_preflight(env: dict):
    """None — окружение позволяет достоверный прогон; иначе строка-факт,
    почему прогон заранее недостоверен (см. докстринг «Упала гвардия» ≠
    «окружение не даёт прогнать»)."""
    gh = shutil.which("gh")
    if gh is None:
        return ("gh CLI не найден в PATH — гвардии каталога, требующие gh "
                "(GitHub API), упадут не из-за кода; достоверный прогон "
                "каталога невозможен")
    try:
        proc = subprocess.run(
            [gh, "auth", "status"], capture_output=True, text=True,
            timeout=GH_PREFLIGHT_TIMEOUT_SECONDS, env=env)
    except subprocess.TimeoutExpired:
        return (f"gh auth status не ответил за "
                f"{GH_PREFLIGHT_TIMEOUT_SECONDS} c — достоверный прогон "
                f"каталога невозможен")
    except OSError as exc:
        return f"запуск gh auth status провалился: {exc}"
    if proc.returncode != 0:
        return (f"gh не аутентифицирован или недоступен (gh auth status, "
                f"код {proc.returncode}) — гвардии каталога, требующие gh, "
                f"упадут не из-за кода; достоверный прогон каталога "
                f"невозможен")
    return None


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
    else:
        # full: изъять внешний GUARD_CATALOG_SKIP из окружения дочернего
        # процесса — иначе `GUARD_CATALOG_SKIP="..." git push` молча ужал бы
        # «полный» каталог (см. докстринг, режим full).
        env.pop("GUARD_CATALOG_SKIP", None)

    preflight = _gh_preflight(env)
    if preflight is not None:
        return check_result.unknown(preflight)

    try:
        proc = subprocess.run([bash, str(run_guards)], cwd=str(repo_root),
                              env=env, timeout=GUARD_CATALOG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return check_result.unknown(
            f"прогон каталога гвардий не уложился в "
            f"{GUARD_CATALOG_TIMEOUT_SECONDS} c (подвисшая гвардия?) — "
            f"достоверного вердикта нет")
    except OSError as exc:
        return check_result.unknown(f"запуск {run_guards} провалился: {exc}")

    if proc.returncode == 0:
        return check_result.ok()
    return check_result.violation(
        [f"scripts/ci/run_guards.sh завершился с кодом {proc.returncode} "
         f"(режим {mode}, прогон при проверенной gh-аутентификации) — "
         f"конкретная упавшая гвардия названа в выводе выше строкой "
         f"'::error::гвардия каталога ... провалилась'. Красный здесь — "
         f"упавшая гвардия, не слепота окружения: недостоверный прогон "
         f"приходит отдельным сообщением «НЕ СМОГ запуститься»"])


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
