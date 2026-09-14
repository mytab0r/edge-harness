#!/usr/bin/env python3
"""Рецепт мутации, доказанный ИСПОЛНЕНИЕМ, а не памятью (issue #1194).

## Класс дефекта

AGENTS.md требует: гвардия доказывается мутацией — сними фикс, убедись, что
тест покраснел, верни. На практике (PR #1173, комментарий-восстановление
issue #1194) рецепт мутации иногда пишется ПО АНАЛОГИИ с прежним похожим
случаем, а не исполняется: автор гвардии `dsh-edge/test/ingest-resident-
safety.test.mjs` заявил «одно освобождение дескриптора, третья пачка не
запускается» — ai-review реально откатило патч до состояния `d239e324~1` и
получило «три освобождения, третья пачка выполняется и падает с текстом
`session "ingest-check" is not live in this store`». Оба числа выглядели
как правдоподобные конкретные утверждения — ЧИСТО ТЕКСТОВАЯ проверка формы
(«есть ли число рядом со словом failed/passed») не отличает эти два случая:
и заявленное, и фактическое значение — одинаково «похожи на реальный вывод».
Различить их может только исполнение (см. `test_mutation_recipe_guard.py`,
воспроизводящий именно эту историческую пару коммитов).

## Формат MUTATION-PROOF

Комментарий/тело PR, утверждающее конкретный результат мутации, обязано
нести блок из строк вида (после снятия общих префиксов комментария — `#`,
`//`, `*`, пробел — с каждой строки):

    MUTATION-PROOF
    ref: <git-ref состояния ДО фикса>
    paths: <относительный путь[, ещё путь…] — что заменить на историческое>
    run: <команда, которую исполнить в рабочем дереве с подменёнными paths>
    expect: <подстрока, обязанная встретиться в выводе команды ПОСЛЕ подмены>

`verify_block()` реально: (1) достаёт исторический байт-контент `paths` на
`ref` (`git show ref:path`), (2) кладёт его в изолированное рабочее дерево
поверх текущего HEAD, (3) запускает `run`, (4) сверяет `expect` с ФАКТИЧЕСКИМ
захваченным выводом — не с тем, что автор ПОМНИТ или ПРЕДПОЛАГАЕТ. Дополнительно
гоняет тот же `run` БЕЗ подмены (чистый HEAD) и требует, чтобы `expect` там
НЕ встречался — иначе мутация ничего не отличает (пустая проверка).

## Честная граница

Формат добровольный (opt-in) — файлы БЕЗ блока MUTATION-PROOF не считаются
нарушением (не задним числом на весь корпус прозы «Доказательство мутацией»,
которая по-прежнему разрешена AGENTS.md как ручной путь). `ref`/`paths` из
блока обязаны существовать в git-истории ЭТОГО репозитория — если нет,
результат `unknown()` (транспорт/данные недоступны), не `violation()`
(нельзя утверждать «неверно» о том, что не проверено). Команда `run` —
свободный shell (её содержимое доверяется автору блока: та же модель
доверия, что уже несёт любой шаг CI/гвардия каталога `scripts/ci/guards/`).

Запуск тестов: python -m pytest scripts/lib/test_mutation_recipe_guard.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import re
import shutil
import subprocess
import sys
import tempfile
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_CHR_SPEC = importlib.util.spec_from_file_location(
    "check_result", Path(__file__).resolve().parent / "check_result.py")
check_result = importlib.util.module_from_spec(_CHR_SPEC)
_CHR_SPEC.loader.exec_module(check_result)  # type: ignore[union-attr]

MARKER = "MUTATION-PROOF"
_COMMENT_PREFIX_RE = re.compile(r"^[ \t]*(#|//|\*|<!--)*[ \t]*")
_KV_RE = re.compile(r"^([a-z_]+):[ \t]*(.+?)[ \t]*(-->)?$")
_KNOWN_KEYS = {"ref", "paths", "run", "expect"}


def _strip_comment_prefix(line: str) -> str:
    """Снимает общий префикс комментария/маркер markdown-конца ('-->'),
    чтобы один и тот же блок читался одинаково из .py/.mjs/.sh/.md."""
    stripped = _COMMENT_PREFIX_RE.sub("", line, count=1)
    return stripped.rstrip()


class MutationProofBlock(NamedTuple):
    source_file: str
    line_no: int  # 1-based, строка маркера MUTATION-PROOF
    ref: str
    paths: tuple[str, ...]
    run: str
    expect: str


def parse_mutation_proof_blocks(text: str, source_file: str = "<text>") -> list[MutationProofBlock]:
    """Находит все блоки MUTATION-PROOF в тексте (файл гвардии, PR-тело).
    Блок без всех четырёх обязательных ключей ИГНОРИРУЕТСЯ (недо-написанный
    маркер — не полноценный блок, но и не наша забота здесь; забота —
    вызывающего кода, который может отдельно потребовать полноту)."""
    lines = text.splitlines()
    blocks: list[MutationProofBlock] = []
    i = 0
    while i < len(lines):
        if _strip_comment_prefix(lines[i]) == MARKER:
            marker_line_no = i + 1
            fields: dict[str, str] = {}
            j = i + 1
            while j < len(lines):
                candidate = _strip_comment_prefix(lines[j])
                if not candidate:
                    break
                m = _KV_RE.match(candidate)
                if not m or m.group(1) not in _KNOWN_KEYS:
                    break
                fields[m.group(1)] = m.group(2)
                j += 1
            if _KNOWN_KEYS.issubset(fields.keys()):
                paths = tuple(p.strip() for p in fields["paths"].split(",") if p.strip())
                blocks.append(MutationProofBlock(
                    source_file=source_file,
                    line_no=marker_line_no,
                    ref=fields["ref"],
                    paths=paths,
                    run=fields["run"],
                    expect=fields["expect"],
                ))
            i = j
        else:
            i += 1
    return blocks


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )


def _historical_content(ref: str, path: str, repo_root: Path) -> str | None:
    proc = _git(["show", f"{ref}:{path}"], cwd=repo_root)
    if proc.returncode != 0:
        return None
    return proc.stdout


def _run_command(command: str, cwd: Path) -> str:
    proc = subprocess.run(
        command, shell=True, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    return (proc.stdout or "") + (proc.stderr or "")


def _sync_uncommitted_changes(repo_root: Path, worktree: Path) -> None:
    """`git worktree add HEAD` несёт только ЗАКОММИЧЕННОЕ состояние. В CI это
    не имеет значения (гвардия запускается на закоммиченном PR), но при
    прогоне ДО коммита (dev-цикл, эта же гвардия проверяет саму себя) новые/
    изменённые файлы каталога гвардий иначе не попали бы в изолированное
    рабочее дерево — проверка молча падала бы на 'файл не найден', а не на
    содержательном расхождении. Копирует рабочие версии изменённых/новых
    неигнорируемых файлов поверх only проверяемого дерева; в чистом дереве
    (всё закоммичено) `git status --porcelain` пуст — no-op."""
    status = _git(["status", "--porcelain", "--no-renames"], cwd=repo_root)
    if status.returncode != 0:
        return
    for line in status.stdout.splitlines():
        if not line or len(line) < 4:
            continue
        rel = line[3:]
        if line.startswith("D") or line.startswith(" D"):
            continue
        src = repo_root / rel
        if src.is_dir():
            # Целиком неотслеживаемая директория (`?? dir/`) — git status не
            # разворачивает её на файлы, обходим сами.
            for sub in src.rglob("*"):
                if sub.is_file():
                    dst = worktree / sub.relative_to(repo_root)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(sub, dst)
            continue
        if not src.is_file():
            continue
        dst = worktree / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def verify_block(block: MutationProofBlock, repo_root: Path = REPO_ROOT) -> check_result.CheckResult:
    """Реально проверяет один блок: подменяет `paths` историческим
    содержимым на `ref`, гонит `run`, сверяет `expect` с ФАКТИЧЕСКИМ выводом.
    Три исхода: ok() — рецепт подтверждён исполнением; violation() —
    заявленный `expect` разошёлся с фактом (текст различия — в находке);
    unknown() — ref/path не читается (git show отказал), проверка не
    состоялась, не «состоялась и опровергла»."""
    historical = {}
    for path in block.paths:
        content = _historical_content(block.ref, path, repo_root)
        if content is None:
            return check_result.unknown(
                f"MUTATION-PROOF {block.source_file}:{block.line_no}: "
                f"git show {block.ref}:{path} не читается — ref/путь недоступны, "
                f"рецепт не проверен (не 'опровергнут')")
        historical[path] = content

    worktree = Path(tempfile.mkdtemp(prefix="mutation-proof-"))
    try:
        add = _git(["worktree", "add", "--detach", "-f", str(worktree), "HEAD"], cwd=repo_root)
        if add.returncode != 0:
            return check_result.unknown(
                f"MUTATION-PROOF {block.source_file}:{block.line_no}: "
                f"git worktree add отказал ({add.stderr.strip()}) — проверка не состоялась")
        _sync_uncommitted_changes(repo_root, worktree)
        for path, content in historical.items():
            target = worktree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        mutated_output = _run_command(block.run, cwd=worktree)
        baseline_output = _run_command(block.run, cwd=repo_root)
    finally:
        _git(["worktree", "remove", "--force", str(worktree)], cwd=repo_root)
        shutil.rmtree(worktree, ignore_errors=True)

    mutated_confirms = block.expect in mutated_output
    baseline_also_confirms = block.expect in baseline_output

    if baseline_also_confirms:
        return check_result.violation([
            f"MUTATION-PROOF {block.source_file}:{block.line_no}: expect "
            f"«{block.expect}» встречается ДАЖЕ БЕЗ мутации (baseline тот же) — "
            f"рецепт ничего не отличает, expect слишком общий"])

    if not mutated_confirms:
        return check_result.violation([
            f"MUTATION-PROOF {block.source_file}:{block.line_no}: заявлено "
            f"«{block.expect}», фактический вывод исполненной мутации "
            f"НЕ содержит эту подстроку — рецепт записан по памяти/аналогии, "
            f"не исполнен. Фактический вывод: {mutated_output.strip()[:2000]!r}"])

    return check_result.ok()


def scan_and_verify(paths: list[Path], repo_root: Path = REPO_ROOT) -> list[tuple[MutationProofBlock, check_result.CheckResult]]:
    """Сканирует список файлов на блоки MUTATION-PROOF и проверяет каждый."""
    results = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(path.relative_to(repo_root)) if path.is_absolute() else str(path)
        for block in parse_mutation_proof_blocks(text, source_file=rel):
            results.append((block, verify_block(block, repo_root=repo_root)))
    return results


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: mutation_recipe_guard.py <file>...", file=sys.stderr)
        return 2
    paths = [Path(a).resolve() for a in argv]
    results = scan_and_verify(paths)
    if not results:
        print("mutation-recipe-guard: 0 блоков MUTATION-PROOF в переданных файлах (opt-in, не нарушение)")
        return 0
    failed = 0
    for block, result in results:
        if result.status == check_result.STATUS_OK:
            print(f"MUTATION-PROOF {block.source_file}:{block.line_no}: подтверждён исполнением")
        elif result.status == check_result.STATUS_UNKNOWN:
            print(f"MUTATION-PROOF {block.source_file}:{block.line_no}: не проверен — {result.reason}")
        else:
            failed += 1
            for v in result.violations:
                print(f"::error::{v}")
    if failed:
        print(f"mutation-recipe-guard: {failed} рецепт(ов) не подтверждён(ы) исполнением")
        return 1
    print(f"mutation-recipe-guard: {len(results)} блок(ов) проверено, 0 расхождений")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
