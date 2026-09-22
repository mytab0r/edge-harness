#!/usr/bin/env python3
"""Шаг workflow, читающий GitHub API, обязан стоять за гейтом квоты (#1437).

Класс, названный одной фразой: **предохранитель по квоте `GITHUB_TOKEN`
прикручен к отдельным дорогим шагам вручную, а не ко всем читателям API —
и незакрытый шаг красит чужой PR причиной, к которой тот не относится.**

Живой случай, прогон 35715554412 (PR #1449, job `test`, 2026-09-22):

    10:24:21  rate_guard: квота ок (5000/5000)        ← гейт #454 отработал
    10:27:02  ##[error]repo_invariants: … HTTP 403    ← и не спас
    10:29:54  ##[error]upstream-namespace-collision: … HTTP 403

Второй отказ пришёл из шага, который про гейт не знал вовсе: гейт стоял в
СЕРЕДИНЕ job'а, ниже трёх шагов, которые сами ходят в API. `test` —
обязательная проверка, и покраснел он на исчерпанной общей квоте, а не на
диффе автора.

Что проверяет эта гвардия: каждый шаг любого workflow, которому передан
`GH_TOKEN: ${{ github.token }}`, либо стоит за условием
`steps.<id>.outputs.skip != 'true'` гейта `rate_guard.py` из своего же job'а,
либо перечислен в `ALLOWED_UNGATED_API_STEPS` с причиной. Реестр —
измеренный долг, а не разрешение: он существует, чтобы долг был виден
числом и не рос молча (тот же приём, что `ALLOWED_CURL_STUB_WITHOUT_OUTFILE`
и соседние реестры репозитория).

Газ (AGENTS.md, «тормоз без газа не принимается»): шаг снимается из реестра
тем же PR, который ставит ему `if: steps.quota.outputs.skip != 'true'` и
добавляет гейт в его job, если гейта там ещё нет. Новая запись в реестре
обязана нести причину — пустая строка не проходит.

Запуск: python scripts/lib/api_quota_gate_guard.py
Тесты:  python -m pytest scripts/lib/test_api_quota_gate_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import ast
import re
import sys

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Признак «шаг ходит в GitHub API нашим общим бюджетом»: ему передан токен
# job'а. Секретный PAT (`secrets.GH_PIPELINE_PAT`) — ДРУГОЙ бюджет (личный
# 5000/час, не общий 1000/час репозитория), и под это правило не попадает:
# смешивать их значило бы гасить шаги из-за чужой квоты.
# Орфографий у одного и того же значения несколько: `${{ github.token }}`,
# `${{github.token}}`, `${{ secrets.GITHUB_TOKEN }}` — GitHub Actions
# трактует их одинаково, а сравнение посимвольно видело бы только первую
# (находка ai-review PR #1451, без блокировки: гвардия печатала бы зелёное
# «все закрыты» и молча пропускала шаг). Сравниваем по НОРМАЛИЗОВАННОМУ
# выражению внутри `${{ }}`, без пробелов и регистра.
_JOB_TOKEN_EXPRESSIONS = frozenset({"github.token", "secrets.github_token"})

# Имя переменной окружения тоже не одно: `GH_TOKEN` понимает gh CLI,
# `GITHUB_TOKEN` — и gh, и actions/github-script, и любой свой скрипт.
_TOKEN_ENV_NAMES = ("GH_TOKEN", "GITHUB_TOKEN")

# Оставлено для читаемости сообщений и как канонический вид значения.
JOB_TOKEN_VALUE = "${{ github.token }}"


def _is_job_token(value: object) -> bool:
    """True — значение раскрывается в токен job'а, при любой орфографии."""
    text = str(value or "").strip()
    if not (text.startswith("${{") and text.endswith("}}")):
        return False
    return text[3:-2].replace(" ", "").lower() in _JOB_TOKEN_EXPRESSIONS

# Признак самого гейта — вызов rate_guard.py в `run` шага.
GATE_SCRIPT = "rate_guard.py"

# Измеренный долг на 2026-09-22: 29 шагов в 17 workflow читают API общим
# бюджетом мимо гейта (замер — эта же гвардия на дереве PR, не оценка на
# глаз). Ключ — «<файл>::<job>::<имя шага>», значение — ПОЧЕМУ запись ещё
# здесь; пустая причина не проходит.
#
# Причин ровно три, и они РАЗНЫЕ по цене закрытия — одна общая формулировка
# смазала бы именно то, что решает порядок разбора:
#   _GATE_EXISTS_STEP_NOT_BEHIND_IT — гейт в workflow есть, шаг мимо него
#       (7 записей, самый дешёвый долг);
#   _SINGLE_STEP_WORKFLOW — гейта в job'е нет вовсе (21 запись);
#   _GUARD_CATALOGUE — гасить целиком неверно по существу (1 запись).
_SINGLE_STEP_WORKFLOW = (
    "workflow-одиночка: гейта квоты в job'е нет вовсе, шаги реагируют на событие "
    "и делают единицы вызовов. Участие именно этих прогонов в исчерпании общего "
    "бюджета НЕ измерено — заводить гейт до замера значит чинить незамеренное "
    "(AGENTS.md). Разбирается отдельной узкой задачей на workflow, со своим замером"
)

_GATE_EXISTS_STEP_NOT_BEHIND_IT = (
    "гейт квоты в этом workflow УЖЕ есть, но конкретно этот шаг за ним не стоит — "
    "узкий и дешёвый долг: нужно одно условие `if` и проверка, что пропуск шага "
    "не ломает смысл прогона (у ai-review/orchestra пропуск меняет вердикт и такт, "
    "поэтому не делается вслепую этим PR)"
)

_GUARD_CATALOGUE = (
    "гасить целиком нельзя: шаг гоняет ВЕСЬ каталог scripts/ci/guards, где "
    "подавляющее большинство гвардий в API не ходит, и пропуск по чужой квоте снял "
    "бы проверки, к квоте отношения не имеющие. ИСПРАВЛЕНО #1004: прежняя редакция "
    "этой причины добавляла «GH_TOKEN передан на будущее, потребителей нет» — я "
    "процитировал комментарий repo-ci.yml вместо замера, и он был ложным. "
    "Потребителей четыре, и закрыты они не гейтом шага, а обёрткой "
    "rate_guard.run_guard_main вокруг собственного main каждого; отсутствие "
    "обёртки у нового потребителя красит catalogue_problems ниже"
)

ALLOWED_UNGATED_API_STEPS: dict[str, str] = {
    'ai-review.yml::review::Нужен ли дорогой прогон (сверка отпечатка диффа)':
        _GATE_EXISTS_STEP_NOT_BEHIND_IT,
    'ai-review.yml::review::Сбор фактов и промпт (доверенный шаг)':
        _GATE_EXISTS_STEP_NOT_BEHIND_IT,
    'ai-review.yml::verdict::Вердикт: комментарий + метка':
        _GATE_EXISTS_STEP_NOT_BEHIND_IT,
    'ai-review.yml::verdict::Разбудить оркестратор (#297): вердикт AI готов, не ждать расписания':
        _GATE_EXISTS_STEP_NOT_BEHIND_IT,
    'branch-protection-watch.yml::watch::Сверить защиту main против EXPECTED_* и сигналить при ослаблении':
        _SINGLE_STEP_WORKFLOW,
    'checklist-tail-triage.yml::triage::Наследование области хвостом чеклиста ревью':
        _SINGLE_STEP_WORKFLOW,
    'dependabot-alert-watch.yml::watch::Наблюдатель алертов Dependabot':
        _SINGLE_STEP_WORKFLOW,
    'deploy-dsh-edge.yml::deploy::Канарейка runner-bridge (#390/#945)':
        _SINGLE_STEP_WORKFLOW,
    'deploy-dsh-edge.yml::deploy::Коллизия имён с апстримом (namespace/label/inject/slot)':
        _SINGLE_STEP_WORKFLOW,
    'deploy-dsh-edge.yml::deploy::Скачать плагины и сверить sha256':
        _SINGLE_STEP_WORKFLOW,
    'deploy-worker.yml::deploy::Эскалация автооткота (#120/Telegram)':
        _SINGLE_STEP_WORKFLOW,
    'dsh-edge-pr-smoke.yml::pr-smoke::Скачать плагины и сверить sha256':
        _SINGLE_STEP_WORKFLOW,
    'inbox-issue.yml::create::Issue из директивы инбокса':
        _SINGLE_STEP_WORKFLOW,
    'merge-health-watch.yml::watch::Регрессия worker_success_rate, привязанная к слиянию':
        _SINGLE_STEP_WORKFLOW,
    'orchestra.yml::contract::Контракт PR ↔ задача':
        _GATE_EXISTS_STEP_NOT_BEHIND_IT,
    'orchestra.yml::orchestra::Гвардия waiting:owner — задача ждёт решения владельца (#470)':
        _GATE_EXISTS_STEP_NOT_BEHIND_IT,
    'orchestra.yml::orchestra::Гвардия протухшей метки blocked (#334)':
        _GATE_EXISTS_STEP_NOT_BEHIND_IT,
    'owner-decision.yml::apply::Комментарий с решением владельца':
        _SINGLE_STEP_WORKFLOW,
    'plugin-forge.yml::forge::Интеграционный дым (локальная сборка с плагином)':
        _SINGLE_STEP_WORKFLOW,
    'plugin-forge.yml::forge::Создать/обновить релиз plugins-<id>-v<version>':
        _SINGLE_STEP_WORKFLOW,
    'plugin-forge.yml::prepare::Определить состав форжа (dispatch/push/schedule)':
        _SINGLE_STEP_WORKFLOW,
    'pr-review.yml::review::Детерминированное ревью диффа':
        _SINGLE_STEP_WORKFLOW,
    'pr-review.yml::review::Разбудить оркестратор (#297): вердикт изменился, не ждать расписания':
        _SINGLE_STEP_WORKFLOW,
    'quota-watch.yml::watch::Гейт: нужен ли реальный замер':
        _SINGLE_STEP_WORKFLOW,
    'quota-watch.yml::watch::Замер квоты (Cloudflare)':
        _SINGLE_STEP_WORKFLOW,
    'quota-watch.yml::watch::Замер лимита GitHub API':
        _SINGLE_STEP_WORKFLOW,
    'quotas.yml::quotas::Снять срез квот':
        _SINGLE_STEP_WORKFLOW,
    'repo-ci.yml::test::Каталог гвардий scripts/ci/guards — перебор (#749)':
        _GUARD_CATALOGUE,
    'secret-scan.yml::full-history::Эскалация в пул задач при падении шага (issue, без значений секрета)':
        _SINGLE_STEP_WORKFLOW,
}


def _steps_of(job: dict) -> list[dict]:
    steps = job.get("steps")
    return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []


def _uses_job_token(step: dict, job: dict, workflow: dict) -> bool:
    """Токен job'а виден шагу и со своего `env:`, и с уровня job/workflow —
    проверять только шаг значило бы не заметить самый широкий радиус."""
    for scope in (step, job, workflow):
        env = scope.get("env") if isinstance(scope, dict) else None
        if not isinstance(env, dict):
            continue
        if any(_is_job_token(env.get(name)) for name in _TOKEN_ENV_NAMES):
            return True
    return False


def _gate_ids(job: dict) -> set[str]:
    """id шагов этого job'а, которые и есть гейт квоты."""
    return {
        step["id"] for step in _steps_of(job)
        if step.get("id") and GATE_SCRIPT in str(step.get("run", ""))
    }


def _is_gated(step: dict, gate_ids: set[str]) -> bool:
    condition = str(step.get("if", ""))
    return any(f"steps.{gate_id}.outputs.skip" in condition for gate_id in gate_ids)


def ungated_api_steps(workflows_dir: Path = WORKFLOWS_DIR) -> list[str]:
    """Ключи «<файл>::<job>::<имя шага>» всех шагов, читающих API токеном
    job'а мимо гейта. Сам шаг-гейт в список не попадает: он и есть проверка,
    гасить его собственным выводом — замкнуть гейт на себя."""
    found: list[str] = []
    # Оба суффикса: GitHub Actions грузит workflow и из `*.yml`, и из
    # `*.yaml` (класс #146/#635, гвардия scripts/lib/workflow_glob_suffix_
    # guard.py — она же эту дыру здесь и нашла). Скан по одному суффиксу
    # сделал бы `evil.yaml` с незакрытым чтением API невидимым для гвардии,
    # которая ровно за это и отвечает.
    workflow_files = sorted(
        list(workflows_dir.glob("*.yml")) + list(workflows_dir.glob("*.yaml")))
    for path in workflow_files:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        jobs = workflow.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            gate_ids = _gate_ids(job)
            for step in _steps_of(job):
                if GATE_SCRIPT in str(step.get("run", "")):
                    continue
                if not _uses_job_token(step, job, workflow):
                    continue
                if _is_gated(step, gate_ids):
                    continue
                found.append(f"{path.name}::{job_name}::{step.get('name', '<без имени>')}")
    return found


# ── Вторая поверхность того же класса: гвардии каталога (#1004) ──────────────
#
# Шаг «Каталог гвардий scripts/ci/guards — перебор» гейтом квоты НЕ гасится, и
# это правильно: он гоняет весь каталог, где подавляющее большинство гвардий в
# API не ходит. Но ЧЕТЫРЕ ходят, и до #1004 они падали трейсбеком на
# исчерпанном бюджете — то есть красили чужой PR чужой причиной уже ПОСЛЕ
# того, как гейт корректно пропустил дорогие шаги (живой случай: прогон
# 35727716021, PR #1458, `decision-doc-numbering-guard`, HTTP 403).
#
# Обоснование «ни одна гвардия каталога не читает gh api», стоявшее в
# run_guards.sh и в repo-ci.yml, было ЛОЖНЫМ (issue #1004 назвала одного
# потребителя, замер 2026-09-22 нашёл четырёх) — и я перенёс эту ложную
# посылку в реестр выше, процитировав комментарий вместо замера. Эта проверка
# закрывает саму возможность: модуль, который читает API, обязан пропускать
# свой `main` через `rate_guard.run_guard_main`.
CATALOGUE_DIR = REPO_ROOT / "scripts" / "ci" / "guards"

# Имена вызовов, означающих «этот модуль ходит в GitHub API». Разбор — AST,
# не поиск подстроки, и это оплачено ревью PR #1460 дважды в одном месте:
#
#   * текстовый маркер `gh(` совпал с прозой комментария
#     («Оркестрация без keyword-аргументов gh()») в
#     `ci_guard_registration_guard.py`, который наружу не ходит вовсе, —
#     замер был завышен, а число «четыре» оказалось пересказом;
#   * тот же текстовый поиск НЕ видел `invariant_numbering.py`, который ходит
#     в API транзитивно: своих маркеров у него нет, вызов живёт этажом ниже.
#
# AST снимает первую половину (комментарий вызовом не является), обход
# зависимостей — вторую.
_API_CALL_NAMES = frozenset({"list_pages", "gh"})
# Имя-суффикс, а не точное равенство: живой замер на дереве этого PR показал
# `_gh_api(...)` в `pr_mutation_claim_check.py` — точный набор имён его
# пропускал, и детектор недоставал ровно так же, как текстовый до него.
_API_CALL_SUFFIX = "gh_api"
# Сама обёртка из обхода исключена, иначе получается замкнутый круг: модуль
# подключает `rate_guard`, `rate_guard` зондирует остаток бюджета своим
# `gh api` — и модуль становится «потребителем API» ровно оттого, что его
# уже починили. Замер на дереве этого PR показал круг вживую:
# `ci-guard-registration` и `mutation-claim-guard` всплыли с единственным
# достижимым модулем `rate_guard.py`. Собственный запрос обёртки к бюджету
# обработан внутри неё самой (`QuotaExhausted` → skip, rc 0) — это не
# потребитель, которого надо чинить.
_WRAPPER_MODULE = "scripts/lib/rate_guard.py"
_QUOTA_WRAPPER = "run_guard_main"
_GUARD_SCRIPT_MODULE_RE = re.compile(r"python3?\s+(scripts/[A-Za-z0-9_/]+\.py)")


def _is_gh_argv(node: ast.AST) -> bool:
    """Литерал вида `["gh", "api", …]` — вызов CLI через subprocess.

    Второй способ сходить в API, и он не ловится именем функции: зовут
    `subprocess.run`, а «gh» лежит первым элементом списка аргументов. Живой
    случай — `plugin_manager_roster_guard.py`, которого набор имён не видел."""
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return False
    head = node.elts[0]
    return isinstance(head, ast.Constant) and head.value == "gh"


def _calls_api_directly(source: str) -> bool:
    """Ходит ли модуль в GitHub API: вызовом примитива или `gh` через shell.

    Определение `def gh(...)` вызовом не является и сюда не попадает —
    иначе модуль, предоставляющий примитив, считался бы его потребителем."""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in _API_CALL_NAMES or (name or "").endswith(_API_CALL_SUFFIX):
            return True
        if any(_is_gh_argv(arg) for arg in node.args):
            return True
    return False


def _module_dependencies(source: str, repo_root: Path) -> list[str]:
    """Модули репозитория, которые этот модуль подгружает.

    Проводка здесь своя, а не `ast` по `import`: модули репозитория грузят
    друг друга через `importlib.util.spec_from_file_location(..., <путь>)`,
    и обычного `import` между ними нет — обход по `ast.Import` нашёл бы
    ноль зависимостей и молча зеленел бы ровно там, где нужен.

    Признак — строковый литерал, кончающийся на `.py`, внутри вызова
    `spec_from_file_location`. Имя файла ищется под `scripts/`; неоднозначное
    (два файла с одним именем) пропускается, и это сказано вслух, а не
    умолчано: лучше честный пробел, чем тихо взятая не та зависимость."""
    deps: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "spec_from_file_location":
            continue
        for literal in ast.walk(node):
            if not (isinstance(literal, ast.Constant) and isinstance(literal.value, str)):
                continue
            if not literal.value.endswith(".py"):
                continue
            matches = sorted((repo_root / "scripts").rglob(Path(literal.value).name))
            if len(matches) == 1:
                deps.append(matches[0].relative_to(repo_root).as_posix())
    return list(dict.fromkeys(deps))


def _api_reachable_from(
    rel: str, repo_root: Path, seen: set[str] | None = None
) -> list[str]:
    """Модули, вызывающие примитив API, достижимые из `rel` (включая сам `rel`).

    Обход транзитивный, не на один шаг: «один hop» — произвольная глубина,
    которую следующее звено молча обойдёт.

    Это ВЕРХНЯЯ ОЦЕНКА, и это сказано вслух, а не выдаётся за факт: статически
    видно «модуль загружает модуль, который умеет ходить в API», а не «на этом
    прогоне вызов случится». Замер 2026-09-22 на живом дереве показал обе
    стороны: `declared_deps.py` под заглушкой `gh`, отвечающей 403, реально
    падает трейсбеком (EXIT=1), а `deploy_workflow_registry_guard.py` — нет
    (EXIT=0), хотя оба загружают модули с вызовами API.

    Завышение принято сознательно, потому что его цена — ноль: `run_guard_main`
    ловит только отказ формы «бюджет исчерпан» и пробрасывает всё остальное,
    так что обёртка на модуле, который в API не пошёл, не делает ничего.
    Обратная ошибка (пропустить настоящего потребителя) стоит красной
    обязательной проверки на чужой причине — ровно то, ради чего заведён
    #1004."""
    seen = seen if seen is not None else set()
    if rel in seen:
        return []
    seen.add(rel)
    path = repo_root / rel
    if not path.exists() or Path(rel).name.startswith("test_") or rel == _WRAPPER_MODULE:
        return []
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    reachable = [rel] if _calls_api_directly(source) else []
    for dep in _module_dependencies(source, repo_root):
        reachable.extend(_api_reachable_from(dep, repo_root, seen))
    return sorted(dict.fromkeys(reachable))


def catalogue_modules_reading_api(
    catalogue_dir: Path = CATALOGUE_DIR, repo_root: Path = REPO_ROOT
) -> dict[str, dict[str, list[str]]]:
    """{гвардия: {модуль-точка-входа: [модули с вызовами API]}}.

    Тесты гвардий сюда не входят намеренно: они ходят по заглушкам и бюджет
    не тратят."""
    found: dict[str, dict[str, list[str]]] = {}
    for script in sorted(catalogue_dir.glob("*.sh")):
        entries: dict[str, list[str]] = {}
        for rel in dict.fromkeys(
            _GUARD_SCRIPT_MODULE_RE.findall(script.read_text(encoding="utf-8"))
        ):
            reachable = _api_reachable_from(rel, repo_root)
            if reachable:
                entries[rel] = reachable
        if entries:
            found[script.stem] = entries
    return found


def _has_quota_wrapper(source: str) -> bool:
    """True — модуль реально использует обёртку, а не упоминает её прозой.

    Проверка по сырому тексту ловила имя обёртки в докстринге: модуль,
    читающий API без обёртки, оставался зелёным, пока проза упоминала
    `run_guard_main` (поймано исполнением мутации, не чтением исходника:
    обёртка снята до вызова в `__main__` — `catalogue_problems` молчал).
    Тот же класс «проза вместо кода», что у маркеров чтения API выше.
    AST видит только код: имя или атрибут `run_guard_main`, как в
    `_rate_guard.run_guard_main(...)`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Модуль, который не парсится, не исполняется; требовать обёртку от
        # него — пере-флажок в сторону строгого, не тихий пропуск.
        return _QUOTA_WRAPPER in source
    return any(
        (isinstance(node, ast.Attribute) and node.attr == _QUOTA_WRAPPER)
        or (isinstance(node, ast.Name) and node.id == _QUOTA_WRAPPER)
        for node in ast.walk(tree)
    )


def catalogue_problems(
    catalogue_dir: Path = CATALOGUE_DIR, repo_root: Path = REPO_ROOT
) -> list[str]:
    """Точка входа гвардии может дойти до API, но не обёрнута `run_guard_main`.

    Требование — к ТОЧКЕ ВХОДА, первому звену: именно её запускает гвардия, и
    именно её трейсбек красит обязательную проверку."""
    problems: list[str] = []
    for guard_name, entries in catalogue_modules_reading_api(catalogue_dir, repo_root).items():
        for entry, reachable in entries.items():
            text = (repo_root / entry).read_text(encoding="utf-8")
            if _has_quota_wrapper(text):
                continue
            via = ", ".join(m for m in reachable if m != entry)
            where = f" (через {via})" if via else ""
            problems.append(
                f"{guard_name} запускает {entry}, откуда достижим вызов GitHub API{where}, "
                f"но его `main` не обёрнут `rate_guard.{_QUOTA_WRAPPER}` — при исчерпанном "
                f"бюджете гвардия упадёт трейсбеком и покрасит обязательную "
                f"проверку чужой причиной (#1004)"
            )
    return problems


def check(workflows_dir: Path = WORKFLOWS_DIR) -> list[str]:
    """Нарушения: незакрытый шаг без записи в реестре, запись без причины,
    мёртвая запись (шага больше нет — реестр не должен переживать
    переименование молча, это тот же класс, что #891/#893)."""
    problems: list[str] = []
    ungated = ungated_api_steps(workflows_dir)

    for key in ungated:
        if key not in ALLOWED_UNGATED_API_STEPS:
            problems.append(
                f"{key}: шаг читает GitHub API токеном job'а мимо гейта квоты. "
                f"Поставь `if: steps.<id гейта>.outputs.skip != 'true'` (и сам гейт "
                f"`{GATE_SCRIPT}` в этот job, если его там нет) либо внеси в "
                f"ALLOWED_UNGATED_API_STEPS с причиной")

    for key, reason in ALLOWED_UNGATED_API_STEPS.items():
        if not str(reason).strip():
            problems.append(f"{key}: запись в ALLOWED_UNGATED_API_STEPS без причины")
        if key not in ungated:
            problems.append(
                f"{key}: мёртвая запись в ALLOWED_UNGATED_API_STEPS — такого незакрытого "
                f"шага в .github/workflows/ больше нет (шаг закрыт гейтом, переименован "
                f"или удалён). Убери запись")
    problems.extend(catalogue_problems())
    return problems


def main() -> int:
    problems = check()
    if problems:
        for problem in problems:
            print(f"::error::api-quota-gate: {problem}")
        return 1
    print(
        f"api-quota-gate: все шаги, читающие API токеном job'а, закрыты гейтом квоты "
        f"или перечислены с причиной ({len(ALLOWED_UNGATED_API_STEPS)} записей долга)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
