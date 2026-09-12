"""Тесты чистой логики scripts/lib/duplicate_guard.py (#566/#570).

Фикстуры — ДОСЛОВНЫЕ заголовки/тела живых issue репозитория (не пересказ, не
выдуманные примеры — правило AGENTS.md «тест кормит прод-форму данных»):
#562/#564 — измеренный настоящий дубль (заведены с разницей 20 минут, один
и тот же отпечаток `deploy-dsh-edge: автооткат (#549) ...`); #518/#548 —
измеренный настоящий случай, который ПЕРВЫЙ слой (заголовок) ЧЕСТНО не
ловит (разные слова описывают тот же корень), а ВТОРОЙ слой (улики в теле,
#570) ловит через ссылки #505/#513 + прямую цитату «#518» в теле #548. Тела
целиком — в scripts/lib/fixtures_issue_<N>_body.md (`gh issue view --json
body -q .body`), не инлайн: тела многострочные (~3-4К), инлайн раздул бы
файл теста без выгоды для читаемости.

Мутация, которой доказана проверка ПЕРВОГО слоя: занизь DEFAULT_THRESHOLD до
значения выше 0.53 (например 0.9) в scripts/lib/duplicate_guard.py —
красится test_real_incident_pair_is_flagged (реальный дубль #562/#564
перестаёт находиться). Верни порог 0.3 — тест снова зелёный (см. также
мутацию на уровне обёртки в
scripts/gh/test/issue-create-duplicate-guard.test.sh).

Мутация, которой доказана проверка ВТОРОГО слоя (улики): в
find_evidence_matches() удали ветку `if self_cite and (shared_paths or
shared_refs)` целиком — test_evidence_catches_518_548_via_refs_and_self_cite
краснеет (пара перестаёт ловиться, единственная опора которой — self-cite+
ref, без quote/run). Верни ветку — тест снова зелёный. Аналогично для quote:
занизь EVIDENCE_QUOTE_JACCARD_THRESHOLD до значения выше 0.5 (например 0.9)
— test_evidence_catches_562_564_via_quote краснеет.

Запуск: python -m pytest scripts/lib/test_duplicate_guard.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "duplicate_guard", Path(__file__).resolve().with_name("duplicate_guard.py"))
duplicate_guard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(duplicate_guard)  # type: ignore[union-attr]

_FIXTURES_DIR = Path(__file__).resolve().parent


def _fixture_body(issue_number: int) -> str:
    return (_FIXTURES_DIR / f"fixtures_issue_{issue_number}_body.md").read_text(encoding="utf-8")


# ── Заголовки живых issue (дословно, gh issue view --json title) ────────────

TITLE_562 = (
    "deploy-dsh-edge: автооткат (#549) оставляет рассинхрон версий — "
    "следующий wrangler secret put падает"
)
TITLE_564 = (
    "deploy-dsh-edge: автооткат (#549) блокирует следующий деплой — "
    "wrangler secret put падает VERSION_NOT_DEPLOYED"
)
TITLE_518 = (
    "deploy-dsh-edge.yml красный после бампа 0.11.1: plugin-manager/integrations "
    "ссылаются на убранный апстримом dsh-client-runtime"
)
TITLE_548 = (
    "Шелл dsh-edge пуст после бампа 0.11.1 — коллизия локали settings.plugins "
    "с апстримным dsh-client-ui-settings-plugins"
)
TITLE_UNRELATED = "Атомарная аренда задачи (task lease): единый claim для всех пайплайнов"

# ── Тела живых issue (дословно, см. докстринг модуля) ────────────────────────

BODY_562 = _fixture_body(562)
BODY_564 = _fixture_body(564)
BODY_518 = _fixture_body(518)
BODY_548 = _fixture_body(548)
BODY_UNRELATED = _fixture_body(121)  # #121 — «Атомарная аренда задачи», см. TITLE_UNRELATED


def test_tokenize_drops_stopwords_and_short_tokens():
    tokens = duplicate_guard.tokenize("Это не дефект, а фича — см. #1")
    assert "это" not in tokens
    assert "не" not in tokens
    assert "а" not in tokens
    assert "1" not in tokens  # короче 3 символов
    assert "дефект" in tokens
    assert "фича" in tokens


def test_jaccard_empty_sets_is_zero():
    assert duplicate_guard.jaccard(set(), {"a"}) == 0.0
    assert duplicate_guard.jaccard(set(), set()) == 0.0


def test_real_incident_pair_is_flagged():
    """#562 vs #564 — измеренный настоящий дубль (см. модульный докстринг)."""
    score = duplicate_guard.jaccard(
        duplicate_guard.tokenize(TITLE_562), duplicate_guard.tokenize(TITLE_564))
    assert score >= duplicate_guard.DEFAULT_THRESHOLD, (
        f"реальный дубль #562/#564 обязан ловиться порогом по умолчанию, score={score:.2f}")


def test_honest_ceiling_different_vocabulary_not_flagged():
    """#518 vs #548 — тот же корень, разные слова: инструмент честно не ловит."""
    score = duplicate_guard.jaccard(
        duplicate_guard.tokenize(TITLE_518), duplicate_guard.tokenize(TITLE_548))
    assert score < duplicate_guard.DEFAULT_THRESHOLD, (
        "предел инструмента задокументирован в модуле — если это начало ловиться, "
        "докстринг обязан обновиться вместе с порогом")


def test_find_similar_open_tasks_returns_sorted_matches_above_threshold():
    candidates = [
        {"number": 562, "title": TITLE_562, "url": "https://example/562"},
        {"number": 1, "title": TITLE_UNRELATED, "url": "https://example/1"},
    ]
    matches = duplicate_guard.find_similar_open_tasks(TITLE_564, candidates)
    assert [m["number"] for m in matches] == [562]
    assert matches[0]["score"] >= duplicate_guard.DEFAULT_THRESHOLD


def test_find_similar_open_tasks_empty_when_no_match():
    candidates = [{"number": 1, "title": TITLE_UNRELATED, "url": "https://example/1"}]
    assert duplicate_guard.find_similar_open_tasks(TITLE_564, candidates) == []


def test_find_similar_open_tasks_empty_candidates_is_empty():
    assert duplicate_guard.find_similar_open_tasks(TITLE_564, []) == []


# ── Второй слой (#570): улики в теле — см. модульный докстринг ───────────────


def test_extract_evidence_finds_paths_refs_runs_and_quote_tokens():
    body = (
        "см. #505 и прогон https://github.com/o/r/actions/runs/34041272592\n"
        "файл plugins-src/plugin-manager/package.json\n"
        "```\nError: something broke badly here\n```"
    )
    evidence = duplicate_guard.extract_evidence(body)
    assert "505" in evidence["refs"]
    assert "34041272592" in evidence["runs"]
    assert "plugins-src/plugin-manager/package.json" in evidence["paths"]
    assert "broke" in evidence["quote_tokens"]
    assert "something" in evidence["quote_tokens"]


def test_evidence_catches_562_564_via_quote():
    """#564 vs #562 — дословная цитата ошибки wrangler в блоке ```, разный
    перенос строк (см. модульный докстринг: score 0.50 на реальных телах)."""
    candidates = [{"number": 562, "title": TITLE_562, "url": "https://example/562", "body": BODY_562}]
    matches = duplicate_guard.find_evidence_matches(BODY_564, candidates)
    assert [m["number"] for m in matches] == [562]
    assert "цитата" in matches[0]["reason"]


def test_evidence_catches_518_548_via_refs_and_self_cite():
    """#548 vs #518 — ЧЕСТНЫЙ ПРЕДЕЛ первого слоя (заголовок, score 0.18),
    второй слой ловит: тело #548 дословно содержит «#518» И оба ссылаются на
    #505/#513 (бамп 0.11.1) — ни то, ни другое поодиночке не считается
    уликой (см. модульный докстринг), только их пересечение."""
    candidates = [{"number": 518, "title": TITLE_518, "url": "https://example/518", "body": BODY_518}]
    matches = duplicate_guard.find_evidence_matches(BODY_548, candidates)
    assert [m["number"] for m in matches] == [518]
    assert "518" in matches[0]["reason"]
    assert "505" in matches[0]["reason"] or "513" in matches[0]["reason"]


def test_evidence_lone_ref_mention_is_not_evidence():
    """Голое упоминание номера БЕЗ общего пути/ссылки — не улика (измерено:
    102/138 открытых задач репозитория дали бы совпадение, если бы голого
    self-cite было достаточно — см. модульный докстринг)."""
    body_mentions_only = "Смотри также #518 для контекста, не связано иначе."
    candidates = [{"number": 518, "title": TITLE_518, "url": "https://example/518", "body": BODY_518}]
    assert duplicate_guard.find_evidence_matches(body_mentions_only, candidates) == []


def test_evidence_unrelated_body_has_no_matches():
    """#121 (реальная, но топически не связанная задача) не матчится ни с
    #518, ни с #562 — отрицательный контроль на реальных телах."""
    candidates = [
        {"number": 518, "title": TITLE_518, "url": "https://example/518", "body": BODY_518},
        {"number": 562, "title": TITLE_562, "url": "https://example/562", "body": BODY_562},
    ]
    assert duplicate_guard.find_evidence_matches(BODY_UNRELATED, candidates) == []


def test_evidence_common_path_filtered_on_large_pool():
    """Путь, общий для большой доли пула (например обязательная строка
    чеклиста), не считается уликой — частотный фильтр включается только при
    EVIDENCE_COMMON_MIN_POOL+ кандидатах (иначе на маленьких фикстурах он бы
    выбрасывал настоящую улику просто из-за размера выборки)."""
    common_path = "docs/research/30-rejected-alternatives.md"
    boilerplate = f"- [x] Я прочитал {common_path} и задача не из отвергнутых"
    new_body = f"# Совсем другая задача\n{boilerplate}"
    candidates = [
        {"number": n, "title": f"Задача {n}", "url": f"https://example/{n}",
         "body": f"# Тема {n}\n{boilerplate}"}
        for n in range(1, 6)  # 5 кандидатов — выше EVIDENCE_COMMON_MIN_POOL
    ]
    assert duplicate_guard.find_evidence_matches(new_body, candidates) == []


def test_evidence_empty_candidates_is_empty():
    assert duplicate_guard.find_evidence_matches(BODY_564, []) == []


def test_evidence_excludes_candidate_matching_own_number():
    """`number=` — вызывающий передаёт номер СВОЕЙ задачи (если уже известен),
    чтобы не сравнивать issue саму с собой, если она попала в свой же пул."""
    candidates = [{"number": 564, "title": TITLE_564, "url": "https://example/564", "body": BODY_564}]
    assert duplicate_guard.find_evidence_matches(BODY_564, candidates, number=564) == []
