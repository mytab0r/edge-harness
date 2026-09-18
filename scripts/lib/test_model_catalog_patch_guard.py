#!/usr/bin/env python3
"""Гвардия шага «Патч каталога моделей под реального провайдера»
(`.github/workflows/deploy-dsh-edge.yml`, задача #1294).

ЖИВОЙ класс отказа: якорь патча был привязан к ПОРЯДКУ элементов апстримного
`DEFAULT_MODELS` (`[{id:"deepseek-v4-flash"` — то есть «маркерная запись стоит
первой»). Апстрим `@deepseek-ai/dsh-llm-deepseek@0.1.5-rc.2` (входит в
`dsh-edge-v0.15.0`) поставил первым элементом новую модель `deepseek-flash`
(V41) — якорь перестал встречаться вовсе, шаг падал, деплой стоял, и морда
отвечала `0.14.1` при пине `0.15.0`. Порядок элементов в ЧУЖОМ массиве нам не
подконтролен и контрактом быть не может; наш контракт — НАЛИЧИЕ маркерной
записи и ровно одно её вхождение.

Тест кормится ПРОД-ФОРМОЙ, а не пересказом (AGENTS.md, «тест кормит прод-форму
данных»). Фикстура ниже получена воспроизводимо:

    npm pack @deepseek-ai/dsh-llm-deepseek@0.1.5-rc.2
    # массив DEFAULT_MODELS взят ДОСЛОВНО из package/lib/index.js,
    # константы (DEFAULT_CONTEXT_WINDOW и т.п.) подставлены значениями
    npx esbuild@0.25.0 models-src.mjs --bundle --minify --format=esm

То есть это настоящий текст настоящего апстрима, пропущенный через настоящий
минификатор, а не наше представление о том, как выглядит минифицированный
бандл. Регекс и маркер тест читает ИЗ САМОГО workflow (не копирует к себе) —
иначе гвардия проверяла бы собственную копию, а не то, что исполнит деплой.

Запуск: python -m pytest scripts/lib/test_model_catalog_patch_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import re

import pytest

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "deploy-dsh-edge.yml"

# Прод-форма (см. докстринг): дословный DEFAULT_MODELS апстрима 0.1.5-rc.2
# после esbuild --minify. Первым элементом стоит deepseek-flash — ровно то,
# на чём сломался прежний якорь.
UPSTREAM_MINIFIED_CATALOG = (
    '[{id:"deepseek-flash",name:"DeepSeek-V41-Flash",contextWindow:131072,inputModalities:["text","image"],imagePixelBudget:1e6,imageMaxBytes:5e6,systemPromptUpdate:"in-history"},{id:"deepseek-v4-flash",name:"DeepSeek-V4-Flash",description:"Fast, efficient, and economical; suited to focused, routine, or parallel tasks.",contextWindow:131072},{id:"deepseek-v4-pro",name:"DeepSeek-V4-Pro",description:"Stronger agentic coding, knowledge, and difficult reasoning; suited to complex or quality-critical tasks at higher cost.",contextWindow:131072},{id:"deepseek-v4-flash-vision-exp",name:"DeepSeek-V4-Flash-Vision-Exp",contextWindow:131072,inputModalities:["text","image"],imagePixelBudget:1e6,imageMaxBytes:5e6}]'
)

# Прежний якорь — хранится здесь НЕ как рабочий код, а как носитель класса:
# тест обязан доказывать, что на живой прод-форме он не находит ничего.
ANCHORED_TO_FIRST_ELEMENT = r'\[\{id:"deepseek-v4-flash"(?:[^\[\]]|\[[^\[\]]*\])*\]'

MODEL_IDS_IN_FIXTURE = (
    "deepseek-flash",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-v4-flash-vision-exp",
)


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _catalog_pattern() -> re.Pattern:
    """Регекс каталога — из самого workflow, не копия."""
    text = _workflow_text()
    match = re.search(r"pattern = re\.compile\(r'(.+?)'\)\n", text)
    assert match, ("в deploy-dsh-edge.yml не найден `pattern = re.compile(r'...')` шага "
                   "патча каталога — гвардии нечего проверять")
    return re.compile(match.group(1))


def _marker() -> str:
    text = _workflow_text()
    match = re.search(r"""marker = '(\{id:"[^"]+"\})?'""", text) or re.search(
        r"""marker = '(.+?)'\n""", text)
    assert match, "в deploy-dsh-edge.yml не найден `marker = '...'` — проверка уникальности снята"
    return match.group(1)


def test_previous_anchor_really_fails_on_live_upstream_form():
    """Не «мы так думаем», а факт: прежний якорь на живой прод-форме даёт ноль.

    Этот тест — носитель ПРИЧИНЫ, по которой якорь менялся. Если он когда-нибудь
    станет зелёным «сам по себе», значит фикстура перестала быть прод-формой
    (апстрим вернул прежний порядок) — и тогда её надо обновить, а не тихо
    радоваться."""
    assert re.findall(ANCHORED_TO_FIRST_ELEMENT, UPSTREAM_MINIFIED_CATALOG) == []


def test_catalog_pattern_matches_whole_array_on_live_upstream_form():
    pattern = _catalog_pattern()
    found = pattern.findall(UPSTREAM_MINIFIED_CATALOG)
    assert len(found) == 1, f"ожидалось ровно одно совпадение, получено {len(found)}"

    captured = pattern.search(UPSTREAM_MINIFIED_CATALOG).group(0)
    # Захват обязан быть скобочно сбалансированным: иначе замена порвёт бандл
    # на вложенном inputModalities:["text","image"].
    assert captured.count("[") == captured.count("]")
    assert captured.count("{") == captured.count("}")
    # И обязан накрыть ВЕСЬ массив, а не только маркерную запись: шаг заменяет
    # массив целиком на каталог из vars.
    assert captured.startswith("[") and captured.endswith("]")
    for model_id in MODEL_IDS_IN_FIXTURE:
        assert f'id:"{model_id}"' in captured, model_id


def test_marker_is_unique_in_live_upstream_form():
    """`subn(count=1)` молча берёт первый матч, поэтому шаг обязан сначала
    убедиться, что маркер один. На живой форме он один — но проверка нужна не
    ради сегодняшнего бандла, а ради завтрашнего."""
    assert UPSTREAM_MINIFIED_CATALOG.count(_marker()) == 1
    # Префиксная ловушка: deepseek-v4-flash-vision-exp начинается с того же id.
    # Закрывающая кавычка в маркере — единственное, что их различает.
    assert 'id:"deepseek-v4-flash-vision-exp"' in UPSTREAM_MINIFIED_CATALOG


def test_step_refuses_loudly_when_marker_is_not_unique():
    """Гейт уникальности должен СУЩЕСТВОВАТЬ в шаге и падать, а не «брать
    первый». Проверяется поведением: воспроизводим ту же проверку на входе с
    двумя вхождениями маркера."""
    text = _workflow_text()
    assert "markers = src.count(marker)" in text, "проверка уникальности маркера снята из шага"
    assert "if markers != 1:" in text, "проверка уникальности маркера не падает"
    # Окно — от гейта до самой замены, а не «первые N символов»: между ними
    # живёт объяснение класса, и привязка к длине комментария сделала бы
    # гвардию хрупкой к правке прозы.
    after_gate = text.split("if markers != 1:")[1]
    body = after_gate.split("pattern = re.compile")[0]
    assert "sys.exit(" in body, (
        "проверка уникальности не завершает шаг ошибкой — silent-wrong вернулся")
    assert "{markers}" in body, (
        "сообщение отказа не называет фактическое число вхождений (AGENTS.md: алерт не гадает)")

    doubled = UPSTREAM_MINIFIED_CATALOG + "," + UPSTREAM_MINIFIED_CATALOG
    assert doubled.count(_marker()) == 2  # вход, на котором шаг обязан упасть


def test_pattern_does_not_swallow_the_rest_of_the_bundle():
    """Регекс жадный по конструкции — проверяем, что он не уезжает за пределы
    массива, когда за ним в бандле идёт ещё код со скобками."""
    pattern = _catalog_pattern()
    bundle = "var e=" + UPSTREAM_MINIFIED_CATALOG + ';var t=["text","image"];var n=[1,2,3];'
    captured = pattern.search(bundle).group(0)
    assert captured == UPSTREAM_MINIFIED_CATALOG


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
