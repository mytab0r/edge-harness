#!/usr/bin/env python3
"""Гвардия «карта документации полна» (#670).

Класс: `AGENTS.md` первым действием сессии предписывает читать `docs/INDEX.md`
— «там карта всего, что уже исследовано» (docs/INDEX.md, строка 4). Документ
может физически лежать в репозитории и не быть достижим с карты — тогда
агент, следующий этой инструкции, систематически не видит часть знания.
Живой случай: ADR `docs/decisions/0015-clock-shift-freezegun.md` (PR #667) не
попал в индекс, и ни одна проверка этого не заметила.

Наблюдаемые каталоги — ровно те, что `docs/INDEX.md` сам называет разделами
постоянно живущей документации:
  - `docs/decisions/` — ADR (таблица «Как устроена документация»);
  - `docs/research/`  — установленные факты о внешних системах (та же таблица);
  - `docs/runbooks/`  — операционные процедуры владельца (та же таблица);
  - `docs/agents/`    — протокол и инфраструктура мультиагентной работы
    (раздел индекса «Мультиагентная работа»; в таблицу выше не включён, но
    состоит из документов того же класса — «уже исследовано/решено, не
    переисследовать» — и явно перечисляется в индексе своим разделом).
`openspec/specs/` и `openspec/changes/` сюда НЕ входят: это не архив
исследованного, а рабочая область активных изменений со своим жизненным
циклом (архивация после выполнения tasks.md) — другой класс документа,
описанный в той же таблице отдельной строкой, вне карты «что уже
исследовано». `docs/api.md` (одиночный файл верхнего уровня, не каталог) и
`docs/research/data/*.csv` (сырые данные измерений, не документ) — тоже вне
области этой гвардии.

Оба markdown-расширения (`.md` и `.markdown`) — класс дефекта «скан по одному
суффиксу» уже стоил репозиторию отдельного инцидента (#635, `.github/workflows`
разбирался только по `.yml`, `.yaml` пропускался); не повторяем его здесь,
хотя на сегодняшний день `.markdown`-файлов в репозитории нет.

Проверка в обе стороны:
  1. Прямое: каждый файл документации из наблюдаемых каталогов обязан быть
     упомянут в `docs/INDEX.md` markdown-ссылкой `[текст](путь)`, реально
     резолвящейся (относительно `docs/`) в этот файл — не подстрокой имени
     файла где-то в тексте.
  2. Обратное: каждая локальная (не `http(s)://`, не якорь `#...`) ссылка
     `docs/INDEX.md` обязана резолвиться в существующий на диске файл —
     битая карта хуже неполной.

Газ намеренного исключения — маркер `<!-- DOCS-INDEX-OK: <причина> -->`,
начинающий строку файла документа (после пробелов). Пустая причина не
считается газом (regex требует непустой хвост, тот же приём, что
`orphan_test_guard.py::EXEMPTION_RE`).

При нуле найденных файлов документации в наблюдаемых каталогах гвардия
падает громко (`RuntimeError`) — пустой список входов иначе даёт зелёный
отчёт по неверной причине (тот же класс, что и «список тестов внезапно
пуст» у orphan_test_guard.py).

Запуск:
  python scripts/lib/docs_index_guard.py       # живой снимок, exit 1 при находках
  python -m pytest scripts/lib/test_docs_index_guard.py -q
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"
INDEX_MD = DOCS_DIR / "INDEX.md"

# Наблюдаемые каталоги — repo-relative posix, обоснование в докстринге модуля.
OBSERVED_DIRS = ("docs/decisions", "docs/research", "docs/runbooks", "docs/agents")

# Оба markdown-расширения — см. докстринг («скан по одному суффиксу», #635).
DOC_EXTENSIONS = {".md", ".markdown"}

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

# Анкер на начало строки (после пробелов/`<!--`) — не подстрока где угодно,
# тот же приём, что EXEMPTION_RE в orphan_test_guard.py.
_EXEMPTION_RE = re.compile(
    r"^\s*<!--\s*DOCS-INDEX-OK:\s*(\S.*\S|\S)\s*-->\s*$", re.MULTILINE
)


def _relpath(path: Path, root: Path = REPO_ROOT) -> str:
    return path.relative_to(root).as_posix()


def discover_doc_files(repo_root: Path = REPO_ROOT, dirs: tuple[str, ...] = OBSERVED_DIRS) -> list[str]:
    """Файловая система, не список в коде. Repo-relative posix-пути,
    отсортированные, по всем наблюдаемым каталогам и обоим расширениям."""
    found: list[str] = []
    for rel_dir in dirs:
        base = repo_root / rel_dir
        if not base.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                if Path(name).suffix.lower() in DOC_EXTENSIONS:
                    found.append(_relpath(Path(dirpath) / name, repo_root))
    return sorted(found)


def read_exemption(path: Path) -> str | None:
    """Газ (докстринг модуля, требование 4): `<!-- DOCS-INDEX-OK: причина -->`
    началом строки. Пустая/отсутствующая причина — не газ."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = _EXEMPTION_RE.search(text)
    return match.group(1).strip() if match else None


def _is_local_link(target: str) -> bool:
    if target.startswith("#"):
        return False
    scheme = urlparse(target).scheme
    return scheme in ("", "file")


def _resolve_link(target: str, index_dir: Path = DOCS_DIR, repo_root: Path = REPO_ROOT) -> str | None:
    """Ссылка `target` из docs/INDEX.md, резолвленная в repo-relative posix
    путь. `None`, если ссылка внешняя/якорная (см. `_is_local_link`)."""
    if not _is_local_link(target):
        return None
    fragment_free = target.split("#", 1)[0]
    if not fragment_free:
        return None
    combined = (index_dir / fragment_free).resolve()
    try:
        return combined.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None


def find_local_links(index_text: str) -> list[str]:
    """Все локальные ссылки `docs/INDEX.md`, резолвленные в repo-relative
    posix-пути (дубликаты сохраняются — вызывающему это не мешает, оба
    потребителя работают с множествами/фильтрами)."""
    resolved: list[str] = []
    for raw in _LINK_RE.findall(index_text):
        target = _resolve_link(raw)
        if target is not None:
            resolved.append(target)
    return resolved


def build_report(repo_root: Path = REPO_ROOT, dirs: tuple[str, ...] = OBSERVED_DIRS,
                  index_md: Path = INDEX_MD) -> dict:
    doc_files = discover_doc_files(repo_root, dirs)
    if not doc_files:
        raise RuntimeError(
            f"ноль файлов документации найдено в наблюдаемых каталогах {dirs} — "
            "похоже, сломан обход, а не репозиторий опустел (fail loud вместо "
            "тихого зелёного отчёта)"
        )

    index_text = index_md.read_text(encoding="utf-8")
    linked = set(find_local_links(index_text))

    exemptions: dict[str, str] = {}
    for rel in doc_files:
        reason = read_exemption(repo_root / rel)
        if reason:
            exemptions[rel] = reason

    prefixes = tuple(d.rstrip("/") + "/" for d in dirs)
    missing = sorted(
        rel for rel in doc_files
        if rel not in linked and rel not in exemptions
    )
    broken_links = sorted(
        target for target in linked
        if target.startswith(prefixes) and not (repo_root / target).is_file()
    )
    return {
        "total": len(doc_files),
        "linked": sorted(linked),
        "exemptions": exemptions,
        "missing": missing,
        "broken_links": broken_links,
    }


def main() -> int:
    report = build_report()
    print(
        f"docs-index-guard: файлов документации найдено {report['total']}, "
        f"газ (осознанные исключения) {len(report['exemptions'])}"
    )
    for path, reason in sorted(report["exemptions"].items()):
        print(f"  газ: {path} — {reason}")

    ok = True
    for path in report["missing"]:
        ok = False
        print(f"::error::{path} не упомянут в docs/INDEX.md — добавь ссылку "
              f"в соответствующий раздел карты документации")
    for target in report["broken_links"]:
        ok = False
        print(f"::error::docs/INDEX.md ссылается на {target}, которого нет на "
              f"диске — почини или удали ссылку")
    if ok:
        print("docs-index-guard: карта документации полна, битых ссылок нет")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
