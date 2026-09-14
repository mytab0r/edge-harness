#!/usr/bin/env python3
"""Тесты классификации классов дефектов в источнике (issue #1237).

Кормятся прод-формой там, где это возможно: блок «Живые тексты #1172»
использует ДОСЛОВНЫЕ абзацы реальных комментариев-вердиктов PR #1089
(08:13Z 2026-09-13), #1104 (09:44Z) и #1114 (10:43Z) — три подлинных
инстанса класса #1172 («механизм физически не может сработать»), не
пересказ. Строка `КЛАСС: недостижимый-механизм` в конце каждого текста —
ЧЕСТНО не результат живого вызова модели ai-review (сборка промпта в CI не
воспроизведена этим тестом), а ручная разметка по контракту ai_prompt.md,
сделанная автором PR #1237 — см. «Не подтверждено» в defect_classes.py.

Запуск: python -m pytest scripts/review/test_defect_classes.py -q
"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).with_name("defect_classes.py")
spec = importlib.util.spec_from_file_location("defect_classes", SCRIPT)
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)  # type: ignore[union-attr]


# ── Разбор поля КЛАСС — чистая функция ───────────────────────────────────────

def test_parse_defect_classes_finds_all_lines_in_order():
    text = (
        "1. Блокирует мерж: первая находка.\n"
        "КЛАСС: класс-один\n\n"
        "2. Блокирует мерж: вторая находка.\n"
        "КЛАСС: класс-два\n"
    )
    assert dc.parse_defect_classes(text) == ["класс-один", "класс-два"]


def test_parse_defect_classes_no_lines_returns_empty():
    assert dc.parse_defect_classes("Просто проза без единого поля.\nВЕРДИКТ: approve") == []


def test_parse_defect_classes_tolerates_trailing_period_and_spaces():
    assert dc.parse_defect_classes("КЛАСС:  недостижимый-механизм .  \n") == ["недостижимый-механизм"]


def test_is_valid_slug():
    assert dc.is_valid_slug("недостижимый-механизм")
    assert dc.is_valid_slug("two-word")
    assert not dc.is_valid_slug("однословный")  # без дефиса — не slug
    assert not dc.is_valid_slug("bug123")  # цифры/нет дефиса
    assert not dc.is_valid_slug("путь/файла")  # символ пути


# ── Три состояния (не два) — тот же принцип, что scripts/lib/check_result.py ──

def test_classify_not_named_when_answer_has_no_class_line():
    # Обратная совместимость (#1237 п.8): старый ответ без поля КЛАСС —
    # контракт используют параллельные PR — разбирается честным третьим
    # состоянием, не ошибкой и не молчаливым "known".
    signal = dc.classify("1. Блокирует мерж: находка без класса.\nВЕРДИКТ: rework",
                          known={"известный-класс"})
    assert signal.state == dc.STATE_NOT_NAMED
    assert signal.known == ()
    assert signal.candidates == ()


def test_classify_known_when_slug_in_registry():
    signal = dc.classify("Находка.\nКЛАСС: известный-класс\nВЕРДИКТ: rework",
                          known={"известный-класс"})
    assert signal.state == dc.STATE_KNOWN
    assert signal.known == ("известный-класс",)
    assert signal.candidates == ()


def test_classify_candidate_when_slug_not_in_registry():
    signal = dc.classify("Находка.\nКЛАСС: новый-невиданный-класс\nВЕРДИКТ: rework",
                          known={"известный-класс"})
    assert signal.state == dc.STATE_CANDIDATE
    assert signal.candidates == ("новый-невиданный-класс",)
    assert signal.known == ()


def test_classify_mixed_known_and_candidate_state_is_candidate():
    # Хотя бы один НЕизвестный slug — состояние "candidate" (модель нашла
    # что-то новое), даже если рядом есть известный: candidate не теряется в
    # молчаливом "known" среди прочих находок того же PR.
    text = "A.\nКЛАСС: известный-класс\n\nB.\nКЛАСС: новый-класс\nВЕРДИКТ: rework"
    signal = dc.classify(text, known={"известный-класс"})
    assert signal.state == dc.STATE_CANDIDATE
    assert signal.known == ("известный-класс",)
    assert signal.candidates == ("новый-класс",)


def test_classify_deduplicates_repeated_slug_preserving_order():
    text = "A.\nКЛАСС: класс-один\n\nB.\nКЛАСС: класс-один\n\nC.\nКЛАСС: класс-два\n"
    signal = dc.classify(text, known=set())
    assert signal.candidates == ("класс-один", "класс-два")


def test_classify_malformed_token_reported_as_invalid_not_named():
    signal = dc.classify("Находка.\nКЛАСС: bug123\nВЕРДИКТ: rework", known=set())
    assert signal.state == dc.STATE_NOT_NAMED
    assert signal.invalid == ("bug123",)
    assert signal.candidates == ()


# ── Реестр известных классов — читается из JSON, не из кода ──────────────────

def test_known_classes_seeded_with_1172():
    assert "недостижимый-механизм" in dc.known_slugs()
    entry = next(e for e in dc.KNOWN_CLASSES if e["slug"] == "недостижимый-механизм")
    assert entry["issue"] == 1172
    assert entry["summary"]


def test_registry_file_is_the_single_source_dc_reads_at_import():
    import json
    raw = json.loads(dc.REGISTRY_FILE.read_text(encoding="utf-8"))
    assert {e["slug"] for e in raw["known"]} == dc.known_slugs()


# ── Промпт: известные + кандидаты, потолок размера ───────────────────────────

def test_render_prompt_section_lists_known_and_candidates():
    section = dc.render_prompt_section([{"slug": "новый-кандидат", "count": 2, "prs": {1, 2}}])
    assert "недостижимый-механизм" in section
    assert "новый-кандидат (замечено 2x)" in section


def test_render_prompt_section_no_candidates_says_so_explicitly():
    section = dc.render_prompt_section([])
    assert "Кандидатов сейчас нет." in section


def test_render_prompt_section_caps_candidates_shown():
    # Потолок размера промпта — MAX_CANDIDATES_SHOWN (issue #1237, «Требования
    # к словарю: потолок размера»). recent_candidate_stats уже обрезает список
    # (проверено ниже отдельно) — render_prompt_section лишь рендерит то, что
    # получила, проверяем здесь, что вход, УЖЕ превышающий потолок, не
    # взрывает вывод (защита в глубину — обрезка не единственное место).
    huge = [{"slug": f"класс-{i}", "count": 1, "prs": {i}} for i in range(50)]
    section = dc.render_prompt_section(huge[: dc.MAX_CANDIDATES_SHOWN])
    assert section.count(" (замечено ") == dc.MAX_CANDIDATES_SHOWN


def test_render_prompt_section_token_cost_is_small():
    # Грубая оценка 4 символа/токен (тот же порядок, что использует остальной
    # промпт-бюджет репозитория) — потолок словаря (1 известный + 15
    # кандидатов) не должен доминировать над остальным промптом (context_pack/
    # rules_section — десятки КБ).
    candidates = [{"slug": f"кандидат-класса-{i}", "count": i + 1, "prs": set(range(i + 1))}
                  for i in range(dc.MAX_CANDIDATES_SHOWN)]
    section = dc.render_prompt_section(candidates)
    approx_tokens = len(section) / 4
    assert approx_tokens < 700, f"словарь на потолке стоит ~{approx_tokens:.0f} токенов"


def test_render_prompt_section_unavailable_names_reason_not_silent():
    section = dc.render_prompt_section_unavailable("gh api: HTTP 503")
    assert "HTTP 503" in section
    assert "не прочитаны" in section
    assert "недостижимый-механизм" in section  # известные классы всё равно видны


# ── Кандидаты: запись маркера и чтение среза (issue #1238) ───────────────────

def test_record_candidate_observation_posts_expected_marker():
    calls = []

    def fake_run_gh(*args):
        calls.append(args)

    dc.record_candidate_observation("o/r", fake_run_gh, "новый-класс", 4242)
    assert calls == [(
        "api", "-X", "POST", f"repos/o/r/issues/{dc.DEFECT_CLASS_TRACKER_ISSUE}/comments",
        "-f", "body=<!-- defect-class-candidate: slug=новый-класс pr=4242 -->",
    )]


def _fake_gh_comments(bodies):
    def fake_gh(url):
        assert str(dc.DEFECT_CLASS_TRACKER_ISSUE) in url
        return [{"body": body} for body in bodies]
    return fake_gh


def test_recent_candidate_stats_counts_distinct_prs_not_raw_markers():
    # Три маркера одного PR — один случай, не три (issue #1237 п. «порог
    # считает разные PR» — то же требование, что follow-up #1240 обязан
    # применить при повышении). Два маркера того же slug на РАЗНЫХ PR — два.
    bodies = [
        "<!-- defect-class-candidate: slug=новый-класс pr=1 -->",
        "<!-- defect-class-candidate: slug=новый-класс pr=1 -->",
        "<!-- defect-class-candidate: slug=новый-класс pr=2 -->",
    ]
    stats = dc.recent_candidate_stats("o/r", _fake_gh_comments(bodies))
    assert stats == [{"slug": "новый-класс", "count": 2, "prs": {1, 2}}]


def test_recent_candidate_stats_excludes_already_known_slugs():
    bodies = [
        "<!-- defect-class-candidate: slug=недостижимый-механизм pr=1 -->",
        "<!-- defect-class-candidate: slug=свежий-кандидат pr=2 -->",
    ]
    stats = dc.recent_candidate_stats("o/r", _fake_gh_comments(bodies))
    assert [s["slug"] for s in stats] == ["свежий-кандидат"]


def test_recent_candidate_stats_ignores_unrelated_comments():
    bodies = ["обычный комментарий человека, не маркер", ""]
    assert dc.recent_candidate_stats("o/r", _fake_gh_comments(bodies)) == []


def test_recent_candidate_stats_caps_and_ranks_by_distinct_pr_count():
    bodies = []
    for i in range(dc.MAX_CANDIDATES_SHOWN + 5):
        bodies.append(f"<!-- defect-class-candidate: slug=класс-{i} pr={i} -->")
    # класс-0 замечен в трёх разных PR — должен оказаться первым по ранжиру.
    bodies += [f"<!-- defect-class-candidate: slug=класс-0 pr={900 + j} -->" for j in range(2)]
    stats = dc.recent_candidate_stats("o/r", _fake_gh_comments(bodies))
    assert len(stats) == dc.MAX_CANDIDATES_SHOWN
    assert stats[0]["slug"] == "класс-0"
    assert stats[0]["count"] == 3


# ── Живые тексты #1172: три инстанса, доказательство схождения числом ───────
# Дословные абзацы находок п.1 «Блокирует мерж» из реальных комментариев
# ai-review (см. докстринг модуля выше — оговорка о ручной разметке).

_A1_PR1089 = (
    "**1. Блокирует мерж: тишина физически не может сработать раньше "
    "возраста — эффект PR не достигается.** `scripts/orchestra/scheduler.py:2802` "
    "пускает наблюдение тишины только когда прогону уже ≥ `WORKER_SILENCE_MINUTES` "
    "(150), а окно тишины меряется от ts маркера (scheduler.py:2629–2631), и эти "
    "маркеры пишет только сам `_worker_silence_reason` — другого писателя с этим "
    "префиксом в репозитории нет. Значит, база появляется не раньше 150-й минуты, "
    "и тишина может сработать не раньше чем на 150+150=300-й — а с 295-й уже "
    "вычислен возрастной `reason`, и тишинная ветка не вызывается вовсе. Улика: "
    "прогон живым кодом (пульсы каждые 15 мин, сессия молчит с 0-й минуты) "
    "разряжен только на 300-й минуте с причиной «возраст, порог 295 мин»; "
    "тишинная причина не встретилась ни разу; первый маркер код сам написал лишь "
    "на 150-й минуте.\n"
    "КЛАСС: недостижимый-механизм"
)

_A2_PR1104 = (
    "1. Блокирует: ветка `pulseStale` в `pulseAlertText` недостижима из "
    "единственного прод-вызова, и спека обещает владельцу текст, который не "
    "может уйти. `#dispatchOrchestraTick` зовёт `#tickPulseAlert(now, { ts: now, "
    "... })` (harness.ts:1377), а `pulseStale` требует `now - ts >= "
    "2×selfOrchestrationMs` (harness.ts:526) — при `ts = now` она ложна всегда. "
    "Значит ветка на harness.ts:599 мертва, `minutes` в фактическом тексте всегда "
    "0 («(0 мин назад)»), и сценарий «alarm подвис, страховка дозвонилась и "
    "упала» уводит обычный текст с detail текущей попытки, а не «тик не "
    "обновлялся N мин».\n"
    "КЛАСС: недостижимый-механизм"
)

_A3_PR1114 = (
    "**2. `INVALID_API_KEY` объявлен «прод-формой» без единого подтверждения — "
    "и на нём теперь держится весь стоп-класс.** Каждый префикс в этой функции "
    "несёт живой прогон: HTTP_404 — 33572445063, RATE_LIMIT — 34176910458, "
    "INVALID_REQUEST — 34730173870, STREAM_CLOSED — 34727164425/09:39Z. У "
    "`INVALID_API_KEY` прогона нет: по репозиторию он живёт только в "
    "синтетической заглушке смоука с самого #727 (там он был просто "
    "представителем «настоящей ошибки», никто не заявлял его прод-формой), ни в "
    "docs/research, ни в логах он не зафиксирован. До этого PR битый ключ "
    "останавливал цепочку общим стоп-классом с любым текстом; теперь стоп "
    "срабатывает только на этой конкретной строке — если реальный dsh на 401 "
    "печатает что-то другое, единственный именованный стоп-класс в проде не "
    "выстрелит никогда.\n"
    "КЛАСС: недостижимый-механизм"
)


def test_three_real_pr1172_findings_each_classify_as_candidate_before_promotion():
    # known=set() — состояние словаря ДО этого PR (класс #1172 ещё не
    # утверждён) — воспроизводит исторический момент 2026-09-13.
    for text in (_A1_PR1089, _A2_PR1104, _A3_PR1114):
        signal = dc.classify(text, known=set())
        assert signal.state == dc.STATE_CANDIDATE
        assert signal.candidates == ("недостижимый-механизм",)


def test_three_real_pr1172_findings_collapse_to_one_slug_by_third_pr():
    """Критерий владельца: механизм обязан поймать класс на 3-4-м, не на
    11-м инстансе. Симуляция: после A1(#1089) и A2(#1104) кандидат уже
    зафиксирован issue #1238 (2 разных PR); A3(#1114) — ТРЕТИЙ PR того же
    slug — пересекает порог `DIGEST_REPEAT_THRESHOLD`-стиля 3 (follow-up
    #1240 обязан завести задачу здесь, не на #1089/#1104/#1114 + восемь
    последующих, как было исторически с #1172)."""
    for pr_number, text in ((1089, _A1_PR1089), (1104, _A2_PR1104)):
        signal = dc.classify(text, known=set())
        assert signal.candidates == ("недостижимый-механизм",)

    marker_bodies_after_two = [
        f"<!-- defect-class-candidate: slug=недостижимый-механизм pr={pr} -->"
        for pr in (1089, 1104)
    ]
    stats_after_two = dc.recent_candidate_stats(
        "o/r", _fake_gh_comments(marker_bodies_after_two), known=set())
    assert stats_after_two == [{"slug": "недостижимый-механизм", "count": 2, "prs": {1089, 1104}}]

    signal_third = dc.classify(_A3_PR1114, known=set())
    assert signal_third.candidates == ("недостижимый-механизм",)

    marker_bodies_after_three = marker_bodies_after_two + [
        "<!-- defect-class-candidate: slug=недостижимый-механизм pr=1114 -->"
    ]
    stats_after_three = dc.recent_candidate_stats(
        "o/r", _fake_gh_comments(marker_bodies_after_three), known=set())
    assert stats_after_three == [
        {"slug": "недостижимый-механизм", "count": 3, "prs": {1089, 1104, 1114}}
    ]
    # Три РАЗНЫХ PR, один slug — сходимость на третьем инстансе, не на
    # одиннадцатом (живая история #1172).
    assert stats_after_three[0]["count"] == 3
