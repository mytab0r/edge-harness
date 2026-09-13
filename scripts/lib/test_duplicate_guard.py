"""Тесты чистой логики scripts/lib/duplicate_guard.py (#566).

Фикстуры — ДОСЛОВНЫЕ заголовки живых issue репозитория (не пересказ, не
выдуманные примеры — правило AGENTS.md «тест кормит прод-форму данных»):
#562/#564 — измеренный настоящий дубль (заведены с разницей 20 минут, один
и тот же отпечаток `deploy-dsh-edge: автооткат (#549) ...`); #518/#548 —
измеренный настоящий случай, который эта гвардия ЧЕСТНО не ловит (разные
слова описывают тот же корень — предел инструмента, задокументирован в
модуле).

Мутация, которой доказана проверка: занизь DEFAULT_THRESHOLD до значения
выше 0.53 (например 0.9) в scripts/lib/duplicate_guard.py — красится
test_real_incident_pair_is_flagged (реальный дубль #562/#564 перестаёт
находиться). Верни порог 0.3 — тест снова зелёный (см. также мутацию
на уровне обёртки в scripts/gh/test/issue-create-duplicate-guard.test.sh).

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
