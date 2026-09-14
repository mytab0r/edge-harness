#!/usr/bin/env python3
"""Номер инварианта `repo_invariants.py` не назначается вручную без арбитра (#904).

## Класс

Тот же класс, что #1078 у `docs/decisions/*`/`docs/research/*`
(`scripts/lib/decision_numbering.py`): общий числовой ресурс, правится
вручную, конкурирует между независимыми PR без арбитра — до слияния никто
не узнаёт о коллизии. Носитель здесь ДРУГОЙ: не каталог пронумерованных
файлов, а нумерованный список ВНУТРИ ОДНОГО файла — реестр в докстринге
модуля `scripts/orchestra/repo_invariants.py`, строки вида
"  N. check_имя_функции — описание" (см. `REGISTRY_ENTRY_RE` ниже; формат
подтверждён на живом файле — все 18 записей реестра на 2026-09-14
соответствуют ему, включая "(retired)"-запись у бывшего инварианта 2).

Эта разница НЕ позволяет использовать `parse_numbered_files`/
`ls_tree_files_under_root`/`added_files_under_root` из `decision_numbering.py`
как есть — те сравнивают СПИСКИ ФАЙЛОВ каталога, а здесь нужно сравнивать
СТРОКИ одного файла. Но газовые/git-примитивы (`run_git`, `fetch_refs`,
`GitError`/`GhError`, `gh`, `open_pull_refs`, `build_refs`, `current_branch`,
`self_source_name`, `_local_ref`) и чистое ядро сравнения
(`find_number_collisions`) — ПЕРЕИСПОЛЬЗУЮТСЯ импортом из
`decision_numbering.py`, не второй реализацией (AGENTS.md, «Одно место
правды»): это ровно тот случай, когда форма ложится, а не повод для нового
параллельного механизма.

## Живой случай, использованный как фикстура доказательства мутацией

НЕ по аналогии (issue #1194) — по факту состояния репозитория на
2026-09-14, обнаруженному при подготовке этого PR: открытые PR **#1061**
(issue #925, ветка `agent/925-ci-run-on-main`) и **#1136** (issue #1121,
ветка `agent/1121-soft-failure-digest`) НЕЗАВИСИМО добавили инвариант **19**
в `repo_invariants.py` — разными функциями
(`check_ci_failure_closed_but_main_red` и `check_continue_on_error_readers`
соответственно), при этом main на тот момент несёт только записи 1..18.
Это произошло ПОСЛЕ того, как коллизию на номере 17 (между PR #1076 и
незав. каналом) устранили переносом руками на 19 — сам перенос и породил
новую коллизию, потому что арбитра не было (issue #904, раздел «Живая
коллизия»). Тест `test_end_to_end_detects_live_904_collision_on_real_git`
воспроизводит РОВНО эти два источника литерально (дословные строки реестра,
скопированные из веток PR на момент написания этого модуля — не пересказ).

## Устройство

  - `REGISTRY_ENTRY_RE`/`parse_registry_entries` — чистая функция, парсит
    строки реестра из ТЕКСТА файла (не из git) — тестируется без сети.
  - `read_blob_at_ref`/`added_registry_entries`/`collect_sources_from_refs` —
    РЕАЛЬНЫЙ git (`show`/`diff`, без тройной точки — тот же приём, что
    `decision_numbering.added_files_under_root`, работает на shallow-клоне
    `actions/checkout@v7` без `fetch-depth: 0`), без GitHub API.
  - `collect_sources` — тонкая обвязка: спрашивает у `gh api` список открытых
    PR (через `decision_numbering.build_refs`), передаёт ссылки выше.
  - `cmd_check` красит ТОЛЬКО тот прогон, чья ветка реально участвует в
    найденной коллизии (`decision_numbering.self_source_name`) — тот же газ,
    что у `decision_numbering.cmd_check` (AGENTS.md, «тормоз без газа не
    принимается»): посторонний PR не обязан чинить чужую коллизию.

## Схема нумерации — сквозная, не мигрирована на стабильный ключ

Issue #904 предлагал также рассмотреть уход от сквозной нумерации (номер =
номер задачи, либо стабильный строковый ключ). Решение этого PR: НЕ
мигрировать сейчас. Цена миграции на живом `repo_invariants.py`
(2026-09-14, посчитано по числу мест, знающих число, а не долю догадки):
докстринг-реестр (18 записей), `CI_GATING`/`GATING_RELEASE_CONDITION`
(словари по числовому ключу), `ESCALATING_INVARIANTS` (кортеж), `findings[N]`
(≥18 присваиваний + столько же строк отчёта), `CHECK_RESULT_MIGRATED_
INVARIANTS`, эскалационные блоки `run_escalations` (по одному на
эскалирующий инвариант, с текстом «инвариант N» в алертах и Telegram),
десятки ссылок в `test_repo_invariants.py`, и — упоминания «инвариант N» в
теле уже закрытых/открытых issue/PR (не перепривязываются механически).
Строковый ключ (например, имя функции) убрал бы саму коллизию по
конструкции, но: (а) `repo_invariants.py` занят ДВУМЯ параллельными PR
(#1136, #1189) прямо сейчас — правка такого масштаба физически не сольётся
без конфликта с обоими; (б) число мест правки — не только код, но и текст
уже отправленных владельцу алертов (Telegram, #120) — обратной силы у них
нет, и смешанная нумерация (часть инвариантов по номеру, часть по ключу)
хуже, чем сквозная нумерация с арбитром. Дешёвая правка сейчас — тот же
рецепт, что #1078 выбрал для decision-документов: не убирать номер, а
добавить арбитра (эта гвардия) — цена низкая (один новый файл, ноль правок
занятых файлов), польза равна: коллизия видна ДО мержа, а не после.
Если/когда #1136 и #1189 сольются и `repo_invariants.py` освободится —
переоценка схемы (номер задачи вместо сквозного счётчика) остаётся
открытым вопросом, не закрытым этим PR.

## Живой дефект найден в decision_numbering.py (#1078) — НЕ починен здесь

При подготовке этого PR (2026-09-14) обнаружено: обязательная проверка
`test` красная НА КАЖДОМ push в `main` (repo-ci.yml, job `test`, шаг
«Каталог гвардий scripts/ci/guards — перебор») из-за
`decision-doc-numbering-guard.sh` — `main` резолвится в `self_name ==
"main"` на push-событии и потому считается «участником» коллизии между
уже слитым в main номером ADR и номером, который независимо занял
сторонний, ещё не смёрженный открытый PR (0017 против PR #944, 0018
против PR #667) — сам push, вызвавший прогон, к этой коллизии не имеет
отношения, а тормоз не снимается, пока сторонний PR не решит свою судьбу.
См. докстринг `cmd_check` ниже — этот, НОВЫЙ, инвариантный модуль
спроектирован БЕЗ этого дефекта с первого коммита (main отвечает только
за коллизию ВНУТРИ себя самого). `decision_numbering.py` не мой файл в
рамках #904 — инцидент заведён отдельным issue, не чинится этим PR.

## НЕ подключено в repo_invariants.py

`check_invariant_number_collisions` (аналог `decision_numbering.
check_decision_doc_number_collisions`) намеренно НЕ вызывается из
`build_report()` этим PR — `repo_invariants.py`/`test_repo_invariants.py`
заняты PR #1136 (инвариант 19, соперник за номер) и #1189 (инвариант 14 +
`pulse_guard`). Подключение — отдельная узкая задача, тот же порядок, что
#1090 у `decision_numbering.check_decision_doc_number_collisions`.

## CLI

  python scripts/lib/invariant_numbering.py next
      печатает следующий свободный номер инварианта (main ∪ все открытые PR).
  python scripts/lib/invariant_numbering.py check
      печатает найденные коллизии номеров и завершается кодом 1, если хоть
      одна есть (сужено к текущей ветке — см. cmd_check).
Оба читают `GITHUB_REPOSITORY` (см. `decision_numbering.py`) и работают в
git-дереве текущей рабочей директории."""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import os
import re
import subprocess
import sys

# Импорт соседнего модуля по файлу (паттерн decision_numbering/claim_task:
# скрипты этого репозитория запускаются как файлы, не как пакет). Реюз
# генерик-ядра (find_number_collisions/next_free_number) и git/gh-примитивов
# — не вторая реализация того же класса (см. докстринг выше).
_DN_SPEC = importlib.util.spec_from_file_location(
    "decision_numbering", Path(__file__).resolve().parent / "decision_numbering.py")
dn = importlib.util.module_from_spec(_DN_SPEC)
_DN_SPEC.loader.exec_module(dn)  # type: ignore[union-attr]

_CR_SPEC = importlib.util.spec_from_file_location(
    "check_result", Path(__file__).resolve().parent / "check_result.py")
check_result = importlib.util.module_from_spec(_CR_SPEC)
_CR_SPEC.loader.exec_module(check_result)  # type: ignore[union-attr]

# Единственное место правды: какой файл несёт нумерованный реестр инвариантов.
TARGET_PATH = "scripts/orchestra/repo_invariants.py"

# Строка реестра: "  N. check_имя_функции — ..." или "  N. (retired) check_имя …",
# либо та же форма с именем функции, обёрнутым markdown-разметкой
# (`check_имя`, **check_имя** — правдоподобная форма будущей записи: авторы
# реестра уже перенумеровывали его руками под давлением мержа, находка
# ai-review PR #1201, класс #891/#893 «гвардия слепнет молча при дрейфе
# формата»). `[`*_]*` съедает разметку ДО имени функции; после — незначим,
# `\w+` в самом имени останавливается на первом не-word символе (бэктике/
# звёздочке) сам по себе.
# Подтверждено на живом файле (18 записей, 2026-09-14, пин —
# test_parse_registry_entries_pins_the_live_repo_invariants_registry).
REGISTRY_ENTRY_RE = re.compile(r"^\s*(\d+)\.\s*(?:\(retired\)\s*)?[`*_]*(check_\w+)")


def parse_registry_entries(text: str) -> dict[str, list[str]]:
    """Чистая функция: строки реестра → {номер: [имена_функций]}. Список,
    не строка (тот же класс, что `decision_numbering.parse_numbered_files`
    докстринг объясняет) — main сам может нести дубль номера после тихого
    слияния двух PR с разными путями (здесь — разными строками файла)."""
    result: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = REGISTRY_ENTRY_RE.match(line)
        if not match:
            continue
        result.setdefault(match.group(1), []).append(match.group(2))
    return result


# ── IO: git (реальный fetch + show/diff, без GitHub API) ───────────────────
# Фетч и разрешение локальных рефов — прямой реюз decision_numbering.py:
# fetch_refs/_local_ref НЕ специфичны для каталогов, оперируют произвольным
# {локальное_имя: remote_ref}, разницы для одного файла против дерева нет.


def read_blob_at_ref(local_name: str, path: str, cwd=None) -> str | None:
    """Содержимое `path` на дереве `local_name` (`git show <ref>:<path>`).
    `None`, если путь не существует на этом ref (файл ещё не создан/удалён)
    — не GitError: отсутствие пути там же законно, где отсутствие каталога
    у decision_numbering (просто «эта ветка ничего не парсит здесь»)."""
    ref = f"{dn._local_ref(local_name)}:{path}"
    result = subprocess.run(
        ["git", "show", ref], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "does not exist" in stderr or "exists on disk, but not" in stderr:
            return None
        raise dn.GitError(f"git show {ref} упал: {stderr}")
    return result.stdout


def added_registry_entries(base_local_name: str, local_name: str, path: str, cwd=None) -> dict[str, list[str]]:
    """Строки реестра, которые `local_name` ДОБАВИЛ относительно
    `base_local_name` — только '+'-строки unified diff, совпавшие с
    `REGISTRY_ENTRY_RE`. Тот же приём и та же причина, что у
    `decision_numbering.added_files_under_root` (докстринг там разбирает оба
    гранта подробно, здесь коротко):

      - ДВА отдельных рефа, БЕЗ тройной точки (`git diff A B`, не `A...B`) —
        тройная точка требует merge-base в истории, которого нет на
        shallow-клоне (`actions/checkout@v7` без `fetch-depth: 0`); прямое
        сравнение двух деревьев работает без общего предка вообще.
      - Читаем ТОЛЬКО добавленное этой веткой, не весь её реестр целиком —
        иначе PR, просто НЕСУЩИЙ унаследованные из main записи 1..18,
        ложно считался бы их «автором», и любая посторонняя ветка стала бы
        мнимым участником коллизии вокруг них."""
    out = dn.run_git(
        "diff", "--unified=0",
        dn._local_ref(base_local_name), dn._local_ref(local_name), "--", path,
        cwd=cwd,
    )
    result: dict[str, list[str]] = {}
    for line in out.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        match = REGISTRY_ENTRY_RE.match(line[1:])
        if not match:
            continue
        result.setdefault(match.group(1), []).append(match.group(2))
    return result


def _parse_full_registry_or_die(local_name: str, content: str | None) -> dict[str, list[str]]:
    """Полный (не diff) парс реестра ветки `local_name`. Пустой словарь при
    НЕПУСТОМ содержимом файла — не легитимное «записей нет», а слепота
    парсера к дрейфу формата (находка ai-review PR #1201, класс #891/#893
    «гвардия слепнет молча»): реестр `repo_invariants.py` никогда не бывает
    легитимно пуст, если файл существует. Раньше это тихо трактовалось как
    «источник ничего не несёт» → отсутствие коллизии не отличить от
    неспособности её увидеть — громкий отказ (`GitError`) вместо этого,
    пойманный `check_invariant_number_collisions` в `check_result.unknown`.
    `content is None` (файла нет на этом ref) остаётся легитимным «нечего
    парсить» — путь мог законно отсутствовать."""
    if content is None:
        return {}
    parsed = parse_registry_entries(content)
    if not parsed:
        raise dn.GitError(
            f"{TARGET_PATH} на {local_name} прочитан ({len(content)} байт), "
            "но REGISTRY_ENTRY_RE не нашёл ни одной записи реестра — формат "
            "дрейфует или файл переехал, парсер слеп (issue #904, находка "
            "ai-review PR #1201)"
        )
    return parsed


def collect_sources_from_refs(refs: dict[str, str], path: str = TARGET_PATH, cwd=None) -> dict[str, dict[str, list[str]]]:
    """Реальный git: фетчит все `refs` (`dn.fetch_refs`), затем читает
    реестр каждого через `read_blob_at_ref`/`added_registry_entries`.
    `main` (если присутствует) читается ЦЕЛИКОМ — официальное текущее
    состояние; остальные источники — только ДОБАВЛЕННОЕ ими относительно
    main (см. `added_registry_entries`)."""
    dn.fetch_refs(refs, cwd=cwd)
    sources: dict[str, dict[str, list[str]]] = {}
    has_main = "main" in refs
    if has_main:
        content = read_blob_at_ref("main", path, cwd=cwd)
        parsed = _parse_full_registry_or_die("main", content)
        if parsed:
            sources["main"] = parsed
    for local_name in refs:
        if local_name == "main":
            continue
        if has_main:
            parsed = added_registry_entries("main", local_name, path, cwd=cwd)
        else:
            content = read_blob_at_ref(local_name, path, cwd=cwd)
            parsed = _parse_full_registry_or_die(local_name, content)
        if parsed:
            sources[local_name] = parsed
    return sources


def collect_sources(repo: str, path: str = TARGET_PATH, cwd=None, refs=None) -> dict[str, dict[str, list[str]]]:
    if refs is None:
        refs = dn.build_refs(repo)
    return collect_sources_from_refs(refs, path, cwd=cwd)


def format_violation(violation: dict) -> str:
    parts = "; ".join(
        f"{occ['filename']} ({', '.join(occ['sources'])})"
        for occ in violation["occurrences"]
    )
    return f"{TARGET_PATH}: номер инварианта {violation['number']} занят разными функциями — {parts}"


def cmd_next(repo: str, path: str = TARGET_PATH, cwd=None) -> str:
    sources = collect_sources(repo, path, cwd=cwd)
    all_numbers = [number for mapping in sources.values() for number in mapping]
    return dn.next_free_number(all_numbers, width=1)


def cmd_check(repo: str, path: str = TARGET_PATH, cwd=None) -> tuple[list[str], str | None]:
    """Красит только тот прогон, чья ВЕТКА реально участвует в найденной
    коллизии — тот же газ, что `decision_numbering.cmd_check` (AGENTS.md,
    «тормоз без газа не принимается»): посторонний PR не обязан чинить
    чужую коллизию вокруг номера, который он не трогал.

    Живой случай, найденный ПРИ ПОДГОТОВКЕ этого PR (не по аналогии, issue
    #1194): у `decision_numbering.cmd_check` та же формула («self_name не в
    involved → пропусти») даёт ложный тормоз для `main` — на push-событии
    `self_name` резолвится в буквальное `"main"` (см. `dn.build_refs`,
    `dn.self_source_name`), и main оказывается «участником» ЛЮБОЙ коллизии
    между уже слитым номером main и номером, который независимо (и пока не
    смёржено) выбрал сторонний открытый PR — а такая пара сторонних PR
    существует в репозитории прямо сейчас (`docs/decisions` 0017 против PR
    #944, 0018 против PR #667). Итог живого прогона repo-ci.yml на push в
    main (2026-09-14, run 34803174089): обязательная проверка `test` красная
    ИЗ-ЗА decision-doc-numbering-guard.sh, хотя PR, вызвавший push, к этой
    коллизии не имеет отношения — main наказан за чужой, ещё не смёрженный
    долг (AGENTS.md, «тормоз без газа не принимается»: обязательная
    проверка после push в main не восстанавливается сама, пока сторонний PR
    не решит СВОЮ судьбу — а её решение никак не форсируется этим тормозом).
    Не чиню `decision_numbering.py` этим PR (не мой файл, отдельный
    инцидент — заведён issue) — но эта, НОВАЯ, гвардия спроектирована без
    этого дефекта с самого начала: `main` виновен только в коллизии ВНУТРИ
    самого себя (два инварианта под одним номером после тихого слияния двух
    коллидирующих PR, `involved == {"main"}`) — коллизия main против
    стороннего, ещё не смёрженного PR остаётся на совести ЭТОГО PR (его
    собственный прогон видит `self_name == "PR #N"`, и `involved` содержит
    его — он покраснеет сам, `main` — нет)."""
    lines = []
    refs = dn.build_refs(repo)
    self_name = dn.self_source_name(refs)
    sources = collect_sources_from_refs(refs, path, cwd=cwd)
    for violation in dn.find_number_collisions(sources):
        involved = {src for occ in violation["occurrences"] for src in occ["sources"]}
        if self_name is None:
            pass  # честный дефолт (см. decision_numbering.cmd_check): не знаю → покажи всё
        elif self_name == "main":
            if involved != {"main"}:
                continue
        elif self_name not in involved:
            continue
        lines.append(format_violation(violation))
    return lines, self_name


def check_invariant_number_collisions(repo: str, cwd=None) -> "check_result.CheckResult":
    """Обвязка, готовая для `repo_invariants.py` (см. докстринг модуля —
    НЕ подключена этим PR: файл занят PR #1136/#1189). Возвращает
    `check_result.CheckResult` (issue #1096) сразу в целевой форме, не в
    старой `list[dict]`, которую пришлось бы мигрировать вторым проходом."""
    try:
        sources = collect_sources(repo, cwd=cwd)
    except (dn.GhError, dn.GitError) as error:
        return check_result.unknown(
            f"invariant_number_collisions: сбор данных не удался ({error})"
        )
    violations = dn.find_number_collisions(sources)
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
        print("usage: invariant_numbering.py next|check", file=sys.stderr)
        return 2
    command, *rest = argv
    repo = _repo_from_env()
    if command == "next":
        print(cmd_next(repo))
        return 0
    if command == "check":
        violations, self_name = cmd_check(repo)
        if violations:
            for line in violations:
                print(f"::error::{line}")
            return 1
        if self_name is None:
            print("invariant_numbering: коллизий номеров нет")
        else:
            print(
                f"invariant_numbering: коллизий, касающихся {self_name}, нет "
                "(у других открытых PR они могли не проверяться этим прогоном)"
            )
        return 0
    print(f"неизвестная команда: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
