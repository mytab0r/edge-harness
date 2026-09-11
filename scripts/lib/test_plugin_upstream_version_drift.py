#!/usr/bin/env python3
"""Тесты гвардии класса #507 (scripts/lib/plugin_upstream_version_drift.py).

Мутация-доказательство (описана в теле PR): закомментировать тело `if
pinned != declared: problems.append(...)` в find_drift красит
test_real_repo_has_zero_drift_after_the_fix и test_find_drift_flags_mismatch
зелёными фиктивно (нет находок в принципе) — обратное: снять условие `if
pinned is None: continue` заставляет функцию считать «дрифтом» пакеты, о
которых апстрим вообще не знает, — test_find_drift_ignores_packages_absent_upstream
краснеет. Комбинация обоих тестов доказывает, что проверяется именно
пересечение (плагин объявил вопрос, апстрим на него ответил другой версией),
а не более широкое/узкое множество.

Запуск: python -m pytest scripts/lib/test_plugin_upstream_version_drift.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import importlib
import json
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
drift = importlib.import_module("plugin_upstream_version_drift")


def test_collect_plugin_deepseek_deps_scans_all_three_dependency_fields(tmp_path):
    plugin_dir = tmp_path / "sample"
    plugin_dir.mkdir()
    (plugin_dir / "package.json").write_text(json.dumps({
        "name": "@edge-harness/dsh-plugin-sample",
        "dependencies": {"@deepseek-ai/dsh-llm": "0.1.2-rc.1", "left-pad": "1.0.0"},
        "devDependencies": {"@deepseek-ai/dsh-client-locale": "0.1.2-rc.1"},
        "peerDependencies": {"@deepseek-ai/dsh-tools": "0.1.1-rc.2"},
    }), encoding="utf-8")

    found = drift.collect_plugin_deepseek_deps(tmp_path)
    assert ("sample", "@deepseek-ai/dsh-llm", "0.1.2-rc.1") in found
    assert ("sample", "@deepseek-ai/dsh-client-locale", "0.1.2-rc.1") in found
    assert ("sample", "@deepseek-ai/dsh-tools", "0.1.1-rc.2") in found
    assert not any(name == "left-pad" for _, name, _ in found)


def test_find_drift_flags_mismatch():
    plugin_deps = [("provider-registry", "@deepseek-ai/dsh-settings", "0.1.1-rc.2")]
    upstream_deps = {"@deepseek-ai/dsh-settings": "0.1.2-rc.1"}
    problems = drift.find_drift(plugin_deps, upstream_deps)
    assert len(problems) == 1
    assert "@deepseek-ai/dsh-settings@0.1.1-rc.2" in problems[0]
    assert "0.1.2-rc.1" in problems[0]


def test_find_drift_ignores_packages_absent_upstream():
    plugin_deps = [("hello-world", "@deepseek-ai/does-not-exist-upstream", "9.9.9")]
    assert drift.find_drift(plugin_deps, {}) == []


def test_find_drift_passes_when_versions_match():
    plugin_deps = [("provider-registry", "@deepseek-ai/dsh-llm", "0.1.2-rc.1")]
    upstream_deps = {"@deepseek-ai/dsh-llm": "0.1.2-rc.1"}
    assert drift.find_drift(plugin_deps, upstream_deps) == []


def test_load_upstream_pin_reads_repo_and_sha(tmp_path):
    upstream_path = tmp_path / "upstream.json"
    upstream_path.write_text(json.dumps({"repo": "pawaca/dsh-edge", "sha": "a" * 40}), encoding="utf-8")
    assert drift.load_upstream_pin(upstream_path) == ("pawaca/dsh-edge", "a" * 40)


def test_real_repo_has_zero_drift_after_the_fix():
    """Поведенческий регресс-тест на РЕАЛЬНОМ дереве plugins-src этого
    репозитория (прод-форма, не пересказ), против пина upstream.json,
    зафиксированного на момент разработки этой гвардии (issue #507/#806):
    после фикса разрыва не осталось ни для одного @deepseek-ai/* пакета,
    который апстрим объявляет на верхнем уровне. Список версий — снят живым
    запросом к raw.githubusercontent.com при разработке гвардии (см. PR),
    не сеть в pytest: живую сверку с ТЕКУЩИМ пином делает сам
    scripts/lib/plugin_upstream_version_drift.py при запуске гвардии в CI."""
    upstream_deps_snapshot = {
        "@deepseek-ai/dsh-anonymous-user-id": "0.1.2-rc.1",
        "@deepseek-ai/dsh-credentials": "0.1.2-rc.1",
        "@deepseek-ai/dsh-llm": "0.1.2-rc.1",
        "@deepseek-ai/dsh-llm-deepseek": "0.1.2-rc.1",
        "@deepseek-ai/dsh-settings": "0.1.2-rc.1",
        "@deepseek-ai/dsh-tools": "0.1.2-rc.1",
    }
    plugin_deps = drift.collect_plugin_deepseek_deps(drift.PLUGINS_SRC_DIR)
    assert plugin_deps, "plugins-src не даёт ни одной @deepseek-ai/* зависимости — карта устарела"
    problems = drift.find_drift(plugin_deps, upstream_deps_snapshot)
    assert problems == []
