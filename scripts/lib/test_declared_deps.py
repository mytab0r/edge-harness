#!/usr/bin/env python3
"""Тесты детектора рассинхрона «тело называет блокирующую issue — граф не
знает» (scripts/lib/declared_deps.py, задача #371).

Фрагменты тел — прод-форма: реальный текст открытых issues этого репозитория
на момент внедрения (#258 действительно называет #133 «Единственный
настоящий блокер», #243 упоминает #201 в перечислении «Поглощает часть
#227… и часть #201», не как зависимость — оба случая пойманы живым замером
при разработке #371, не выдуманы для теста).

Запуск: python -m pytest scripts/lib/test_declared_deps.py -q
"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).with_name("declared_deps.py")
spec = importlib.util.spec_from_file_location("declared_deps", SCRIPT)
dd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dd)  # type: ignore[union-attr]


# ── declared_candidates: формулировки зависимости ───────────────────────────


def test_declared_candidates_finds_blocker_noun_form():
    # #258, прод-текст: «блокер» существительным, не глагольной формой
    body = (
        "Единственный настоящий блокер — #133 (egress api.github.com из "
        "воркера морды даёт 403)."
    )
    assert dd.declared_candidates(body) == {133}


def test_declared_candidates_finds_zavisit_ot():
    assert dd.declared_candidates("Также зависит от #170 (форма сообщений).") == {170}


def test_declared_candidates_finds_zablokirovan_stem_forms():
    assert dd.declared_candidates("Заблокировано #80 (PoC плагинной системы).") == {80}
    assert dd.declared_candidates("Заблокирована #170 форма сообщений.") == {170}


def test_declared_candidates_ignores_posle_across_closed_paren():
    # #243, прод-текст: «после» относится к «слиянию» ВНУТРИ скобки, #201 —
    # следующий пункт перечисления за закрытой скобкой, не зависимость.
    body = (
        "Поглощает часть #227 (закрытие задач после слияния) и часть #201 "
        "(дедупликация по отпечатку)."
    )
    assert dd.declared_candidates(body) == set()


def test_declared_candidates_case_insensitive_trigger():
    assert dd.declared_candidates("ПОСЛЕ #99 закрытия конвейера.") == {99}


def test_declared_candidates_no_trigger_no_candidates():
    assert dd.declared_candidates("Обычный текст с номером #55 без формулировки.") == set()


def test_declared_candidates_finds_canonical_form_field_render():
    # Находка AI-ревью PR #387: канонический рендер GitHub обязательного поля
    # формы `### <label>\n\n<ответ>` (task.yml/white-spot.yml, id blocked_by)
    # не пересекался окном _TRIGGER_RE (не проходит через пустую строку между
    # заголовком и значением) — ровно тот путь, где перенос в граф ручной.
    body = (
        "### Цель\n\nСделать штуку.\n\n"
        "### Чем блокируется\n\n#123 #124\n\n"
        "### Контекст и ссылки\n\nпросто текст без формулировки зависимости"
    )
    assert dd.declared_candidates(body) == {123, 124}


def test_declared_candidates_form_field_nichem_no_candidates():
    body = "### Чем блокируется\n\nничем\n\n### Что блокирует\n\n#55"
    assert dd.declared_candidates(body) == set()


# ── find_desync: тело называет открытую задачу, граф не знает ──────────────


def test_find_desync_flags_missing_native_edge():
    issues = [
        {"number": 258, "body": "Единственный настоящий блокер — #133.", "blocked_by_open": []},
        {"number": 133, "body": "Ничего не блокирует.", "blocked_by_open": []},
    ]
    findings = dd.find_desync(issues)
    assert findings == [{"issue": 258, "declared_blocking": 133}]


def test_find_desync_clean_when_native_edge_already_present():
    # Мутация: тот же текст, но связь УЖЕ проставлена в графе — находки нет.
    issues = [
        {"number": 258, "body": "Единственный настоящий блокер — #133.", "blocked_by_open": [133]},
        {"number": 133, "body": "Ничего не блокирует.", "blocked_by_open": []},
    ]
    assert dd.find_desync(issues) == []


def test_find_desync_ignores_reference_to_closed_issue_not_in_pool():
    # #105 упомянут («после #105»), но не входит в открытый пул (уже закрыт)
    # — не рассинхрон: закрытая ссылка не влияет на приоритет.
    issues = [
        {"number": 111, "body": "после #105 журнал станет доступен.", "blocked_by_open": []},
    ]
    assert dd.find_desync(issues) == []


def test_find_desync_ignores_false_positive_enumeration():
    issues = [
        {
            "number": 243,
            "body": (
                "Поглощает часть #227 (закрытие задач после слияния) и часть "
                "#201 (дедупликация по отпечатку)."
            ),
            "blocked_by_open": [],
        },
        {"number": 201, "body": "Дедупликация.", "blocked_by_open": []},
        {"number": 227, "body": "Закрытие после слияния.", "blocked_by_open": []},
    ]
    assert dd.find_desync(issues) == []


def test_find_desync_ignores_self_reference():
    issues = [
        {"number": 5, "body": "После #5 ничего не меняется.", "blocked_by_open": []},
    ]
    assert dd.find_desync(issues) == []


# ── form_field_numbers: структурный ответ поля, не любое «#N» в теле ────────

# Реальный ответ `gh api repos/mytab0r/edge-harness/issues/529 --jq .body`
# (задача #529, эта же задача, заведённая перед реализацией) — прод-форма,
# не пересказ: секция «Чем блокируется» отвечает «ничем», а секция «Контекст
# и ссылки» ниже упоминает #361/#371/#387 — ровно тот класс ложного
# срабатывания, которого структурный разбор обязан избежать (наивный поиск
# «любое #N в теле» дал бы [361, 371, 387], что неверно).
ISSUE_529_BODY = (
    "### Цель\n\n"
    "Поле «Чем блокируется» (обязательное с #387, шаблоны `task.yml`/`white-spot.yml`)\n"
    "должно автоматически становиться связью `blockedBy` графа зависимостей для\n"
    "ЛЮБОГО пути заведения задачи (форма, API, агент) — сегодня перенос делает\n"
    "только `wire_declared_dependency` в `file_tasks.py` (АИ-ревью-путь), для\n"
    "остальных нужна ручная команда `task_deps.py block`.\n\n"
    "### Критерий готовности\n\n"
    "- Открытая задача любого происхождения с заполненным полем «Чем блокируется»\n"
    "  и номером, которого нет в нативном `blockedBy`, получает эту связь\n"
    "  автоматически на ближайшем прогоне без ручной команды.\n"
    "- Поле разбирается структурно (по заголовку секции формы `### Чем\n"
    "  блокируется`), не по любому `#N` в теле — ложная связь опаснее отсутствующей.\n"
    "- Явный ответ «ничем» не создаёт предупреждений и не считается пропуском.\n"
    "- Тесты на мутацию прод-формы (реальный рендер поля формы, реальные тела\n"
    "  открытых задач) зелёные.\n\n"
    "### Площадь\n\n"
    "area:process\n\n"
    "### Чем блокируется\n\n"
    "ничем\n\n"
    "### Что блокирует\n\n"
    "ничем\n\n"
    "### Контекст и ссылки\n\n"
    "Продолжение #361 (`scripts/lib/task_deps.py`, граф блокировок) и #371\n"
    "(`scripts/lib/declared_deps.py`, `wire_declared_dependency` в\n"
    "`scripts/review/file_tasks.py`) — тот же класс, что закрыт для АИ-ревью-пути,\n"
    "здесь нужен для человеческого/шаблонного пути. `design.md` #361/#371 сознательно\n"
    "оставлял человеческий путь ручным («форма не структурная для программных\n"
    "создателей») — пересматривается прямым решением владельца, обоснование в\n"
    "`design.md` этой задачи.\n"
)


def test_form_field_numbers_real_body_nichem_ignores_unrelated_numbers_below():
    # Прод-тело реальной issue #529: поле отвечает «ничем», а несвязанные
    # #361/#371/#387 ниже по телу НЕ должны попасть в результат.
    assert dd.form_field_numbers(ISSUE_529_BODY) == []


def test_form_field_numbers_none_when_section_absent():
    assert dd.form_field_numbers("Обычное тело безо всякой формы.") is None


def test_form_field_numbers_case_insensitive_nichem():
    body = "### Чем блокируется\n\nНичем\n\n### Контекст\n\nтекст"
    assert dd.form_field_numbers(body) == []


def test_form_field_numbers_parses_canonical_render():
    # Тот же канонический рендер, что уже проверен для declared_candidates
    # (находка AI-ревью PR #387) — здесь для отдельного структурного разбора.
    body = (
        "### Цель\n\nСделать штуку.\n\n"
        "### Чем блокируется\n\n#123 #124\n\n"
        "### Контекст и ссылки\n\nссылка на #999 в другом разделе"
    )
    assert dd.form_field_numbers(body) == [123, 124]


# ── auto_wire: единый перенос поля формы в граф (задача #529) ───────────────


class FakeTaskDeps:
    """Двойник task_deps.py для `auto_wire`: без сети и без импорта настоящего
    модуля — тестируем только логику declared_deps.py (form_field_numbers +
    фильтр «уже в графе» + делегирование в wire_dependencies)."""

    def __init__(self, issues):
        self.issues = issues
        self._default_gh = object()
        self.wire_calls: list[tuple[int, list[int]]] = []

    def fetch_pool(self, repo, label="task", include_body=False, gh_call=None):
        return self.issues

    def wire_dependencies(self, repo, blocked, blocking_numbers, open_numbers, gh_call=None, log=print):
        self.wire_calls.append((blocked, list(blocking_numbers)))
        return list(blocking_numbers)


def test_auto_wire_links_declared_numbers_missing_from_graph(monkeypatch):
    issues = [
        {"number": 500, "body": "### Чем блокируется\n\n#55\n", "blocked_by_open": []},
        {"number": 55, "body": "### Чем блокируется\n\nничем\n", "blocked_by_open": []},
    ]
    fake = FakeTaskDeps(issues)
    monkeypatch.setattr(dd, "_load_task_deps", lambda: fake)
    report = dd.auto_wire("owner/repo")
    assert report == [{"issue": 500, "linked": [55]}]
    assert fake.wire_calls == [(500, [55])]


def test_auto_wire_idempotent_when_already_native_no_network_calls(monkeypatch):
    # Мутация: тот же ответ поля, но связь УЖЕ проставлена в графе — не должно
    # быть повторного вызова wire_dependencies (сетевого добавления связи).
    issues = [
        {"number": 500, "body": "### Чем блокируется\n\n#55\n", "blocked_by_open": [55]},
    ]
    fake = FakeTaskDeps(issues)
    monkeypatch.setattr(dd, "_load_task_deps", lambda: fake)
    report = dd.auto_wire("owner/repo")
    assert report == []
    assert fake.wire_calls == []


def test_auto_wire_ignores_missing_field_and_nichem(monkeypatch):
    issues = [
        {"number": 1, "body": "Тело без поля формы вовсе.", "blocked_by_open": []},
        {"number": 2, "body": "### Чем блокируется\n\nничем\n", "blocked_by_open": []},
    ]
    fake = FakeTaskDeps(issues)
    monkeypatch.setattr(dd, "_load_task_deps", lambda: fake)
    report = dd.auto_wire("owner/repo")
    assert report == []
    assert fake.wire_calls == []


def test_auto_wire_real_issue_529_body_yields_nothing():
    # Прод-тело: поле «ничем» — auto_wire не должен даже пытаться дойти до
    # wire_dependencies для этой issue (никакой FakeTaskDeps не нужен — форма
    # уже отфильтрована на form_field_numbers).
    assert dd.form_field_numbers(ISSUE_529_BODY) == []


def test_auto_wire_skips_closed_blocker_without_delegate_or_warning(monkeypatch):
    # Находка AI-ревью PR #537: `blocked_by_open` содержит только ОТКРЫТЫХ
    # блокеров, поэтому на push после закрытия #55 у задачи с полем «#55»
    # каждый прогон снова давал missing=[55] и предупреждение «связь НЕ
    # поставлена» — хотя связь стоит, а ссылка на закрытую «не влияет ни на
    # что». Фильтр до делегирования: ни вызова wire_dependencies, ни лога.
    issues = [
        {"number": 500, "body": "### Чем блокируется\n\n#55\n", "blocked_by_open": []},
    ]
    fake = FakeTaskDeps(issues)  # пул не содержит #55 — она закрыта
    monkeypatch.setattr(dd, "_load_task_deps", lambda: fake)
    report = dd.auto_wire("owner/repo")
    assert report == []
    assert fake.wire_calls == []


def test_auto_wire_filters_closed_and_self_before_delegate_keeps_open(monkeypatch):
    # Мутационная гвардия фильтра: из поля «#55 #56 #500» (55 закрыт, 500 —
    # сама issue) до wire_dependencies доходит только открытый чужой #56 —
    # тот же делегат, что и раньше, но без повторяемого шума.
    issues = [
        {
            "number": 500,
            "body": "### Чем блокируется\n\n#55 #56 #500\n",
            "blocked_by_open": [],
        },
        {"number": 56, "body": "### Чем блокируется\n\nничем\n", "blocked_by_open": []},
    ]
    fake = FakeTaskDeps(issues)
    monkeypatch.setattr(dd, "_load_task_deps", lambda: fake)
    report = dd.auto_wire("owner/repo")
    assert report == [{"issue": 500, "linked": [56]}]
    assert fake.wire_calls == [(500, [56])]


# ── CLI wire ─────────────────────────────────────────────────────────────────


def test_cli_wire_reports_nothing_to_link(monkeypatch, capsys):
    fake = FakeTaskDeps([{"number": 1, "body": "ничего", "blocked_by_open": []}])
    monkeypatch.setattr(dd, "_load_task_deps", lambda: fake)
    rc = dd.main(["wire", "owner/repo"])
    assert rc == 0
    assert "переносить нечего" in capsys.readouterr().out


def test_cli_wire_prints_linked_numbers(monkeypatch, capsys):
    # #55 в пуле (иначе фильтр auto_wire правильно не стал бы её линковать —
    # фикстура раньше притворялась линкуемым закрытым номером, находка
    # AI-ревью PR #537).
    issues = [
        {"number": 500, "body": "### Чем блокируется\n\n#55\n", "blocked_by_open": []},
        {"number": 55, "body": "### Чем блокируется\n\nничем\n", "blocked_by_open": []},
    ]
    fake = FakeTaskDeps(issues)
    monkeypatch.setattr(dd, "_load_task_deps", lambda: fake)
    rc = dd.main(["wire", "owner/repo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "#500" in out and "#55" in out
