#!/usr/bin/env python3
"""Дешёвый механический ребейз конфликтных PR (issue #762).

Зачем отдельный модуль, не правка scheduler.py: `dispatch_conflict_rework`
(scheduler.py) уже расшивает конфликты, но ЕДИНСТВЕННЫМ путём — через
LLM-агента `worker.yml` (git rebase происходит внутри хода DSH). Живой
замер 2026-09-08: 25 конфликтных PR из 38 открытых, ни один не сливается —
при «воркер один на репозиторий, ровно один workflow_dispatch за пульс»
это 25 отдельных агентских проходов, каждый жжёт квоту дефицитного
LLM-провайдера, хотя сам `dispatch_conflict_rework` дословно говорит: «PR с
меткой conflict почти всегда просто отстал от main — git rebase
origin/main решает это без содержательного решения». Признака «дрейф или
содержательный конфликт» ДО попытки не существует (GitHub REST отдаёт
только mergeable_state, не конфликтующие ханки) — САМА попытка ребейза и
есть этот признак, а она дешёвая и не требует модели вовсе.

`scheduler.py` намеренно НЕ заводит локальный git-клон (см. докстринг
модуля и `openspec/changes/conflict-auto-rebase/proposal.md`, раздел «Что
вне рамок»: единственный источник состояния там — `gh api`). Этот модуль —
осознанное исключение из того принципа, поэтому живёт отдельно, не внутри
scheduler.py: он работает НАД РЕАЛЬНЫМ git-деревом (job с `actions/
checkout@v7`, `fetch-depth: 0`), которого у scheduler.py принципиально нет.

Разделение труда после этой правки:
  - `mechanical_rebase.py` (этот модуль) — берёт ВСЕ PR с меткой `conflict`
    за один проход, пытается `git rebase origin/main` МЕХАНИЧЕСКИ, без
    модели. Сошлось — пушит `--force-with-lease`; снятие метки `conflict`
    остаётся ЗА `mark_conflicts` (scheduler.py) — единственное место,
    которое меняет эту метку (не заводим вторую точку записи того же
    факта: `mark_conflicts` и так перепроверяет `mergeable_state` на
    следующем проходе, у него уже есть вся логика «„не знаю“ не значит
    „нет конфликта“», дублировать её здесь — второй источник истины).
  - `dispatch_conflict_rework` (scheduler.py, НЕ изменён) — по-прежнему
    зовёт агента, но теперь только для PR, которые механический проход НЕ
    смог свести: PR, у которого этот модуль снял метку `conflict` (руками
    mark_conflicts, следующим тактом), уже не попадает в выборку по метке
    `conflict` — агент на него не тратится.

Шесть исходов на PR (process_pull), различены по смыслу, а не по тексту
ошибки git (тот протухнет при смене формулировки — тот же принцип, что
WORKER_GIT_STEP_MARKER в scheduler.py):
  - "resolved"          — рёбейз сошёлся БЕЗ единого конфликта, ветка
                            запушена `--force-with-lease`;
  - "resolved-additive"  — issue #1032: по пути был хотя бы один структурный
                            конфликт, но КАЖДЫЙ сведён механически
                            (additive_conflict_merge.try_resolve — класс «обе
                            стороны независимо дописали новый элемент в одну
                            точку общего реестра», см. докстринг этого
                            модуля) и перепроверен (ast.parse полного файла +
                            pytest затронутых test_*.py) ДО `git rebase
                            --continue`; ветка запушена так же, как
                            "resolved" — вызывающая сторона (process_pull) не
                            различает эти два исхода при решении пушить;
  - "conflict"           — рёбейз СТРУКТУРНО уткнулся в конфликт, который
                            additive_conflict_merge либо не признал своим
                            классом (обычная правка одного и того же кода —
                            основной случай), либо признал, но верификация
                            (синтаксис/тесты) отказала: после неудачного
                            `git rebase` существует каталог
                            `.git/rebase-merge` или `.git/rebase-apply` И в
                            индексе есть НЕЗАВЕДЁННЫЕ пути (`git diff
                            --diff-filter=U`) — оба признака самого git, не
                            подстрочный матч stderr. Каталог паузы рёбейза
                            заводится и в этом случае, И тогда, когда патч
                            применился БЕЗ конфликта, но `git commit` внутри
                            рёбейза упал по другой причине (issue #764,
                            находка ревью, требование 1: отсутствующая git
                            identity на раннере даёт rc=128 «unable to
                            auto-detect email address» и ТОТ ЖЕ каталог паузы
                            без единого конфликтующего пути) — различаем по
                            наличию незаведённых путей, см. attempt_rebase.
                            PR остаётся кандидатом агентского пути, эта
                            функция НЕ трогает assignee/замок/dispatch;
  - "ai-review-running"  — тот же тормоз, что update_branch (scheduler.py):
                            не двигаем head, пока по PR летит ai-review.yml
                            (review_labels.other_active_ai_review_runs —
                            переиспользован, вторая копия не заводится);
  - "worker-running"     — issue #764, требование 2, СУЖЕНО issue #1032: не
                            двигаем head, если активный `in_progress`-прогон
                            worker.yml несёт CLAIM_VIA-след ИМЕННО задачи
                            этого PR (worker_blocks_pr — единственный воркер
                            физически может пушить только в СВОЮ ветку в
                            данный момент, не в любую конфликтную), ЛИБО
                            активный прогон ещё `queued` (атрибуция
                            невозможна — claim ещё не мог случиться), ЛИБО
                            он `in_progress` без следа, но МОЛОЖЕ порога
                            sch.WORKER_CLAIM_TRACE_GRACE_MINUTES (след
                            появляется в первые минуты job'а, не в первую
                            секунду статуса; находка ai-review PR #1033,
                            требование 2 — без этого окна гейт молча
                            разрешал самый опасный случай), — см.
                            блок-комментарий у worker_blocks_pr;
                            без этого гейта механический force-with-lease мог
                            бы уехать НАД коммитами агента и сжечь его
                            единственную засчитанную попытку
                            (CONFLICT_REWORK_MAX_ATTEMPTS=1) вхолостую;
  - "infra-error: <текст>" — сбой НЕ через конфликт (сеть, права, ветка
                            удалена, force-with-lease отклонён гонкой,
                            отсутствующая git identity) — AGENTS.md, «fail
                            loud, не гадать»: лечится иначе, чем
                            содержательный конфликт, поэтому называется
                            отдельной строкой в отчёте.

Обход очереди — от старейшего эпизода конфликта к новейшему (issue #588,
`sch.conflict_labeled_at` — переиспользован, вторая сортировка не
заводится): без этого свежие конфликты систематически обгоняли бы старые
голодающие PR тем же классом бага, что уже был найден и исправлен для
агентского пути.

Известный, документированный, самокорректирующийся край: между пушем этого
модуля и следующим пересчётом `mergeable_state` на стороне GitHub есть
асинхронное окно (см. `docs/research/21-github-actions.md` — GitHub не
гарантирует немедленный пересчёт). Если `orchestra`-job (mark_conflicts)
успеет спросить `mergeable_state` РАНЬШЕ, чем GitHub пересчитал его после
нашего пуша, метка `conflict` на мгновение вернётся. НИКАКОЙ механизм это
окно не сокращает: `orchestra.yml` и `conflict-mechanical-rebase.yml` —
два независимых workflow-файла на одном 15-минутном кроне без взаимной
`needs:` (cross-workflow `needs` в GitHub Actions не существует вовсе, см.
design.md, «Развилка 2», «Цена решения» — находка ревью issue #764,
требование 3: более ранняя версия этого докстринга ошибочно утверждала
обратное). Цена не бесплатна (спам-комментарий mark_conflicts «PR
конфликтует с main»), но следующий же проход этого модуля увидит `git
rebase` как no-op («Already up to date») и снова запушит/пропустит без
вреда — самокорректируется, отдельного тормоза не требует.

Запуск: python scripts/orchestra/mechanical_rebase.py (внутри job'а с
полным git-деревом и PAT-авторизацией git — см. .github/workflows/
conflict-mechanical-rebase.yml, СВОЙ workflow-файл, не job внутри
orchestra.yml — изоляция от pulse_guard.heartbeat_check, см. докстринг
самого workflow-файла и openspec/changes/mechanical-conflict-rebase/
design.md, «Развилка 2»).

main() отказывается работать вне CI (issue #764, находка приёмки): требует
ОДНОВРЕМЕННО GITHUB_ACTIONS=true И GITHUB_RUN_ID (см. _require_ci_environment) —
ensure_clean_repo безусловно делает `git rebase --abort`/`reset --hard`/
`clean -fd` над repo_dir, в CI это безопасно (actions/checkout даёт свежее
дерево), но при случайном ручном запуске снесло бы незакоммиченные правки и
незавершённый рёбейз человека. Одного признака GITHUB_ACTIONS недостаточно:
.githooks/pre-commit использует ЭТУ ЖЕ переменную РОВНО НАОБОРОТ (её
присутствие ОТКЛЮЧАЕТ гвардию хука) и сам называет риск, что она утечёт в
локальное окружение (частый случай при работе с `gh`/`act`) — опираться на
неё одну означало бы наследовать тот же риск здесь.

Тесты: python -m pytest scripts/orchestra/test_mechanical_rebase.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))  # scheduler.py делает `from pulse_guard import …`

import scheduler as sch  # noqa: E402 — после sys.path выше, тот же приём, что тесты scheduler.py
import additive_conflict_merge  # noqa: E402 — тот же приём, свой модуль этого же каталога

review_labels = sch.review_labels  # уже загруженный scheduler'ом модуль — не грузим второй раз

# Загрузка по пути файла (issue #897) — тот же приём, что console_utf8 bootstrap
# выше и сам guard_step_translator.py уже применяют (в scripts/lib нет
# __init__.py). Регистрация в sys.modules ДО exec_module: модуль несёт
# @dataclass на отложенных аннотациях (см. его же докстринг _load_sibling) —
# без регистрации импорт падает AttributeError на Python 3.11.
_gst_spec = importlib.util.spec_from_file_location(
    "guard_step_translator", _DIR.parent / "lib" / "guard_step_translator.py"
)
guard_step_translator = importlib.util.module_from_spec(_gst_spec)
sys.modules["guard_step_translator"] = guard_step_translator
_gst_spec.loader.exec_module(guard_step_translator)  # type: ignore[union-attr]


class GitError(RuntimeError):
    """Сбой git-операции (сеть, права, отсутствующая ветка, force-with-lease
    отклонён гонкой) — ОТДЕЛЬНЫЙ класс от «структурный конфликт» (см.
    attempt_rebase: конфликт распознаётся по каталогу rebase-merge/-apply,
    не по этому исключению)."""


def run_git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        # GIT_EDITOR=true (issue #1032): `git rebase --continue` после
        # аддитивного сведения (additive_conflict_merge) хочет открыть редактор
        # для commit message — раннер CI его не имеет, а сообщение и так не
        # меняется (git rebase сохраняет исходное). "true" — no-op-редактор,
        # принимает предзаполненное сообщение как есть; безвредно для ВСЕХ
        # остальных git-команд этого модуля (fetch/checkout/rebase/push/add
        # редактор не открывают вовсе).
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"},
    )
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(result.stderr or result.stdout).strip()}")
    return result


def _rebase_paused(repo_dir: Path) -> bool:
    return (repo_dir / ".git" / "rebase-merge").exists() or (repo_dir / ".git" / "rebase-apply").exists()


def ensure_clean_repo(repo_dir: Path) -> None:
    """Защита от каскада (issue #764, находка ревью, требование 5): если
    `git rebase --abort` в attempt_rebase сам упадёт (сеть, файловая
    система) на ОДНОМ PR очереди, `.git/rebase-merge`/`-apply` остаётся, и
    СЛЕДУЮЩИЙ PR очереди получает `git checkout`/`git rebase`, падающие с
    «you need to resolve your current index first» — не потому, что у НЕГО
    есть конфликт, а потому, что предыдущая итерация не отмылась.
    Принудительно возвращает рабочее дерево в чистое состояние ПЕРЕД каждой
    попыткой: если abort сам не справился, каталог паузы удаляется вручную
    (структурный факт, не догадка) — решает класс, а не конкретный сбой."""
    if _rebase_paused(repo_dir):
        run_git(["rebase", "--abort"], repo_dir, check=False)
        for name in ("rebase-merge", "rebase-apply"):
            marker = repo_dir / ".git" / name
            if marker.exists():
                shutil.rmtree(marker, ignore_errors=True)
    run_git(["reset", "--hard"], repo_dir, check=False)
    run_git(["clean", "-fd"], repo_dir, check=False)


# Потолок итераций цикла ниже (issue #1032) — чисто защитный: каждая
# итерация «конфликт → аддитивно сведён → --continue» расходует РОВНО один
# коммит ветки PR (git rebase не может застрять на одном и том же коммите
# дважды — --continue либо продвигается, либо сам возвращает ошибку/новую
# паузу на следующем коммите), поэтому реальный предел — число коммитов PR,
# всегда конечное. Число ниже — заведомо выше любого практического PR этого
# репозитория (contract_check.py уже ограничивает диффы разумным размером),
# это strictly paranoia-гвардия от гипотетического бага цикла, не рабочий
# лимит.
_MAX_ADDITIVE_CONTINUE_ATTEMPTS = 50


def attempt_rebase(repo_dir: Path, head_ref: str) -> str:
    """Возвращает "resolved" (рёбейз сошёлся ЧИСТО, без единого конфликта),
    "resolved-additive" (один или несколько конфликтов по пути сведены
    механически — additive_conflict_merge, issue #1032: ОБЕ стороны
    независимо дописали новый элемент в одну точку общего реестра, см.
    докстринг additive_conflict_merge.py) или "conflict" (структурно уткнулись
    и НЕ сведены — рёбейз уже отменён `git rebase --abort`, working tree
    чист). И "resolved", и "resolved-additive" — working tree на
    перебазированной ветке, пуш ещё не сделан (push_rebased — отдельный шаг,
    вызывающая сторона не различает эти два исхода при принятии решения
    пушить/не пушить). Любой другой сбой — GitError.

    Перед КАЖДОЙ попыткой — ensure_clean_repo (issue #764, требование 5):
    рабочее дерево гарантированно не несёт паузу рёбейза от предыдущего PR
    очереди."""
    ensure_clean_repo(repo_dir)
    run_git(["fetch", "origin", "main", head_ref], repo_dir)
    run_git(["checkout", "-B", head_ref, f"origin/{head_ref}"], repo_dir)
    result = run_git(["rebase", "origin/main"], repo_dir, check=False)
    used_additive_merge = False
    attempts = 0
    while result.returncode != 0:
        attempts += 1
        if attempts > _MAX_ADDITIVE_CONTINUE_ATTEMPTS:
            run_git(["rebase", "--abort"], repo_dir, check=False)
            raise GitError(
                f"git rebase не сошёлся за {_MAX_ADDITIVE_CONTINUE_ATTEMPTS} "
                "итераций аддитивного сведения — защитный потолок, не рабочий "
                "лимит (см. докстринг attempt_rebase)"
            )
        # Каталог паузы рёбейза — структурный признак самого git, не текстовый
        # матч stderr (тот протухнет при смене формулировки, тот же принцип,
        # что WORKER_GIT_STEP_MARKER). НО каталог заводится в ДВУХ разных
        # случаях: (1) честный текстовый конфликт при cherry-pick патча, (2)
        # патч применился БЕЗ конфликта, но `git commit` внутри рёбейза упал по
        # другой причине — живой пример (issue #764, находка ревью, требование
        # 1): раннер без git identity, `git rebase` падает rc=128 «unable to
        # auto-detect email address», каталог паузы тот же самый. Различаем по
        # факту НЕЗАВЕДЁННЫХ путей в индексе (`git diff --diff-filter=U`) — это
        # тоже git-native структурный сигнал (unmerged paths), не подстрочный
        # матч stderr: настоящий конфликт всегда оставляет unmerged-запись,
        # сбой на этапе commit — никогда (мутационно проверено #764: rc=128 без
        # identity даёт `git diff --diff-filter=U` пустым).
        if not _rebase_paused(repo_dir):
            raise GitError(
                f"git rebase origin/main упал не через конфликт (rc={result.returncode}): "
                f"{(result.stderr or result.stdout).strip()}"
            )
        unmerged_output = run_git(
            ["diff", "--name-only", "--diff-filter=U"], repo_dir, check=False
        ).stdout.strip()
        if not unmerged_output:
            run_git(["rebase", "--abort"], repo_dir, check=False)
            raise GitError(
                "git rebase остановился (создан каталог паузы), но конфликтующих "
                f"путей в индексе нет (rc={result.returncode}) — это НЕ содержательный "
                "конфликт, а сбой на этапе commit (частая причина — не настроена git "
                f"identity в этом окружении): {(result.stderr or result.stdout).strip()}"
            )
        unmerged = unmerged_output.splitlines()
        # Лишь ПОСЛЕ того, как признали конфликт содержательным (unmerged
        # непуст) — пробуем механическое сведение аддитивных вставок (issue
        # #1032). Отказ additive_conflict_merge.try_resolve (None) —
        # честный «не наш класс», штатный путь ниже (abort → "conflict",
        # PR остаётся кандидатом агентского пути) НЕ меняется.
        resolved_paths = additive_conflict_merge.try_resolve(repo_dir, unmerged)
        if resolved_paths is None:
            run_git(["rebase", "--abort"], repo_dir, check=False)
            return "conflict"
        for rel in resolved_paths:
            run_git(["add", rel], repo_dir)
        used_additive_merge = True
        result = run_git(["rebase", "--continue"], repo_dir, check=False)
    return "resolved-additive" if used_additive_merge else "resolved"


def push_rebased(repo_dir: Path, head_ref: str) -> None:
    run_git(["push", "--force-with-lease", "origin", f"{head_ref}:{head_ref}"], repo_dir)


def migrate_guard_steps_if_needed(repo_dir: Path) -> str | None:
    """После УСПЕШНОГО механического ребейза (attempt_rebase → "resolved"),
    ДО push_rebased (issue #897): git применяет патч контекстно и ничего не
    знает про каталог гвардий `scripts/ci/guards/` (#749/#771) — PR,
    добавивший рукописный шаг ДО #771, сводится без конфликта, но
    `ci_guard_registration_guard.py` красит CI этого же PR сразу после
    ребейза (живой замер issue #897: PR #890/#895, 14 открытых PR/20 шагов
    на дату замера). `guard_step_translator.translate_repo_ci` переносит всё,
    что умеет, детерминированно; результат коммитится ОТДЕЛЬНЫМ коммитом
    поверх уже перебазированной ветки — `push_rebased` пушит его вместе с
    остальными.

    Не блокирует push резолвнутого PR: перенос — УЛУЧШЕНИЕ, не условие
    "resolved" (attempt_rebase уже решил структурный вопрос конфликта).
    Ничего не нашлось — None, тихо (не находка, TranslationResult.migrated
    пуст — не тормоз без газа, это норма для подавляющего большинства PR).
    Нашёлся неразбираемый шаг ИЛИ упал ЛЮБОЙ этап переноса (находка ревью
    PR #902 и её вторая итерация на этом же PR): `git rebase` мог оставить
    repo-ci.yml с текстуальным конфликтным маркером/повреждением (тогда
    `yaml.safe_load` бросает `yaml.YAMLError`, не `UnsupportedStepError`);
    запись файла каталога теоретически может упасть `OSError` (заполненный
    диск раннера); а git-фаза `add`/`commit` может упасть `GitError`
    (наследник RuntimeError от run_git) — до второй итерации правки она
    стояла ВНЕ try, и `GitError` от `git commit` вылетал в `process_pull`
    как RuntimeError: тот возвращал "infra-error", `push_rebased` НЕ
    вызывался, и УСПЕШНО перебазированная ветка переставала пушиться вовсе
    — ровно то ухудшение исхода "resolved", которое этот докстринг
    отрицает. Поэтому ВЕСЬ перенос (трансляция + git add + git commit)
    стоит под ОДНИМ `try/except Exception`, а не только
    `UnsupportedStepError`/`yaml.YAMLError`/`OSError`: любое другое
    исключение здесь тоже убило бы ВЕСЬ проход `run()` (остаток очереди не
    обрабатывается). PR всё равно пушится КАК ЕСТЬ (тот же исход, что и до
    этой правки; недозакоммиченные файлы переноса в рабочем дереве в push
    не попадают и вытираются `ensure_clean_repo` следующего PR очереди),
    CI после push покажет ту же красную гвардию, но уже с точной
    инструкцией газа (см. ci_guard_registration_guard.py::check_no_undeclared_step)
    — фикс ухудшить исход "resolved" не может, только упростить его для
    человека.

    Ветка/дерево без `.github/workflows/repo-ci.yml` вовсе (например,
    синтетические git-фикстуры тестов этого модуля, которым файл гвардий не
    предмет проверки) — None БЕЗ вызова translate_repo_ci: физически нечего
    переносить, это не находка транслятора (guard_step_names читает файл
    безусловно и бросил бы FileNotFoundError, если бы не эта проверка).

    История этой функции в #1032: первая итерация PR случайно стёрла её
    вместе со всем переносом #897 при переписывании модуля (находка
    ai-review PR #1033, требование 1 — скрытая регрессия чужого фикса,
    молча оставлявшая `translate_repo_ci` без единого продового вызывающего);
    восстановлена дословно — с новым циклом аддитивного сведения совместима:
    вызывается после attempt_rebase (в т.ч. "resolved-additive" — конфликтные
    файлы уже сведены, repo-ci.yml среди них мог быть), до push_rebased."""
    if not (repo_dir / ".github" / "workflows" / "repo-ci.yml").exists():
        return None
    try:
        result = guard_step_translator.translate_repo_ci(repo_dir)
        if not result.migrated:
            return None
        rel_paths = [str(p.relative_to(repo_dir)) for p in result.changed_paths]
        run_git(["add", "--", *rel_paths], repo_dir)
        names = ", ".join(m.step_name for m in result.migrated)
        run_git(
            ["commit", "-m", f"перенос рукописных шагов гвардии в каталог (#897): {names}"],
            repo_dir,
        )
    except guard_step_translator.UnsupportedStepError as error:
        return f"перенос рукописных шагов гвардии в каталог не выполнен: {error}"
    except Exception as error:
        # Находка ревью PR #902 и её вторая итерация: ЛЮБОЙ сбой переноса —
        # yaml.YAMLError/OSError от трансляции, GitError от git add/commit —
        # не должен вылетать наружу process_pull/run(): незапойманное
        # исключение превращает "resolved" в "infra-error" (push_rebased не
        # вызван, ветка не запушена) и убивает весь проход по очереди, хотя
        # докстринг обещает, что перенос не может ухудшить уже решённый
        # исход "resolved".
        return (
            "перенос рукописных шагов гвардии в каталог упал сбоем ("
            f"{type(error).__name__}), не находкой транслятора: {error}"
        )
    return None


def _same_repo_agent_branch(repo: str, pull: dict) -> bool:
    """issue #764, находка ревью, требование 4: репозиторий публичный, PR из
    форка возможен — `head.ref` без проверки `head.repo.full_name` может
    называть ветку `origin` этого репозитория с СОВПАДАЮЩИМ именем, но
    принадлежащую чужому PR. Без этого фильтра механический путь ребейзил
    бы и `--force-with-lease`-пушил ЧУЖУЮ ветку `origin`, приписывая исход
    номеру PR из форка. Заодно исключаем ветки без префикса `agent/`
    (включая `main`) — тот же структурный признак, что использует
    task-branch/#356, не текстовый список исключений."""
    head = pull.get("head") or {}
    ref = head.get("ref") or ""
    head_repo_full_name = (head.get("repo") or {}).get("full_name")
    return head_repo_full_name == repo and ref.startswith("agent/")


def conflict_queue(repo: str, pulls: list[dict]) -> list[dict]:
    """PR с меткой `conflict`, от старейшего эпизода к новейшему — та же
    дисциплина обхода, что `dispatch_conflict_rework` (issue #588):
    переиспользует `sch.conflict_labeled_at` (одно место правды), вторую
    сортировку не заводит. Фильтрует чужие/форкнутые ветки — см.
    _same_repo_agent_branch."""
    conflict_pulls = [
        p for p in pulls
        if sch.CONFLICT_LABEL in {label["name"] for label in p["labels"]}
        and _same_repo_agent_branch(repo, p)
    ]
    conflict_pulls.sort(
        key=lambda p: sch.conflict_labeled_at(repo, p["number"]) or sch.parse_time(p["created_at"])
    )
    return conflict_pulls


# ── Гейт «воркер активен» — сужен до конкретной ЗАДАЧИ (issue #1032) ────
#
# Было: sch.worker_runs_active(repo) — repo-wide булево «жив ли где-то
# воркер», без разбора, НАД ЧЬЕЙ веткой. Замер 2026-09-12 (issue #1032):
# воркер занят ~89% времени, и при этом бинарном гейте КАЖДЫЙ прогон, начатый
# в занятое окно, откладывал ВСЮ очередь целиком — 15 из 28 прогонов
# `conflict-mechanical-rebase.yml` с 2026-09-08 не тронули НИ ОДНОГО PR по
# этой причине, хотя единственный активный воркер физически может работать
# только НАД ОДНОЙ веткой одновременно (worker.yml: concurrency.group=worker,
# один прогон на репозиторий) — опасность (issue #764, требование 2: force-
# with-lease поверх коммитов, которые в этот момент пушит агент) реальна
# ТОЛЬКО для ТОЙ ОДНОЙ ветки, не для остальных 18.
#
# Причина исходного репо-wide решения была честной (см. design.md активного
# чейнджа mechanical-conflict-rebase, «Гонка с агентским путём», где развилка
# теперь пересмотрена с этим замером): точная привязка «прогон именно по
# задаче ЭТОГО PR» стоит лишний сетевой вызов на PR очереди. Эта правка
# платит эту цену ТОЛЬКО когда воркер вообще активен (иначе — 0 дополнительных
# вызовов, тот же быстрый путь, что раньше): task_ref.resolve_pr_task(pull) —
# вычисление ЛОКАЛЬНОЕ (regex по имени ветки, без сети), а
# sch.run_claimed_task(repo, task_number, run_id) — тот же готовый снаряд,
# которым scheduler.py уже сопоставляет прогон ↔ задача (CLAIM_VIA-след в
# комментариях issue, claim_task.claim пишет его в первые секунды job'а,
# задолго до реальной git-работы — см. reap_stalled_worker_run.__doc__).
#
# ОКНО МОЛОДОГО ПРОГОНА (находка ai-review PR #1033, требование 2): CLAIM_VIA-
# след появляется в задаче НЕ в первые секунды статуса in_progress — до
# claim_task.claim прогон проходит выборку пула, dup-гардию, квоту и
# PAT-авторизацию (десятки секунд-минуты, см. докстринг
# sch.WORKER_CLAIM_TRACE_GRACE_MINUTES). in_progress-прогон БЕЗ следа МЛАДШЕ
# порога блокирует КОНСЕРВАТИВНО (эпистемически тот же случай, что `queued`:
# атрибуции ещё нет — а он у первой итерации этого гейта молча разрешался,
# заново открывая окно #764 ровно на том классе PR — конфликтных, — которые
# dispatch_conflict_rework и диспатчит). Тормоз с газом: отказ самоосвобождается,
# когда прогон завершился ИЛИ перешагнул порог (дальше решает след); оба пути
# короче следующего 15-минутного такта.
#
# `queued` НЕ сужается вовсе: прогон, ещё стоящий в очереди концюренси-группы,
# не успел выполнить claim_task.claim — CLAIM_VIA-следа для него физически нет,
# атрибуция невозможна ни при каком запросе. Пока прогон queued, он ничего не
# пушил и не мог — блокировка ВСЕЙ очереди в этом (редком) случае — тот же
# консервативный, безопасный отказ, не регрессия.
def worker_blocks_pr(repo: str, pull: dict, active_runs: list[dict], now: "datetime") -> bool:
    """True — этому PR НЕЛЬЗЯ двигать head в этом проходе (см. блок-комментарий
    выше). Порядок проверки от дешёвого к дорогому: queued (локально) →
    возраст прогона (локально, sch.run_age_minutes) → CLAIM_VIA-след (сетевой
    вызов на каждую задачу, только для прогонов старше порога). `active_runs` —
    прод-форма из sch.active_worker_runs (единственное место правды фетча,
    находка ai-review PR #1033: дублированный фетч-цикл здесь расходился бы
    с scheduler тихо). Ветка PR без резолвящейся задачи (task_ref.resolve_pr_task
    вернул None — не должно случаться: conflict_queue уже фильтрует agent/-ветки
    через _same_repo_agent_branch, но не гадаем при расхождении) и прогон с
    нечитаемым id/возрастом (не прод-форма ответа GitHub) — консервативный
    блок, не тихое разрешение."""
    if not active_runs:
        return False
    if any((run.get("status") or "") == "queued" for run in active_runs):
        return True  # атрибуция невозможна — claim ещё не мог случиться
    in_progress = [run for run in active_runs if (run.get("status") or "") == "in_progress"]
    if not in_progress:
        return False
    task_number = sch.task_ref.resolve_pr_task(pull)
    if task_number is None:
        return True
    for run in in_progress:
        run_id = run.get("id")
        age = sch.run_age_minutes(run, now)
        if run_id is None or age is None or age < sch.WORKER_CLAIM_TRACE_GRACE_MINUTES:
            return True  # молод (или нечитаемая прод-форма): след мог ещё не появиться
        if sch.run_claimed_task(repo, task_number, run_id):
            return True
    return False


def process_pull(repo: str, pull: dict, repo_dir: Path) -> str:
    """Один PR — см. докстринг модуля для полного разбора исходов."""
    number = pull["number"]
    head_ref = (pull.get("head") or {}).get("ref")
    if not head_ref:
        return "infra-error: PR без head.ref"
    try:
        now = datetime.now(timezone.utc)
        active_runs = sch.active_worker_runs(repo, now)
        if worker_blocks_pr(repo, pull, active_runs, now):
            return "worker-running"
        running = review_labels.other_active_ai_review_runs(
            repo, number, exclude_run_id=None, gh_func=sch.gh)
        if running:
            return "ai-review-running"
        outcome = attempt_rebase(repo_dir, head_ref)
        if outcome == "conflict":
            return "conflict"
        # issue #897, восстановлено после случайной потери в первой итерации
        # #1032 (находка ai-review PR #1033, требование 1): перенос рукописных
        # шагов гвардии в каталог — ПОСЛЕ attempt_rebase (оба исхода
        # "resolved"/"resolved-additive" — working tree на перебазированной
        # ветке), ДО push_rebased; сбой переноса исход не ухудшает (см.
        # докстринг migrate_guard_steps_if_needed).
        warning = migrate_guard_steps_if_needed(repo_dir)
        if warning:
            print(f"::warning::PR #{number}: {warning}")
        push_rebased(repo_dir, head_ref)
        return outcome  # "resolved" или "resolved-additive"
    except RuntimeError as error:
        # GitError (git-слой) и обычный RuntimeError (sch.gh — сетевой/API
        # сбой) — один и тот же бюджет «не по конфликту» (AGENTS.md, «fail
        # loud, не гадать»): различать причину дальше здесь не нужно, важно
        # только не спутать её со структурным конфликтом.
        return f"infra-error: {error}"


def run(repo: str, repo_dir: Path) -> tuple[list[str], dict[int, str]]:
    pulls = sch.open_pulls(repo)
    queue = conflict_queue(repo, pulls)
    outcomes: dict[int, str] = {}
    resolved = additive_resolved = conflicted = ai_deferred = worker_busy = failed = 0
    lines: list[str] = []
    for pull in queue:
        number = pull["number"]
        outcome = process_pull(repo, pull, repo_dir)
        outcomes[number] = outcome
        if outcome == "resolved":
            resolved += 1
            lines.append(
                f"✅ PR #{number}: git rebase origin/main сошёлся механически, "
                "ветка обновлена и запушена (--force-with-lease); снятие метки "
                "`conflict` — за mark_conflicts следующим тактом"
            )
        elif outcome == "resolved-additive":
            additive_resolved += 1
            lines.append(
                f"🧩 PR #{number}: конфликт сведён автоматически (обе стороны "
                "независимо дописали новый элемент в один список/реестр, "
                "additive_conflict_merge, issue #1032) — ветка обновлена и "
                "запушена (--force-with-lease); синтаксис и затронутые тесты "
                "перепроверены ДО пуша"
            )
        elif outcome == "conflict":
            conflicted += 1
            lines.append(
                f"🔀 PR #{number}: рёбейз структурно уткнулся в конфликт — "
                "остаётся кандидатом агентского пути (dispatch_conflict_rework)"
            )
        elif outcome == "ai-review-running":
            ai_deferred += 1
            lines.append(f"⏸️ PR #{number}: по нему летит ai-review.yml — head не двигаю в этом проходе")
        elif outcome == "worker-running":
            worker_busy += 1
            lines.append(
                f"⏸️ PR #{number}: воркер (worker.yml) активен над ЭТОЙ ЖЕ задачей "
                "(либо прогон ещё queued/молод — атрибуции ещё нет) — head не "
                "двигаю в этом проходе, чтобы не сжечь агентскую попытку (#764)"
            )
        else:
            failed += 1
            lines.append(f"⚠️ PR #{number}: {outcome} — не конфликт, лечится отдельно")
    lines.insert(
        0,
        f"Механический ребейз конфликтов: {len(queue)} PR в очереди, "
        f"{resolved} сошлись механически, {additive_resolved} сведены "
        f"аддитивным слиянием, {conflicted} остаются конфликтом "
        f"(агентский путь), {ai_deferred} отложены (ai-review), {worker_busy} "
        f"отложены (воркер занят), {failed} инфраструктурных сбоев",
    )
    return lines, outcomes


def _require_ci_environment() -> None:
    """Отказ вне CI (issue #764, находка приёмки): ensure_clean_repo
    безусловно делает `git rebase --abort`, `git reset --hard` и
    `git clean -fd` над repo_dir — в job'е с actions/checkout это безопасно
    (дерево всегда свежее), но main() берёт repo_dir из GITHUB_WORKSPACE ИЛИ
    Path.cwd(), а докстринг модуля приглашает запускать его вручную. Без
    этой проверки случайный ручной запуск (например, локальная отладка с
    экспортированным GITHUB_REPOSITORY) снёс бы незакоммиченные правки
    человека и его незавершённый рёбейз с уже решёнными конфликтами —
    приёмка воспроизвела это живьём.

    Одного GITHUB_ACTIONS недостаточно: .githooks/pre-commit опирается на ТУ
    ЖЕ переменную РОВНО НАОБОРОТ (её наличие ОТКЛЮЧАЕТ гвардию хука) и сам
    называет риск, что GITHUB_ACTIONS=true утечёт в локальное окружение
    (например, при работе с `gh`/`act`) — доверять одному этому признаку
    здесь означало бы наследовать тот же риск. GITHUB_RUN_ID — второй,
    независимый признак: его выставляет только сам раннер Actions на старте
    job'а, руками его никто не экспортирует."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or not os.environ.get("GITHUB_RUN_ID"):
        raise RuntimeError(
            "mechanical_rebase.main() отказывается запускаться вне GitHub Actions "
            "(нужны ОБА признака: GITHUB_ACTIONS=true и GITHUB_RUN_ID) — "
            "ensure_clean_repo внутри этого модуля безусловно выполняет над "
            "repo_dir `git rebase --abort`, `git reset --hard` и `git clean -fd`, "
            "что снесёт незакоммиченные правки и незавершённый рёбейз в текущем "
            "рабочем дереве. Запускай только через .github/workflows/"
            "conflict-mechanical-rebase.yml."
        )


def main() -> int:
    _require_ci_environment()
    repo = os.environ["GITHUB_REPOSITORY"]
    repo_dir = Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd())
    lines, outcomes = run(repo, repo_dir)
    sch.summary(lines)
    # issue #764, находка ревью, требование 5: раньше эта функция ВСЕГДА
    # возвращала 0 — 25 из 25 "infra-error" (например, все PR очереди упёрлись
    # в один и тот же сбой git identity, находка 1) давали зелёный job, а
    # `failure_watch` (pulse_guard.py) читает только conclusion=="failure"/
    # "timed_out" запуска — запись в WATCHED_WORKFLOWS страховала бы падение
    # ПРОЦЕССА, но не «прошёл и не сделал ничего». Полный отказ очереди —
    # ненулевой исход, чтобы это стало видно тем же механизмом.
    if outcomes and failed_count(outcomes) == len(outcomes):
        print(
            "::error::mechanical-rebase: все PR очереди ("
            f"{len(outcomes)}) завершились инфраструктурной ошибкой — 0 обработано",
        )
        return 1
    return 0


def failed_count(outcomes: dict[int, str]) -> int:
    return sum(1 for outcome in outcomes.values() if outcome.startswith("infra-error"))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"::error::mechanical-rebase: {error}")
        sys.exit(1)
