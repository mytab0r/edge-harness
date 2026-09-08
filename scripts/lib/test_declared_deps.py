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


def test_declared_candidates_form_only_body_yields_nothing():
    # С #529 структурное поле формы — НЕ эвристика: его переносит
    # детерминированный auto_wire, и предупреждение на него было бы шумом о
    # том, что тот же прогон чинит (находка AI-ревью PR #537). Канонический
    # рендер поля без прозаической формулировки — пустой набор кандидатов.
    body = (
        "### Цель\n\nСделать штуку.\n\n"
        "### Чем блокируется\n\n#123 #124\n\n"
        "### Контекст и ссылки\n\nпросто текст без формулировки зависимости"
    )
    assert dd.declared_candidates(body) == set()
    # Прозаическая формулировка рядом продолжает матчиться — эвристика жива.
    assert dd.declared_candidates("### Чем блокируется\n\n#123\n\nзависит от #124") == {124}


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


def test_find_desync_ignores_form_field_number_auto_wire_owns_it():
    # Находка AI-ревью PR #537: номер из структурного поля формы, которого
    # ещё нет в графе, НЕ находка детектора — тот же прогон repo-ci ставит
    # связь через auto_wire (шаг ниже детектора), предупреждение было бы
    # шумом о том, что этот же прогон чинит.
    issues = [
        {"number": 500, "body": "### Чем блокируется\n\n#55\n", "blocked_by_open": []},
        {"number": 55, "body": "### Чем блокируется\n\nничем\n", "blocked_by_open": []},
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


# ── #710: живой рендер уровнем «##» и без пустой строки ─────────────────────

# Прод-тело `gh api repos/mytab0r/edge-harness/issues/601 --jq .body` (замер
# 2026-09-08, живой пул): заголовок уровнем `##`, БЕЗ пустой строки между
# заголовком и ответом («## Чем блокируется\nничем\n\n## Что блокирует\n
# ничем\n\n») — ровно тот рендер, который старый `_FORM_FIELD_RE` (жёстко
# `###\n\s*\n`) не видел вовсе (8% покрытия живого пула, задача #710).
ISSUE_601_BODY = (
    "## Цель\n"
    "Газ гвардии отката (`revert-ok`, #217) и вердикт ревью "
    "(`review:changes-requested`/`review:ok`) достижимы без нового коммита.\n\n"
    "## Критерий готовности\n"
    "`scripts/review/check_pr.py` читает тело PR, но `pr-review.yml` слушает "
    "только opened/synchronize/reopened.\n\n"
    "## Площадь\narea:orchestra\n\n"
    "## Чем блокируется\nничем\n\n"
    "## Что блокирует\nничем\n\n"
    "## Контекст и ссылки\nНайдено при разборе #599.\n"
)


def test_form_field_numbers_real_body_level2_no_blank_line_nichem():
    # Прод-тело #601 без единой правки формата — уровень ## и без пустой
    # строки: обязано разбираться так же, как канонический ### с пустой
    # строкой (см. test_form_field_numbers_parses_canonical_render).
    assert dd.form_field_numbers(ISSUE_601_BODY) == []
    assert dd.blocking_field_numbers(ISSUE_601_BODY) == []


def test_form_field_numbers_level2_no_blank_line_real_number():
    # Мутация прод-формы #601: подставлен номер вместо «ничем» — доказывает,
    # что разбор реально читает ЗНАЧЕНИЕ поля, а не просто узнаёт «ничем»
    # как отдельный частный случай.
    body = ISSUE_601_BODY.replace(
        "## Чем блокируется\nничем", "## Чем блокируется\n#217 #599")
    assert dd.form_field_numbers(body) == [217, 599]


def test_blocking_field_numbers_level2_no_blank_line_real_number():
    body = ISSUE_601_BODY.replace(
        "## Что блокирует\nничем", "## Что блокирует\n#264")
    assert dd.blocking_field_numbers(body) == [264]


def test_form_field_numbers_ignores_adhoc_heading_not_exact_field_text():
    # Прод-тело #373 (замер 2026-09-08, живой пул): заголовок «## Блокирует»
    # — СВОБОДНАЯ формулировка того же смысла, но НЕ точный текст поля формы
    # «Чем блокируется»/«Что блокирует». Разбор структурного поля обязан её
    # игнорировать (иначе это уже не разбор схемы, а жадный греп по любому
    # заголовку со словом «блокир*») — она остаётся в зоне прозаической
    # эвристики (_TRIGGER_RE/find_desync), не автозаписи.
    body = (
        "## Цель\n\nГейт вклада плагина в серверный бандл.\n\n"
        "## Блокирует\n\nБлокирует #347 («Откалибровать порог»).\n\n"
        "## Площадь\n\narea:worker\n"
    )
    assert dd.form_field_numbers(body) is None
    assert dd.blocking_field_numbers(body) is None


# ── declared_blocked_by: объединение заголовка и инлайн-конвенции ──────────


def test_declared_blocked_by_none_when_neither_form_present():
    assert dd.declared_blocked_by("Обычное тело безо всякой формы.") is None


def test_declared_blocked_by_reads_heading_form():
    body = "## Чем блокируется\n#55 #56\n\n## Контекст\nтекст"
    assert dd.declared_blocked_by(body) == [55, 56]


def test_declared_blocked_by_reads_inline_ai_review_convention():
    # Контракт `ai_review.blocked_by_numbers` — последняя непустая строка
    # тела; прод-форма реальных тел #679/#626 (замер 2026-09-08): секция
    # прозы, затем «БЛОКИРУЕТСЯ: …» последней строкой, без заголовков формы.
    body = (
        "Инцидент 2026-09-06 — класс «квота пробита, конвейер не заметил».\n"
        "БЛОКИРУЕТСЯ: #605"
    )
    assert dd.declared_blocked_by(body) == [605]


def test_declared_blocked_by_inline_nichem_is_explicit_empty_not_none():
    body = "Текст задачи.\nБЛОКИРУЕТСЯ: ничем"
    assert dd.declared_blocked_by(body) == []


def test_declared_blocked_by_union_when_both_present():
    body = "## Чем блокируется\n#55\n\nБЛОКИРУЕТСЯ: #56"
    assert dd.declared_blocked_by(body) == [55, 56]


# ── _load_ai_review: module-level memo (находка ревью PR #711) ─────────────


def test_load_ai_review_memoized_across_calls(monkeypatch):
    # Мутация: без memo (снять `if _ai_review_module is None:` — вернуть
    # безусловное exec_module на каждый вызов) этот тест обязан покраснеть,
    # потому что spec_from_file_location будет вызван дважды, не один раз.
    monkeypatch.setattr(dd, "_ai_review_module", None)
    calls = []
    real_spec = importlib.util.spec_from_file_location

    def counting_spec(name, *a, **k):
        if name == "ai_review":
            calls.append(name)
        return real_spec(name, *a, **k)

    monkeypatch.setattr(importlib.util, "spec_from_file_location", counting_spec)
    first = dd._load_ai_review()
    second = dd._load_ai_review()
    assert first is second
    assert len(calls) == 1


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


def test_auto_wire_level2_no_blank_line_real_body_still_wires(monkeypatch):
    # Мутация прод-тела #601 (задача #710): без фикса `_FORM_FIELD_RE`
    # (уровень «##», без пустой строки) этот тест краснеет — auto_wire
    # раньше вообще не видел такое поле.
    body = ISSUE_601_BODY.replace(
        "## Чем блокируется\nничем", "## Чем блокируется\n#217")
    issues = [
        {"number": 601, "body": body, "blocked_by_open": []},
        {"number": 217, "body": "## Чем блокируется\nничем\n", "blocked_by_open": []},
    ]
    fake = FakeTaskDeps(issues)
    monkeypatch.setattr(dd, "_load_task_deps", lambda: fake)
    report = dd.auto_wire("owner/repo")
    assert report == [{"issue": 601, "linked": [217]}]


def test_auto_wire_wires_reverse_blocking_field(monkeypatch):
    # Задача #710: «Что блокирует» — обратное направление. #500 объявляет
    # «Что блокирует: #56» → после прогона #56 должна получить blockedBy на
    # #500, хотя #56 сама ничего о зависимости не говорит.
    issues = [
        {"number": 500, "body": "## Что блокирует\n#56\n", "blocked_by_open": []},
        {"number": 56, "body": "## Чем блокируется\nничем\n", "blocked_by_open": []},
    ]
    fake = FakeTaskDeps(issues)
    monkeypatch.setattr(dd, "_load_task_deps", lambda: fake)
    report = dd.auto_wire("owner/repo")
    assert report == [{"issue": 56, "linked": [500]}]
    assert fake.wire_calls == [(56, [500])]


def test_auto_wire_reverse_blocking_field_idempotent_when_already_native(monkeypatch):
    issues = [
        {"number": 500, "body": "## Что блокирует\n#56\n", "blocked_by_open": []},
        {"number": 56, "body": "## Чем блокируется\nничем\n", "blocked_by_open": [500]},
    ]
    fake = FakeTaskDeps(issues)
    monkeypatch.setattr(dd, "_load_task_deps", lambda: fake)
    report = dd.auto_wire("owner/repo")
    assert report == []
    assert fake.wire_calls == []


def test_auto_wire_wires_inline_ai_review_convention_on_any_push_not_only_creation(monkeypatch):
    # Задача #710: раньше инлайн «БЛОКИРУЕТСЯ: …» переносился ТОЛЬКО в момент
    # заведения (file_tasks.py); задача, чья зависимость на момент заведения
    # ещё не существовала (батч АИ-ревью), теряла связь навсегда. auto_wire
    # теперь пересматривает и эту форму на каждом push.
    issues = [
        {"number": 500, "body": "Текст задачи.\nБЛОКИРУЕТСЯ: #55", "blocked_by_open": []},
        {"number": 55, "body": "Текст.\nБЛОКИРУЕТСЯ: ничем", "blocked_by_open": []},
    ]
    fake = FakeTaskDeps(issues)
    monkeypatch.setattr(dd, "_load_task_deps", lambda: fake)
    report = dd.auto_wire("owner/repo")
    assert report == [{"issue": 500, "linked": [55]}]


def test_auto_wire_no_duplicate_network_call_when_both_directions_declare_same_pair(monkeypatch):
    # #500 объявляет «Чем блокируется: #56» И #56 отдельно объявляет «Что
    # блокирует: #500» — та же пара с двух сторон, избыточно, но не
    # запрещено. Обязан быть РОВНО один вызов wire_dependencies на пару.
    issues = [
        {"number": 500, "body": "## Чем блокируется\n#56\n", "blocked_by_open": []},
        {"number": 56, "body": "## Что блокирует\n#500\n", "blocked_by_open": []},
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
