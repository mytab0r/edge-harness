#!/usr/bin/env python3
"""Тесты гвардии «карта документации полна» (scripts/lib/docs_index_guard.py, #670).

Два слоя, каждый доказан отдельно:
  1. Чистая логика (`discover_doc_files`, `find_local_links`, `read_exemption`,
     `build_report`) — на фикстурах в `tmp_path`, без обращения к реальному
     репозиторию.
  2. Живой снимок текущего репозитория (`test_repo_docs_index_is_complete`,
     `test_repo_docs_index_has_no_broken_links`) — это и есть сама канарейка.
     Мутация, которой доказано прямое направление (живой прогон при починке
     #670): убери строку `[Roadmap потоков работ](agents/ROADMAP.md)` из
     `docs/INDEX.md` — `python scripts/lib/docs_index_guard.py` называет
     ровно `docs/agents/ROADMAP.md` и ничего лишнего (файлов найдено 29, газа
     0); верни строку — снова зелено. Обратное направление: временно впиши в
     `docs/INDEX.md` ссылку на несуществующий файл — гвардия красится списком
     `broken_links`; убери — снова зелено.

Запуск: python -m pytest scripts/lib/test_docs_index_guard.py -q
"""

import importlib.util
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("docs_index_guard", _DIR / "docs_index_guard.py")
dig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dig)  # type: ignore[union-attr]

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── Чистая логика: discover_doc_files ──────────────────────────────────────

def test_discover_doc_files_scans_both_markdown_extensions(tmp_path):
    (tmp_path / "docs" / "decisions").mkdir(parents=True)
    (tmp_path / "docs" / "decisions" / "0001-a.md").write_text("a", encoding="utf-8")
    (tmp_path / "docs" / "decisions" / "0002-b.markdown").write_text("b", encoding="utf-8")
    (tmp_path / "docs" / "decisions" / "readme.txt").write_text("c", encoding="utf-8")
    found = dig.discover_doc_files(tmp_path, ("docs/decisions",))
    assert found == ["docs/decisions/0001-a.md", "docs/decisions/0002-b.markdown"]


def test_discover_doc_files_ignores_unobserved_directory(tmp_path):
    (tmp_path / "docs" / "api_extra").mkdir(parents=True)
    (tmp_path / "docs" / "api_extra" / "x.md").write_text("x", encoding="utf-8")
    found = dig.discover_doc_files(tmp_path, ("docs/decisions",))
    assert found == []


# ── Чистая логика: find_local_links / _resolve_link ────────────────────────

def test_find_local_links_resolves_relative_to_docs_dir():
    text = "- [ADR 1](decisions/0001-a.md)\n- [Внешнее](https://example.com/x.md)\n"
    links = dig.find_local_links(text)
    assert links == ["docs/decisions/0001-a.md"]


def test_find_local_links_ignores_bare_anchor():
    text = "- [Якорь](#раздел)\n"
    assert dig.find_local_links(text) == []


def test_find_local_links_strips_fragment_from_local_link():
    text = "- [ADR 1](decisions/0001-a.md#alternatives)\n"
    assert dig.find_local_links(text) == ["docs/decisions/0001-a.md"]


def test_find_local_links_excludes_link_escaping_repo_root():
    # Второй гейт #673, найденный после первого круга: ссылка, чей `..`
    # уводит за repo_root, не репо-относительный путь — find_local_links её
    # не должен возвращать (раньше терялась в одном `None` с внешними).
    text = "- [Наружу](../../GHOST-OUTSIDE.md)\n"
    assert dig.find_local_links(text) == []


def test_find_escaped_links_reports_link_escaping_repo_root():
    text = (
        "- [ADR 1](decisions/0001-a.md)\n"
        "- [Наружу](../../GHOST-OUTSIDE.md)\n"
    )
    assert dig.find_escaped_links(text) == ["../../GHOST-OUTSIDE.md"]
    # Не пересекается с find_local_links.
    assert "../../GHOST-OUTSIDE.md" not in dig.find_local_links(text)


# ── Газ (легальные исключения) ─────────────────────────────────────────────

def test_read_exemption_requires_non_empty_reason(tmp_path):
    with_reason = tmp_path / "with.md"
    with_reason.write_text("<!-- DOCS-INDEX-OK: черновик, не публикуется -->\n", encoding="utf-8")
    assert dig.read_exemption(with_reason) == "черновик, не публикуется"

    empty = tmp_path / "empty.md"
    empty.write_text("<!-- DOCS-INDEX-OK:  -->\n", encoding="utf-8")
    assert dig.read_exemption(empty) is None

    absent = tmp_path / "absent.md"
    absent.write_text("обычный текст без маркера\n", encoding="utf-8")
    assert dig.read_exemption(absent) is None


def test_marker_mention_mid_line_does_not_self_exempt(tmp_path):
    # Тот же класс регресса, что у orphan_test_guard.py: строка, которая лишь
    # УПОМИНАЕТ литерал маркера (не начинает им строку исходника), не должна
    # засчитываться как объявленный газ.
    path = tmp_path / "mentions.md"
    path.write_text('какой-то текст `<!-- DOCS-INDEX-OK: чужая причина -->` в кавычках\n', encoding="utf-8")
    assert dig.read_exemption(path) is None


# ── build_report: обе мутации ───────────────────────────────────────────────

def _write_docs_fixture(tmp_path: Path, index_body: str) -> Path:
    docs = tmp_path / "docs"
    (docs / "decisions").mkdir(parents=True)
    (docs / "decisions" / "0001-a.md").write_text("a", encoding="utf-8")
    (docs / "decisions" / "0002-b.md").write_text("b", encoding="utf-8")
    (docs / "INDEX.md").write_text(index_body, encoding="utf-8")
    return docs / "INDEX.md"


def test_build_report_flags_unlinked_file(tmp_path):
    index_md = _write_docs_fixture(
        tmp_path, "- [ADR 1](decisions/0001-a.md)\n"  # 0002-b.md не упомянут
    )
    report = dig.build_report(tmp_path, ("docs/decisions",), index_md)
    assert report["missing"] == ["docs/decisions/0002-b.md"]
    assert report["broken_links"] == []


def test_build_report_flags_broken_link(tmp_path):
    index_md = _write_docs_fixture(
        tmp_path,
        "- [ADR 1](decisions/0001-a.md)\n"
        "- [ADR 2](decisions/0002-b.md)\n"
        "- [Призрак](decisions/9999-ghost.md)\n",
    )
    report = dig.build_report(tmp_path, ("docs/decisions",), index_md)
    assert report["missing"] == []
    assert report["broken_links"] == ["docs/decisions/9999-ghost.md"]


def test_build_report_flags_broken_link_outside_observed_dirs(tmp_path):
    # Второй гейт #673: обратная проверка не должна быть сужена молча до
    # наблюдаемых каталогов — ссылка на файл-призрак ЗА их пределами обязана
    # красить отчёт так же, как призрак внутри них.
    index_md = _write_docs_fixture(
        tmp_path,
        "- [ADR 1](decisions/0001-a.md)\n"
        "- [ADR 2](decisions/0002-b.md)\n"
        "- [Ghost sibling](api-ghost.md)\n"
        "- [Ghost outside docs/](../GHOST.md)\n"
        "- [Ghost nested elsewhere](../openspec/ghost/spec.md)\n",
    )
    report = dig.build_report(tmp_path, ("docs/decisions",), index_md)
    assert report["missing"] == []
    assert report["broken_links"] == [
        "GHOST.md",
        "docs/api-ghost.md",
        "openspec/ghost/spec.md",
    ]


def test_build_report_flags_link_escaping_repo_root(tmp_path):
    # Второй гейт #673, найденный после первого круга: ссылка, чей `..`
    # уводит за repo_root, раньше тонула в том же `None`, что честные внешние
    # ссылки, и обратная проверка её не видела никогда (живая улика —
    # `../../GHOST-OUTSIDE.md` на реальном docs/INDEX.md проходил exit 0).
    index_md = _write_docs_fixture(
        tmp_path,
        "- [ADR 1](decisions/0001-a.md)\n"
        "- [ADR 2](decisions/0002-b.md)\n"
        "- [Наружу](../../GHOST-OUTSIDE.md)\n",
    )
    report = dig.build_report(tmp_path, ("docs/decisions",), index_md)
    assert report["missing"] == []
    assert report["broken_links"] == ["../../GHOST-OUTSIDE.md"]


def test_build_report_exemption_suppresses_missing(tmp_path):
    docs = tmp_path / "docs"
    (docs / "decisions").mkdir(parents=True)
    (docs / "decisions" / "0001-a.md").write_text("a", encoding="utf-8")
    (docs / "decisions" / "0002-draft.md").write_text(
        "<!-- DOCS-INDEX-OK: черновик, ещё не готов к публикации -->\n", encoding="utf-8"
    )
    index_md = docs / "INDEX.md"
    index_md.write_text("- [ADR 1](decisions/0001-a.md)\n", encoding="utf-8")
    report = dig.build_report(tmp_path, ("docs/decisions",), index_md)
    assert report["missing"] == []
    assert report["exemptions"] == {"docs/decisions/0002-draft.md": "черновик, ещё не готов к публикации"}


def test_build_report_raises_loud_on_unlisted_docs_subdir(tmp_path):
    # Доделка «хвоста» второго гейта #673: новый подкаталог docs/, не входящий
    # ни в наблюдаемые, ни в явные исключения, не должен ускользать молча.
    docs = tmp_path / "docs"
    (docs / "decisions").mkdir(parents=True)
    (docs / "decisions" / "0001-a.md").write_text("a", encoding="utf-8")
    (docs / "design").mkdir()  # новый каталог, не заявленный нигде
    (docs / "design" / "x.md").write_text("x", encoding="utf-8")
    index_md = docs / "INDEX.md"
    index_md.write_text("- [ADR 1](decisions/0001-a.md)\n", encoding="utf-8")
    try:
        dig.build_report(tmp_path, ("docs/decisions",), index_md)
    except RuntimeError as exc:
        assert "docs/design" in str(exc)
    else:
        raise AssertionError(
            "build_report обязан падать громко на подкаталоге docs/, "
            "не входящем ни в OBSERVED_DIRS, ни в EXCLUDED_TOP_LEVEL_DIRS"
        )


def test_find_unlisted_docs_dirs_respects_excluded_list(tmp_path):
    docs = tmp_path / "docs"
    (docs / "decisions").mkdir(parents=True)
    (docs / "vendor").mkdir()
    unlisted = dig.find_unlisted_docs_dirs(
        tmp_path, docs, ("docs/decisions",), {"docs/vendor": "сторонний снапшот, не наша документация"}
    )
    assert unlisted == []


def test_build_report_raises_loud_on_zero_files(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    index_md = docs / "INDEX.md"
    index_md.write_text("пусто\n", encoding="utf-8")
    try:
        dig.build_report(tmp_path, ("docs/decisions",), index_md)
    except RuntimeError:
        pass
    else:
        raise AssertionError("build_report обязан падать громко при нуле найденных файлов")


# ── Живой снимок репозитория (сама канарейка) ───────────────────────────────

def test_repo_docs_index_is_complete():
    report = dig.build_report()
    assert report["missing"] == [], (
        "документы вне карты docs/INDEX.md (живой класс #670 — 0015 не попал "
        f"в индекс из PR #667): {report['missing']} — добавь ссылку в "
        "соответствующий раздел docs/INDEX.md"
    )


def test_repo_docs_index_has_no_broken_links():
    report = dig.build_report()
    assert report["broken_links"] == [], (
        f"docs/INDEX.md ссылается на несуществующие файлы: {report['broken_links']} — "
        "битая карта хуже неполной, почини или удали ссылку"
    )


def test_repo_discovers_all_four_observed_dirs():
    # Страховка от тихо сломанного обхода: пустой список файлов в любом
    # наблюдаемом каталоге не должен молча дать пустой отчёт (тот же класс,
    # что и защита orphan_test_guard.py от «файлов найдено подозрительно
    # мало»). На 2026-09-07 в репозитории 29 файлов документации в четырёх
    # наблюдаемых каталогах — порог занижен вдвое, чтобы не переписывать
    # тест на каждый новый ADR.
    report = dig.build_report()
    assert report["total"] >= 15, f"найдено подозрительно мало файлов документации: {report['total']}"


# ── Самопроверка: канарейка не может осиротеть сама (тот же класс, что #583) ─

def test_own_test_file_is_named_in_repo_ci():
    repo_ci = (REPO_ROOT / ".github" / "workflows" / "repo-ci.yml").read_text(encoding="utf-8")
    assert "scripts/lib/test_docs_index_guard.py" in repo_ci, (
        "гвардия полноты карты документации сама не подключена к repo-ci.yml — "
        "первая же осиротела бы (класс #583)"
    )
