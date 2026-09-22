#!/usr/bin/env python3
"""Гвардия класса «значение метки в query GitHub API не URL-кодировано» (#938).

Инцидент: `scripts/orchestra/waiting_owner_guard.py::open_waiting_owner_issues`
строил `f"repos/{repo}/issues?state=open&labels={WAITING_OWNER_LABEL}&per_page=100"`,
где `WAITING_OWNER_LABEL = "waiting:owner"`. Двоеточие не кодировалось —
GitHub интерпретирует его как часть URL-синтаксиса, не байт значения фильтра,
и молча отвечает пустым списком, а не ошибкой. Доказано напрямую API:

    $ gh api "repos/mytab0r/edge-harness/issues?state=open&labels=waiting:owner"
    []
    $ gh api "repos/mytab0r/edge-harness/issues?state=open&labels=waiting%3Aowner&per_page=100"
    [782, 133]

Следствие: детектор ответа владельца (`waiting_owner_check`) не видел ни одной
задачи с меткой `waiting:owner` — ответ владельца в #782 («РЕШЕНИЕ: 1»,
2026-09-09) двое суток не был обработан.

Класс, не точечный случай: любое место, подставляющее ПЕРЕМЕННОЕ значение
метки в query-параметр (`labels=`) f-строкой напрямую, ломается тем же
способом, как только значение получит двоеточие — а живые метки этого
репозитория (`review:ok`, `ai:failed`, `area:*`, …) двоеточие уже несут.
Одно место правды на кодирование — `review_labels.label_query_value`
(рядом с `review_labels.list_pages`, тем же местом правды на пагинацию,
#308) — эта гвардия по исходнику требует, чтобы КАЖДАЯ подстановка после
`labels=` шла именно через неё, а не проверяет отдельные файлы точечно.

Признак (AST, не текстовый regex — по образцу
`test_console_utf8_guard.py::_file_io_calls_missing_encoding`): в любом
f-string (`ast.JoinedStr`, включая неявную конкатенацию соседних строковых
литералов — Python склеивает их в один узел на этапе разбора, поэтому перенос
`f"...labels="` + `f"{label_query_value(...)}"` на две строки исходника не
уходит от проверки) константный кусок, заканчивающийся на один из ТРЁХ
суффиксов (`&labels=`, `?labels=`, `label:` — признаки «это позиция значения
метки в запросе»: первые два для `issues?labels=...`, третий для
`search/issues?q=label:...`; не любое упоминание слова «labels»/«label» —
находка живой прогонки: `pool_issue.py` несёт `f"...: labels="` в тексте
ОШИБКИ, не в URL, и не должен считаться нарушением), обязан быть немедленно
продолжен `FormattedValue`, чьё выражение — вызов кодировщика СВОЕЙ
поверхности: для суффиксов `labels=` — `review_labels.label_query_value(...)`,
для `label:` — `review_labels.label_search_value(...)` (или тот же вызов без
префикса, если модуль импортирован напрямую; привязка к поверхности —
замеренная, комментарий над `_ENCODER_BY_SUFFIX` ниже). Голый литерал
`labels=task` (без `{}` вовсе) — не мишень: тут нечего кодировать, значение
не переменное.

Честный потолок (по образцу `stale_blocked_guard.STALE_MARKER_RE`, который сам
называет свою узкую форму маркера): признак — ровно эти три суффикса. Четвёртая
форма запроса с меткой в значении, придуманная позже без одного из этих
суффиксов константы перед подстановкой, гвардией не покрыта — она обязана
получить собственный суффиксный признак здесь же, не тихо остаться дырой.

Запуск: python -m pytest scripts/lib/test_label_query_encoding_guard.py -q
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _production_scripts() -> list[Path]:
    """Все .py в scripts/, кроме тестов и самого модуля-хелпера — тесты
    гоняют собственные фикстуры URL (моки в test_*.py), это не то же самое,
    что запрос в проде; review_labels.py — место, где живёт сама функция
    кодирования, ей нечего требовать от себя."""
    return [
        path for path in (REPO_ROOT / "scripts").rglob("*.py")
        if not path.name.startswith("test_") and path.name != "review_labels.py"
    ]


# Две законные формы подстановки метки, и они НЕ взаимозаменяемы (#1432):
#   label_query_value  — метка как значение query-параметра (`issues?labels=`):
#                        кодируется процентами;
#   label_search_value — метка как часть выражения поиска
#                        (`search/issues?q=label:`): кодировать её здесь
#                        вторично значило бы послать `%253A`, а результат
#                        механического применения процентов (`label:%3A`…)
#                        движок поиска НЕ декодирует.
# Расширение предусмотрено докстрингом этого файла («третья форма обязана
# получить собственный признак здесь же, не тихо остаться дырой»): вторая
# форма пришла с #1432 и получила его.
# Кодировщик ПРИВЯЗАН к поверхности, а не «любой из двух» (находка ai-review
# PR #1435, и это не педантизм). Замер 2026-09-22
# (docs/research/21-github-actions.md) показал, что формы не просто разные —
# каждая ЛОМАЕТ чужую поверхность:
#   `issues?labels=<метка>` — значение query-ПАРАМЕТРА: без `%3A` серверный
#       фильтр отдаёт пустой список (класс #938);
#   `search/issues?q=label:<метка>` — часть ВЫРАЖЕНИЯ поиска: `%3A` внутри
#       значения движок НЕ декодирует, `label:area%3Aprocess` → 0 совпадений
#       против 343 у сырого двоеточия и кавычек. Сырое двоеточие квалификатор
#       НЕ режет — «обрезка по первому двоеточию» была первой редакцией этого
#       же объяснения и опровергнута тем же замером; кавычки в
#       label_search_value выбраны как однозначная форма, а не как лечение
#       обрезки.
# Пока гвардия принимала «любой из двух» на обеих поверхностях, форма
# `q=label:{label_query_value(x)}` — та самая, молча пустая — проходила её
# зелёной, и старая проба этого файла её же канонизировала.
_ENCODER_BY_SUFFIX = {
    "&labels=": "label_query_value",
    "?labels=": "label_query_value",
    "label:": "label_search_value",
}


def _required_encoder(constant: str) -> str | None:
    """Какой кодировщик обязан стоять сразу за этой константой — по её
    суффиксу. None — константа не является позицией значения метки."""
    for suffix, encoder in _ENCODER_BY_SUFFIX.items():
        if constant.endswith(suffix):
            return encoder
    return None


def _is_encoder_call(expr: ast.expr, expected: str) -> bool:
    """True — выражение внутри `{...}` это вызов ИМЕННО того кодировщика,
    которого требует поверхность, голый или через атрибут."""
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    if isinstance(func, ast.Attribute):
        return func.attr == expected
    if isinstance(func, ast.Name):
        return func.id == expected
    return False


def _unencoded_labels_offenders(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        values = node.values
        for i, part in enumerate(values):
            if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                continue
            expected = _required_encoder(part.value)
            if expected is None:
                continue
            # 'labels=' / 'label:' — последний кусок f-строки целиком
            # (константа без подстановки следом, например голый литерал
            # 'labels=task') — кодировать нечего.
            if i + 1 >= len(values):
                continue
            nxt = values[i + 1]
            if not isinstance(nxt, ast.FormattedValue):
                continue
            if not _is_encoder_call(nxt.value, expected):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{part.lineno} "
                                 f"(нужен {expected})")
    return offenders


def test_every_labels_query_substitution_is_url_encoded():
    offenders: list[str] = []
    for path in _production_scripts():
        offenders.extend(_unencoded_labels_offenders(path))
    assert offenders == [], (
        "Подстановка значения метки в query 'labels=' без "
        "review_labels.label_query_value(...) (класс #938 — двоеточие в "
        "имени метки типа 'waiting:owner'/'review:ok'/'ai:failed' ломает "
        "серверный фильтр GitHub молча, пустым списком, не ошибкой): "
        f"{offenders}"
    )


# ── Признак 'label:' (q=label:...) — доказательство на изолированной пробе ───
#
# Живого места с этой формой запроса сегодня в дереве нет (только 'labels='
# в open_waiting_owner_issues/open_task_issues), поэтому саму гвардию нельзя
# доказать прогоном по scripts/ — она просто ничего не найдёт. Проба ниже
# кормит _unencoded_labels_offenders изолированным файлом, а не дереву
# scripts/, ровно затем, чтобы доказать признак «label:» независимо от того,
# появится ли когда-нибудь живой q=label:-вызов (находка ревью PR #941:
# докстринг обещал покрытие 'q=label:...', которого признак не ловил).


def test_unencoded_offenders_flags_q_label_colon_form():
    # Файл обязан жить ВНУТРИ REPO_ROOT — _unencoded_labels_offenders строит
    # offender-строку через path.relative_to(REPO_ROOT), pytest'овский
    # tmp_path снаружи репозитория уронил бы это ValueError'ом, а не честным
    # результатом. Убирается в finally, что бы ни случилось в теле теста.
    probe = REPO_ROOT / "scripts" / "lib" / "_probe_q_label_colon_unencoded.py"
    probe.write_text(
        'def f(repo, label):\n'
        '    return f"repos/{repo}/search/issues?q=label:{label}"\n',
        encoding="utf-8",
    )
    try:
        offenders = _unencoded_labels_offenders(probe)
    finally:
        probe.unlink()
    # Сообщение несёт и адрес, и НУЖНЫЙ кодировщик: без второго автор
    # правит наугад — на этой поверхности их два, и они не заменяют друг друга.
    assert len(offenders) == 1, offenders
    assert offenders[0].startswith(f"{probe.relative_to(REPO_ROOT)}:2")
    assert "label_search_value" in offenders[0], offenders


def test_percent_encoder_on_the_search_surface_is_an_offender():
    """Перевёрнутая проба (находка ai-review PR #1435). Раньше этот тест
    зеленил форму `q=label:{label_query_value(x)}` — ту самую, которую замер
    2026-09-22 объявил молча пустой (0 совпадений против 343). Зелёный тест
    канонизировал дефект: первый же скопированный отсюда вызов ушёл бы в
    прод с пустым списком и зелёным CI."""
    probe = REPO_ROOT / "scripts" / "lib" / "_probe_q_label_colon_encoded.py"
    probe.write_text(
        'from review_labels import label_query_value\n'
        'def f(repo, label):\n'
        '    return f"repos/{repo}/search/issues?q=label:{label_query_value(label)}"\n',
        encoding="utf-8",
    )
    try:
        offenders = _unencoded_labels_offenders(probe)
    finally:
        probe.unlink()
    assert offenders and "label_search_value" in offenders[0], offenders


def test_search_encoder_on_the_query_parameter_surface_is_an_offender():
    """Симметрия, без которой привязка половинчатая: кавычки поиска в
    `labels=` дали бы `%22` в значении и тот же молчаливый пустой список."""
    probe = REPO_ROOT / "scripts" / "lib" / "_probe_labels_param_search_encoded.py"
    probe.write_text(
        'from review_labels import label_search_value\n'
        'def f(repo, label):\n'
        '    return f"repos/{repo}/issues?labels={label_search_value(label)}"\n',
        encoding="utf-8",
    )
    try:
        offenders = _unencoded_labels_offenders(probe)
    finally:
        probe.unlink()
    assert offenders and "label_query_value" in offenders[0], offenders


# ── Поведенческий тест на прод-форме значения ────────────────────────────────


def _load_review_labels():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "review_labels_encoding_check", REPO_ROOT / "scripts" / "lib" / "review_labels.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _load_waiting_owner_guard():
    """Тот же importlib-приём, каким test_waiting_owner_guard.py уже грузит
    модуль (доказанно дёшево там) — `scripts/orchestra` идёт в sys.path
    первым, потому что waiting_owner_guard.py делает голый `import
    pulse_guard`, рассчитывая на путь по каталогу, а не на пакет."""
    import importlib.util
    import sys

    orchestra_dir = REPO_ROOT / "scripts" / "orchestra"
    if str(orchestra_dir) not in sys.path:
        sys.path.insert(0, str(orchestra_dir))
    spec = importlib.util.spec_from_file_location(
        "waiting_owner_guard_encoding_check", orchestra_dir / "waiting_owner_guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_label_query_value_encodes_colon_like_prod_waiting_owner_label():
    review_labels = _load_review_labels()

    # Прод-форма — WAITING_OWNER_LABEL, прочитанная из живого исходника
    # waiting_owner_guard.py, не вписанная копия (AGENTS.md, «тест кормит
    # прод-форму данных», находка ревью PR #941: переименование метки не
    # должно молча оставить тест проверяющим мёртвое значение).
    wog = _load_waiting_owner_guard()
    assert review_labels.label_query_value(wog.WAITING_OWNER_LABEL) == "waiting%3Aowner"
    # Живые метки этого репозитория с тем же символом класса — та же
    # кодировка, не частный случай одной метки.
    assert review_labels.label_query_value("review:large-ok") == "review%3Alarge-ok"
    assert review_labels.label_query_value("ai:changes-requested") == "ai%3Achanges-requested"


# Мутация, которой доказана гвардия (#938): временно верни в
# waiting_owner_guard.py::open_waiting_owner_issues старую форму
# `f"...&labels={WAITING_OWNER_LABEL}&per_page=100"` (без
# review_labels.label_query_value) — test_every_labels_query_substitution_is_url_encoded
# краснеет, называя это же место offender'ом. Верни фикс — тест снова зелёный.


def test_search_qualifier_accepts_the_search_encoder():
    """Вторая законная форма (#1432): в выражении поиска метку кавычит
    `label_search_value`, а не кодирует процентами. Проба живёт ВНУТРИ
    REPO_ROOT по той же причине, что у соседних проб выше."""
    probe = REPO_ROOT / "scripts" / "lib" / "_probe_q_label_search_encoded.py"
    probe.write_text(
        'from review_labels import label_search_value\n'
        'def f(repo, label):\n'
        '    return f"search/issues?q=repo:{repo} label:{label_search_value(label)}"\n',
        encoding="utf-8")
    try:
        offenders = _unencoded_labels_offenders(probe)
    finally:
        probe.unlink()
    assert offenders == []


def test_search_qualifier_without_any_encoder_still_reddens():
    """Обратная сторона, и она важнее: расширение списка кодировщиков не
    должно открыть дверь голой подстановке — иначе гвардия «расширилась» до
    бесполезности."""
    probe = REPO_ROOT / "scripts" / "lib" / "_probe_q_label_search_bare.py"
    probe.write_text(
        'def f(repo, label):\n'
        '    return f"search/issues?q=repo:{repo} label:{label}"\n',
        encoding="utf-8")
    try:
        offenders = _unencoded_labels_offenders(probe)
    finally:
        probe.unlink()
    # Сообщение несёт и адрес, и НУЖНЫЙ кодировщик: без второго автор
    # правит наугад — на этой поверхности их два, и они не заменяют друг друга.
    assert len(offenders) == 1, offenders
    assert offenders[0].startswith(f"{probe.relative_to(REPO_ROOT)}:2")
    assert "label_search_value" in offenders[0], offenders
