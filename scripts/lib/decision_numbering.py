#!/usr/bin/env python3
"""Номер ADR/research-документа не назначается вручную без арбитра (#1078).

Класс (тот же, что #904 у номеров инвариантов `repo_invariants.py`, и #749 у
регистрации гвардий ДО его починки): `docs/decisions/NNNN-*.md` и
`docs/research/NN-*.md` — общий числовой ресурс, изменяемый вручную,
конкурирует между независимыми PR без арбитра. Живые случаи (#1078):
PR #1035 занял номер 0017, уже слитый в main другим ADR
(`0017-dsh-edge-pr-smoke-local-worker.md`); независимый канал держал
незакоммиченный `0015-parallel-work-on-shared-code-boundaries.md`, номер
0015 в main занят другим ADR, а тот же смысл уже слит как 0016.

Не в объёме: #904 (номера инвариантов `repo_invariants.py`) — тот же класс,
другой носитель (ручные int-константы в одном файле, не каталог
пронумерованных файлов), уже заведён отдельной задачей.

Устройство (design.md change `decision-doc-numbering-guard` — путь не называем
дословно: `archive_complete_changes.py` переносит завершённые change в
`openspec/changes/archive/<id>/`, дословный путь тут же стал бы мёртвым;
ищи по имени change в `openspec/changes/` или `openspec/changes/archive/`):

  - `NUMBERED_ROOTS` — единственное место, знающее оба носителя одного
    класса и ширину номера каждого.
  - `parse_numbered_files`/`next_free_number`/`find_number_collisions` —
    чистые функции без сети и без git, тестируемые мутацией (см.
    `test_decision_numbering.py`).
  - `collect_sources_from_refs` — РЕАЛЬНЫЙ git (`fetch` + `ls-tree`), без
    GitHub API: получает {имя_источника: набор файлов}, фетча каждый ref
    (ветка main, ветка каждого открытого PR) в изолированное пространство
    имён `refs/decision-numbering/*`, не трогая обычные ветки рабочего
    дерева. Тестируется на настоящем временном git-репозитории (см.
    test_decision_numbering.py, по образцу test_mechanical_rebase.py) —
    AGENTS.md, «поведенческий тест находит то, чего структурный не видит».
  - `collect_sources` — тонкая обвязка: спрашивает у `gh api`, какие PR
    сейчас открыты и на какую ветку указывают (только метаданные, без
    файлов PR), передаёт эти ссылки в `collect_sources_from_refs`.
  - `cmd_check` красит ТОЛЬКО тот прогон, чья ветка реально ВИНОВНА в
    найденной коллизии (`self_source_name`) — не любую коллизию, которая
    вообще где-то есть в очереди открытых PR. Живой случай, найденный живым
    прогоном на mytab0r/edge-harness (2026-09-13): PR #944 независимо занял
    номер 0017, уже слитый в main другим ADR; без сужения по self это
    красило бы `test` (обязательную проверку) у КАЖДОГО из 20+ посторонних
    открытых PR, не только у #944 — AGENTS.md, «тормоз без газа не
    принимается»: только виновник обязан чинить, остальные не блокируются
    чужим долгом. «Реально участвует» ≠ «виновен» — для main это разные
    условия (issue #1200): main виновен, только если у него самого сидит
    дубль номера (`main_has_intra_duplicate`), не просто потому, что он
    входит в множество участников коллизии.

CLI:
  python scripts/lib/decision_numbering.py next docs/decisions
      печатает следующий свободный номер (main ∪ все открытые PR).
  python scripts/lib/decision_numbering.py check [docs/decisions ...]
      без аргументов — оба корня из NUMBERED_ROOTS; печатает найденные
      коллизии и завершается кодом 1, если хоть одна есть.
Оба читают `GITHUB_REPOSITORY` (уже экспортируется раннером Actions) и
работают в git-дереве текущей рабочей директории (в CI — checkout PR)."""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import os
import re
import subprocess
import sys

# Импорт соседнего модуля по файлу (паттерн claim_task/review_labels: скрипты
# этого репозитория запускаются как файлы, не как пакет).
_RL_SPEC = importlib.util.spec_from_file_location(
    "review_labels", Path(__file__).resolve().parent / "review_labels.py")
_review_labels = importlib.util.module_from_spec(_RL_SPEC)
_RL_SPEC.loader.exec_module(_review_labels)  # type: ignore[union-attr]

# check_result (issue #1096/#1110) — три исхода проверки, не два: см.
# check_decision_doc_number_collisions ниже, готовит #1090 в целевой форме.
_CR_SPEC = importlib.util.spec_from_file_location(
    "check_result", Path(__file__).resolve().parent / "check_result.py")
check_result = importlib.util.module_from_spec(_CR_SPEC)
_CR_SPEC.loader.exec_module(check_result)  # type: ignore[union-attr]

# Корень → ширина номера (кол-во цифр с ведущими нулями). Единственное место
# правды: любой новый носитель того же класса «сквозная нумерация markdown
# под общим каталогом» регистрируется здесь, а не отдельной копией регэкспа.
#
# Честная граница (backlog, найдено ai-review PR #1082, не блокирует мерж):
# `numbered_filename_re` матчит РОВНО `width` цифр — файл с числом цифр
# БОЛЬШЕ ширины (например "100-x.md" при width=2) не совпадает с regex
# вовсе и молча выпадает из поля зрения гвардии, вместо явной ошибки.
# Не подтверждено, что это стоит чинить сейчас: на 2026-09-13 `docs/
# decisions` — 19 файлов (нужно 10000, чтобы переполнить ширину 4), `docs/
# research` — 13 (нужно 100, чтобы переполнить ширину 2) — оба на порядки
# дальше переполнения, чем текущий разговор о доводке.
NUMBERED_ROOTS: dict[str, int] = {
    "docs/decisions": 4,
    "docs/research": 2,
}

# Изолированное пространство имён для служебных фетчей — не пересекается с
# обычными ветками рабочего дерева (тот же приём, что `refs/locks/*` у
# claim_task: собственный префикс, не видимый обычным `git fetch --prune`).
_FETCH_NAMESPACE = "refs/decision-numbering"


def numbered_filename_re(width: int) -> "re.Pattern":
    return re.compile(rf"^(\d{{{width}}})-.+\.md$")


def parse_numbered_files(paths, root: str, width: int) -> dict[str, list[str]]:
    """paths — репо-относительные POSIX-пути. Возвращает {номер: [имена_файлов]}
    для файлов ПРЯМО под `root` (docs/research/data/*.md — не «прямо под»,
    игнорируется: поддиректория не участвует в этой нумерации).

    Значение — СПИСОК, не строка (находка ai-review PR #1082): прежняя форма
    `dict[str, str]` схлопывала два файла с ОДНИМ номером внутри одного и
    того же источника — `result[номер] = имя` вторым присваиванием тихо
    затирала первое. Живой класс, который это прячет: main САМ может нести
    два файла под одним номером (после слияния двух PR, независимо занявших
    один свободный номер разными именами — ни один git-конфликт этого не
    покажет, пути разные). Раньше `find_number_collisions` в этом случае
    видел `{'0017': <последний>}` и молчал; список сохраняет оба имени, и
    `find_number_collisions` (ниже) считает дублей внутри ОДНОГО источника
    точно так же, как дублей между разными источниками — тот же признак."""
    pattern = numbered_filename_re(width)
    prefix = root.rstrip("/") + "/"
    result: dict[str, list[str]] = {}
    for path in paths:
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        if "/" in rest:
            continue
        match = pattern.match(rest)
        if not match:
            continue
        result.setdefault(match.group(1), []).append(rest)
    return result


def next_free_number(existing_numbers, width: int) -> str:
    """`existing_numbers` — любой iterable строковых номеров. Пустой набор →
    "1", дополненный нулями по ширине."""
    numbers = [int(n) for n in existing_numbers]
    candidate = (max(numbers) + 1) if numbers else 1
    return str(candidate).zfill(width)


def find_number_collisions(sources: dict[str, dict[str, list[str]]]) -> list[dict]:
    """`sources`: {имя_источника: {номер: [имена_файлов]}} — например
    {"main": {...}, "PR #1035": {...}, "PR #1040": {...}}.

    Коллизия — номер, под который (в разных источниках ИЛИ внутри ОДНОГО
    источника — см. докстринг `parse_numbered_files`) подставлены РАЗНЫЕ
    имена файлов. Тот же номер + то же имя в main и в PR, который лишь
    редактирует существующий файл, — НЕ коллизия (одна запись имени). Два PR
    добавляют файл с ОДИНАКОВЫМ именем под одним номером — тоже НЕ коллизия
    этого инварианта: это add/add-конфликт одного пути, который git
    обнаружит сам при ребейзе, вторая гвардия здесь не нужна (design.md,
    раздел «Устройство»)."""
    by_number: dict[str, dict[str, list[str]]] = {}
    for source_name, mapping in sources.items():
        for number, filenames in mapping.items():
            for filename in filenames:
                by_number.setdefault(number, {}).setdefault(filename, []).append(source_name)

    violations = []
    for number, by_filename in sorted(by_number.items()):
        if len(by_filename) <= 1:
            continue
        violations.append({
            "number": number,
            "occurrences": [
                {"filename": filename, "sources": sorted(set(srcs))}
                for filename, srcs in sorted(by_filename.items())
            ],
        })
    return violations


def format_violation(root: str, violation: dict) -> str:
    parts = "; ".join(
        f"{occ['filename']} ({', '.join(occ['sources'])})"
        for occ in violation["occurrences"]
    )
    return f"{root}: номер {violation['number']} занят разными файлами — {parts}"


def main_has_intra_duplicate(violation: dict) -> bool:
    """True, если ВНУТРИ самого main реально сидит дубль номера — минимум ДВА
    разных имени файла, каждое из которых пришло ИСКЛЮЧИТЕЛЬНО из main
    (`sources == ["main"]`). Не то же самое, что `involved == {"main"}`
    (блокирующая находка ai-review PR #1202): множество участников коллизии
    может быть `{"main"}` при одном-единственном мэйн-файле — но также может
    быть `{"main", "PR #N"}`, когда main НЕСЁТ настоящий дубль (два своих
    файла под тем же номером — живой класс #1078, состояние «после слияния
    двух независимо коллидировавших PR») И одновременно ещё висит открытый
    сторонний PR с третьим файлом под тем же номером. Старое условие
    (`involved != {"main"}`) в этом случае молчало — маскируя дубль ВНУТРИ
    main ровно в тот момент, когда рядом гонка за номер самая активная.
    Проверяем по `occurrences`, не по `involved`: у каждого имени файла своя
    запись `sources`, и два разных имени, оба целиком из main, — и есть
    дубль внутри main, независимо от того, сколько ещё сторонних источников
    претендует на тот же номер."""
    return sum(1 for occ in violation["occurrences"] if occ["sources"] == ["main"]) >= 2


# ── IO: git (реальный fetch + ls-tree, без GitHub API) ───────────────────────

class GitError(RuntimeError):
    pass


def run_git(*args: str, cwd=None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} упал: {result.stderr.strip()}")
    return result.stdout


def fetch_refs(refs: dict[str, str], cwd=None) -> None:
    """`refs`: {локальное_имя: удалённая_ветка} — например {"main": "main",
    "PR #1035": "agent/1035-pr-conflict-triage"}. Один вызов `git fetch`,
    каждая ветка приземляется под `_FETCH_NAMESPACE/<локальное_имя>`
    (санитизировано — # и пробелы недопустимы в имени рефа).

    `--depth 1` (находка ai-review PR #1082, backlog): вся логика этого
    модуля с тех пор, как `added_files_under_root` перестал использовать
    тройную точку (та же правка, что убрала зависимость от merge-base),
    сравнивает ДВА ДЕРЕВА напрямую (`git diff A B`/`git ls-tree`) — история
    коммитов ей не нужна вообще, нужен только снимок на кончике ветки.
    Раньше каждый прогон тянул ПОЛНУЮ историю main и каждого открытого PR —
    неоправданная сетевая/CPU-цена на каждый CI-прогон при живом бюджете
    репозитория (AGENTS.md, «всё работает на бесплатных планах»)."""
    if not refs:
        return
    refspecs = [
        f"{remote}:{_local_ref(local)}" for local, remote in refs.items()
    ]
    run_git("fetch", "--quiet", "--force", "--depth", "1", "origin", *refspecs, cwd=cwd)


def _local_ref(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    return f"{_FETCH_NAMESPACE}/{safe}"


def ls_tree_files_under_root(local_name: str, root: str, cwd=None) -> list[str]:
    out = run_git("ls-tree", "-r", "--name-only", _local_ref(local_name), "--", root, cwd=cwd)
    return [line for line in out.splitlines() if line.strip()]


def added_files_under_root(base_local_name: str, local_name: str, root: str, cwd=None) -> list[str]:
    """Файлы, которые `local_name` ДОБАВИЛ относительно `base_local_name`
    (git diff --diff-filter=AR).

    ДВА отдельных рефа, БЕЗ тройной точки (находка ai-review PR #1082,
    блокирующая: воспроизведена end-to-end на настоящем shallow-клоне) —
    тройная точка (`A...B`) требует существующего merge-base в истории
    (`git merge-base A B`), а `actions/checkout@v7` в `repo-ci.yml` без
    `fetch-depth: 0` даёт shallow-клон глубины 1: PR-ветка после `fetch_refs`
    остаётся shallow (объекты головы есть, история дальше — нет), у нашего
    `main`, зафетченного отдельно, полная история — но ОБЩЕГО предка между
    ними взять неоткуда, `git diff A...B` падает `fatal: ... no merge base`,
    exit 128 → `GitError` → гвардия красит `test` на КАЖДОМ PR репозитория,
    не только виновника (ровно тот тормоз без газа, которого дизайн обещал
    не допускать, только для другого случая, чем self_source_name решает).
    Простой `git diff A B` (без диапазона) — прямое сравнение ДВУХ ДЕРЕВЬЕВ,
    не историй: не требует общего предка вообще, работает на несвязанных
    графах коммитов, и результат при `--diff-filter=A` тот же самый, что и
    был нужен — статус `D` (файл есть в main, отсутствует в `local_name`,
    потому что main добавил его ПОСЛЕ того, как PR ответвился) этим фильтром
    и так отбрасывается, а именно от него защищала тройная точка; для
    `--diff-filter=A/R` разницы между `A...B` и `A B` нет никогда.

    Зачем это, а не просто ls-tree головы `local_name` целиком (находка
    живого прогона на mytab0r/edge-harness, 2026-09-13): PR, который просто
    НЕСЁТ уже смердженный файл main (унаследованный, не добавленный им),
    иначе засчитывался бы участником коллизии наравне с main — 19 из 20
    посторонних открытых PR репозитория оказались бы «замешаны» в номере
    0017 только потому, что содержат уже слитый `0017-dsh-edge-pr-smoke-
    local-worker.md`, хотя реальный виновник — ровно один PR (#944),
    добавивший СВОЙ, другой файл под тем же номером.

    `R` (rename) — не только `A` (находка ai-review PR #1082): PR, который
    ПЕРЕИМЕНОВАЛ унаследованный файл (например, переномеровал его же ADR),
    у git это `R100 старый\tновый`, не `A новый` — при фильтре `--diff-
    filter=A` такое переименование было бы НЕВИДИМО для этой функции: сам
    факт, что PR теперь претендует на новый номер, потерялся бы. `git diff
    --name-status` отдаёт для рядов `R`/`C` ТРИ поля (статус с процентом
    схожести, старый путь, новый путь), а не два, как у `A`/`M`/`D` — берём
    ПОСЛЕДНЕЕ поле (актуальный путь после переименования) независимо от
    числа полей."""
    out = run_git(
        "diff", "--name-status", "--diff-filter=AR",
        _local_ref(base_local_name), _local_ref(local_name), "--", root, cwd=cwd,
    )
    return [line.split("\t")[-1] for line in out.splitlines() if line.strip()]


def collect_sources_from_refs(
    refs: dict[str, str], root: str, width: int, cwd=None,
) -> dict[str, dict[str, str]]:
    """Реальный git: фетчит все `refs` (см. fetch_refs), затем читает дерево
    каждого через `ls-tree`/`diff` и парсит номерованные файлы. Ничего не
    знает про GitHub API — эта функция тестируется на настоящем временном
    git-репозитории (test_decision_numbering.py), без сети.

    `main` (если присутствует в `refs`) читается ЦЕЛИКОМ (`ls-tree`) — это
    официальное текущее состояние, а не чья-то заявка. Остальные источники
    читаются как ДОБАВЛЕННОЕ ИМИ относительно main (`added_files_under_root`)
    — см. её докстринг, почему не просто ls-tree головы."""
    fetch_refs(refs, cwd=cwd)
    sources: dict[str, dict[str, str]] = {}
    has_main = "main" in refs
    if has_main:
        parsed = parse_numbered_files(ls_tree_files_under_root("main", root, cwd=cwd), root, width)
        if parsed:
            sources["main"] = parsed
    for local_name in refs:
        if local_name == "main":
            continue
        paths = (
            added_files_under_root("main", local_name, root, cwd=cwd) if has_main
            else ls_tree_files_under_root(local_name, root, cwd=cwd)
        )
        parsed = parse_numbered_files(paths, root, width)
        if parsed:
            sources[local_name] = parsed
    return sources


# ── IO: gh api, только метаданные (какие PR открыты и на какую ветку) ──────

class GhError(RuntimeError):
    pass


def gh(*args: str):
    result = subprocess.run(
        ["gh", "api", *args],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise GhError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else None


def open_pull_refs(repo: str) -> dict[str, str]:
    """{"PR #<N>": "<ветка head>"} для каждого открытого PR репозитория."""
    pulls = _review_labels.list_pages(f"repos/{repo}/pulls?state=open&per_page=100", gh)
    return {f"PR #{pull['number']}": pull["head"]["ref"] for pull in pulls}


def build_refs(repo: str) -> dict[str, str]:
    return {"main": "main", **open_pull_refs(repo)}


def collect_sources(repo: str, root: str, width: int, cwd=None, refs=None) -> dict[str, dict[str, str]]:
    if refs is None:
        refs = build_refs(repo)
    return collect_sources_from_refs(refs, root, width, cwd=cwd)


def current_branch() -> str | None:
    """Ветка ТЕКУЩЕГО прогона: `GITHUB_HEAD_REF` на pull_request-событии
    (голова PR), иначе `GITHUB_REF_NAME` (push/workflow_dispatch — обычно
    "main"). `None` — переменных нет (ручной локальный запуск вне Actions);
    вызывающий код обязан трактовать это как «не знаю», не как «main»."""
    head_ref = os.environ.get("GITHUB_HEAD_REF")
    if head_ref:
        return head_ref
    return os.environ.get("GITHUB_REF_NAME") or None


def self_source_name(refs: dict[str, str]) -> str | None:
    branch = current_branch()
    if branch is None:
        return None
    for name, ref in refs.items():
        if ref == branch:
            return name
    return None


def cmd_next(repo: str, root: str, cwd=None) -> str:
    width = NUMBERED_ROOTS[root]
    sources = collect_sources(repo, root, width, cwd=cwd)
    all_numbers = [number for mapping in sources.values() for number in mapping]
    return next_free_number(all_numbers, width)


def cmd_check(repo: str, roots: list[str], cwd=None) -> tuple[list[str], str | None]:
    """Красит только тот прогон, чья ВЕТКА реально ВИНОВНА в найденной
    коллизии (self_source_name) — иначе один зависший PR с чужой коллизией
    (живой случай на 2026-09-13: PR #944 занял номер 0017, уже слитый в main
    другим ADR) красил бы CI КАЖДОГО постороннего PR репозитория, а не
    только виновника: обязательная проверка `test` превратилась бы в
    тормоз без газа для всей очереди (AGENTS.md, «тормоз без газа не
    принимается»). Прогон, чью ветку определить не удалось (ручной запуск
    вне Actions, `current_branch() is None`) — репортит ВСЕ найденные
    коллизии не сужая (честный дефолт «не знаю → покажи всё», не «не знаю →
    молчи»).

    `main` — ОСОБЫЙ случай виновности, не симметричный обычному PR (issue
    #1200, найдено пост-мерж прогоном repo-ci.yml, 12+ красных `workflow_
    dispatch`-прогонов подряд 2026-09-13/14): `push`/`workflow_dispatch` на
    main тоже резолвит `self_name` в буквальное `"main"` (`build_refs` кладёт
    `{"main": "main", ...}`), и main ОКАЗЫВАЕТСЯ «участником» ЛЮБОЙ коллизии
    вокруг номера, который main держит легитимно, включая ту, где ВТОРАЯ
    сторона — сторонний, ещё НЕ смёрженный открытый PR (живой факт: main
    держит `0017-dsh-edge-pr-smoke-local-worker.md`, PR #944 независимо занял
    тот же номер другим именем — это долг PR #944, не main, но старое условие
    `self_name in involved` считало main виновным, потому что main тоже
    входит в `involved` этой коллизии). main виновен ТОЛЬКО если ВНУТРИ
    самого main реально сидит дубль — минимум два разных имени файла, оба
    ИСКЛЮЧИТЕЛЬНО из main (`main_has_intra_duplicate`, см. её докстринг: НЕ
    то же самое, что `involved == {"main"}` — блокирующая находка ai-review
    PR #1202, множество участников остаётся `{"main", "PR #N"}`, даже когда
    дубль сидит внутри main, а рядом просто ещё висит сторонний открытый PR
    с третьим файлом под тем же номером). Тот же приём независимо выбрал
    параллельный канал для номеров инвариантов
    (`scripts/lib/invariant_numbering.py::cmd_check`, issue #904/PR #1201,
    слит) — та же неточная форма `involved != {"main"}` живёт там СЕЙЧАС в
    main (не поправлена этим PR — другой файл, не в объёме #1200), заведён
    issue #1227.

    Возвращает `(строки_нарушений, self_name)` — второй элемент нужен ТОЛЬКО
    вызывающему коду (CLI `main`) для честного сообщения об успехе: «коллизий
    нет» при пустом `self_name` (проверка не сужалась, значит их нет вообще)
    — не то же самое, что «коллизий, касающихся ЭТОЙ ветки, нет» при известном
    `self_name` (у посторонних PR они, возможно, есть — просто не в этом
    прогоне). Находка ai-review PR #1082: старое сообщение «коллизий номеров
    нет» одинаково печаталось в обоих случаях — ложь во втором."""
    lines = []
    refs = build_refs(repo)
    self_name = self_source_name(refs)
    for root in roots:
        width = NUMBERED_ROOTS[root]
        sources = collect_sources_from_refs(refs, root, width, cwd=cwd)
        for violation in find_number_collisions(sources):
            if self_name is None:
                pass  # честный дефолт: не знаю self — показываю всё, не сужаю
            elif self_name == "main":
                if not main_has_intra_duplicate(violation):
                    continue
            else:
                involved = {src for occ in violation["occurrences"] for src in occ["sources"]}
                if self_name not in involved:
                    continue
            lines.append(format_violation(root, violation))
    return lines, self_name


def check_decision_doc_number_collisions(repo: str, cwd=None) -> "check_result.CheckResult":
    """Обвязка, готовая для `repo_invariants.py` (класс — AGENTS.md,
    «Инцидент оставляет инвариант, а не только фикс»; находка ai-review
    PR #1082, блокирующая 2). НЕ подключена туда ЭТИМ PR намеренно: на
    момент PR #1082 `repo_invariants.py`/`test_repo_invariants.py`
    параллельно правит PR #1076 (ADR 0016, «конкуренция за код не
    устраняется приёмом регистрации» — тот же класс, что описывает сама эта
    функция, только для другого носителя). Подключение — отдельная узкая
    задача #1090, одна строка
    `from decision_numbering import check_decision_doc_number_collisions` +
    один вызов в `build_report()`, после того как #1076 сольётся.

    ГВАРДИЯ каталога (`decision-doc-numbering-guard.sh`) ловит коллизию
    только на push/PR — вердикт привязан к head; PR, позеленевший, пока
    номер был свободен, остаётся зелёным, даже если конкурент СЛИЛСЯ и занял
    номер первым ПОСЛЕ этого прогона (дрейф состояния без нового коммита —
    ровно класс, под который заведён 15-минутный инвариант `orchestra.yml`).

    Возвращает `check_result.CheckResult` (issue #1096/#1110, третье
    состояние — репозиторий мигрирует существующие инварианты на этот тип,
    новый инвариант подключается уже в целевой форме, не в старой
    `list[dict]`, которую пришлось бы мигрировать вторым проходом):
    `unknown()`, если `git`/`gh` внутри `collect_sources` реально отказали
    (транспорт, а не «коллизий нет» — тот же класс F1/F3 issue #1096,
    который мигрировал инварианты 13/14); `violation([...])` с находками в
    форме `{"root": ..., "number": ..., "occurrences": [...]}` — то же, что
    `find_number_collisions`, плюс `root`; иначе `ok()`."""
    violations = []
    for root, width in NUMBERED_ROOTS.items():
        try:
            sources = collect_sources(repo, root, width, cwd=cwd)
        except (GhError, GitError) as error:
            return check_result.unknown(
                f"decision_doc_number_collisions: сбор данных для {root} не удался ({error})"
            )
        for found in find_number_collisions(sources):
            violations.append({**found, "root": root})
    if violations:
        return check_result.violation(violations)
    return check_result.ok()


def _repo_from_env() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise SystemExit("GITHUB_REPOSITORY не задан — укажи репозиторий явно")
    return repo


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: decision_numbering.py next|check [root ...]", file=sys.stderr)
        return 2
    command, *rest = argv
    repo = _repo_from_env()
    if command == "next":
        if len(rest) != 1 or rest[0] not in NUMBERED_ROOTS:
            print(f"usage: decision_numbering.py next <{'|'.join(NUMBERED_ROOTS)}>", file=sys.stderr)
            return 2
        print(cmd_next(repo, rest[0]))
        return 0
    if command == "check":
        roots = rest or list(NUMBERED_ROOTS)
        unknown = [r for r in roots if r not in NUMBERED_ROOTS]
        if unknown:
            print(f"неизвестный корень: {unknown}", file=sys.stderr)
            return 2
        violations, self_name = cmd_check(repo, roots)
        if violations:
            for line in violations:
                print(f"::error::{line}")
            return 1
        if self_name is None:
            print("decision_numbering: коллизий номеров нет")
        else:
            print(
                f"decision_numbering: коллизий, касающихся {self_name}, нет "
                "(у других открытых PR они могли не проверяться этим прогоном)"
            )
        return 0
    print(f"неизвестная команда: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
