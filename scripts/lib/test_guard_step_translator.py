#!/usr/bin/env python3
"""Тесты транслятора рукописного шага гвардии в каталог (issue #897,
продолжение #749/#771/#762/#764).

Прод-форма: фикстуры `.github/workflows/repo-ci.yml` ниже — ВЕРБАТИМ куски
реальных диффов измеренных PR (#870/#841/#241 на дату issue #897), не
пересказ формата (AGENTS.md, «тест кормит прод-форму данных, а не
пересказ») — `test_translate_repo_ci_migrates_multiline_pytest_step`
воспроизводит ровно PR #870 (шаг «Тесты механического потолка закрытий
pm-прогона»), `test_translate_repo_ci_raises_when_no_extractable_target`
воспроизводит ровно PR #241 (шаг «Тесты DO журнала», `npm ci`/`npx vitest
run` — ни один паттерн `_extract_run_targets` их не ловит).

Мутация, доказывающая класс: убери проверку `extra_keys` в
translate_repo_ci — test_translate_repo_ci_raises_on_unsupported_env_key
покраснеет (шаг с `env:` тихо мигрирует, теряя переменную окружения вместо
громкого отказа). Убери проверку `targets` — покраснеет
test_translate_repo_ci_raises_when_no_extractable_target тем же способом.

Запуск: python -m pytest scripts/lib/test_guard_step_translator.py -q
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("guard_step_translator", _DIR / "guard_step_translator.py")
gst = importlib.util.module_from_spec(_spec)
# Регистрация в sys.modules ДО exec_module (issue #897, живая находка): сам
# guard_step_translator.py несёт `@dataclass` на классах с отложенными
# аннотациями (`from __future__ import annotations`) — dataclass на Python
# 3.11 разрешает их через sys.modules[cls.__module__], незарегистрированный
# модуль даёт `AttributeError: 'NoneType' object has no attribute '__dict__'`
# при самом импорте, до единого теста.
sys.modules["guard_step_translator"] = gst
_spec.loader.exec_module(gst)  # type: ignore[union-attr]


def _write(tmp_path: Path, repo_ci_text: str) -> tuple[Path, Path, Path]:
    """Возвращает (repo_root, repo_ci_path, catalog_dir) — каталог гвардий
    заводится пустым (нет коллизий по умолчанию)."""
    repo_root = tmp_path
    repo_ci = repo_root / ".github" / "workflows" / "repo-ci.yml"
    repo_ci.parent.mkdir(parents=True, exist_ok=True)
    repo_ci.write_text(repo_ci_text, encoding="utf-8")
    catalog_dir = repo_root / "scripts" / "ci" / "guards"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    return repo_root, repo_ci, catalog_dir


# ── Пусто: allowlist покрывает всё — ни один файл не трогается ──────────────


def test_translate_repo_ci_returns_empty_result_when_nothing_new(tmp_path):
    text = (
        "jobs:\n  test:\n    steps:\n"
        '      - name: "Тесты X"\n        run: echo hi\n'
    )
    repo_root, repo_ci, catalog_dir = _write(tmp_path, text)
    original = repo_ci.read_text(encoding="utf-8")

    result = gst.translate_repo_ci(
        repo_root, repo_ci=repo_ci, catalog_dir=catalog_dir, allowlist=frozenset({"Тесты X"}),
    )

    assert result.migrated == []
    assert result.changed_paths == []
    assert repo_ci.read_text(encoding="utf-8") == original
    assert list(catalog_dir.glob("*.sh")) == []


# ── Однострочный run: (прод-форма PR #878/#596/#395/#328) ───────────────────


def test_translate_repo_ci_migrates_single_line_step_and_removes_it_from_workflow(tmp_path):
    text = (
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        '      - name: "Существующий шаг"\n'
        "        run: echo keep\n"
        "\n"
        "      # Гвардия класса #876 (живой инцидент): комментарий шага,\n"
        "      # переносится в файл каталога дословно.\n"
        "      - name: Smoke критерия успеха воркера — работа доказана этим прогоном (#876)\n"
        "        run: bash scripts/lib/test/dsh-worker-success-gate.smoke.sh\n"
        "\n"
        '      - name: "Следующий шаг"\n'
        "        run: echo next\n"
    )
    repo_root, repo_ci, catalog_dir = _write(tmp_path, text)

    result = gst.translate_repo_ci(
        repo_root, repo_ci=repo_ci, catalog_dir=catalog_dir,
        allowlist=frozenset({"Существующий шаг", "Следующий шаг"}),
    )

    assert len(result.migrated) == 1
    migration = result.migrated[0]
    assert migration.step_name == "Smoke критерия успеха воркера — работа доказана этим прогоном (#876)"
    guard_path = catalog_dir / "dsh-worker-success-gate-guard.sh"
    assert migration.guard_path == guard_path
    content = guard_path.read_text(encoding="utf-8")
    assert content.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in content
    assert "bash scripts/lib/test/dsh-worker-success-gate.smoke.sh" in content
    assert "Гвардия класса #876" in content  # исходный комментарий сохранён дословно

    new_text = repo_ci.read_text(encoding="utf-8")
    assert "Smoke критерия успеха воркера" not in new_text
    assert "Гвардия класса #876" not in new_text
    assert '- name: "Существующий шаг"' in new_text
    assert '- name: "Следующий шаг"' in new_text
    # Ровно одна пустая строка между оставшимися шагами — не задвоенная.
    assert "\n\n\n" not in new_text

    assert result.changed_paths == [repo_ci, guard_path]


# ── Многострочный run: pip install + pytest -q (прод-форма PR #870) ─────────


def test_translate_repo_ci_migrates_multiline_pytest_step(tmp_path):
    text = (
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        '      - name: "Инварианты"\n'
        "        run: |\n"
        "          pip install --quiet pytest pyyaml\n"
        "          python -m pytest scripts/orchestra/test_repo_invariants.py -q\n"
        "\n"
        "      # Требование B (#869, drain-health-curator): механический потолок\n"
        "      # закрытий за авто-pm-прогон считает ФАКТ по issues/events.\n"
        "      - name: Тесты механического потолка закрытий pm-прогона\n"
        "        run: |\n"
        "          pip install --quiet pytest\n"
        "          python -m pytest scripts/orchestra/test_pm_dispatch.py -q\n"
        "\n"
        '      - name: "Автофикс"\n'
        "        run: echo autofix\n"
    )
    repo_root, repo_ci, catalog_dir = _write(tmp_path, text)

    result = gst.translate_repo_ci(
        repo_root, repo_ci=repo_ci, catalog_dir=catalog_dir,
        allowlist=frozenset({"Инварианты", "Автофикс"}),
    )

    assert len(result.migrated) == 1
    guard_path = catalog_dir / "pm-dispatch-guard.sh"
    assert result.migrated[0].guard_path == guard_path
    content = guard_path.read_text(encoding="utf-8")
    assert content.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in content
    assert "pip install --quiet pytest\n" in content
    assert "python -m pytest scripts/orchestra/test_pm_dispatch.py -q" in content

    new_text = repo_ci.read_text(encoding="utf-8")
    assert "pm_dispatch" not in new_text
    assert "Требование B (#869" not in new_text
    assert '- name: "Инварианты"' in new_text
    assert '- name: "Автофикс"' in new_text
    assert "\n\n\n" not in new_text


# ── Несколько новых шагов за один вызов (прод-форма PR #841/#640/#579/#241) ──


def test_translate_repo_ci_migrates_multiple_steps_in_one_call(tmp_path):
    text = (
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        '      - name: "База"\n'
        "        run: echo base\n"
        "\n"
        "      # Гвардия экспорта Anthropic OAuth-учёток (#840).\n"
        "      - name: Гвардия экспорта Anthropic OAuth-учёток (#840)\n"
        "        run: |\n"
        "          pip install --quiet pytest\n"
        "          python -m pytest scripts/lib/test_anthropic_oauth_import.py -q\n"
        "\n"
        "      # Класс #786/#840: gh 2.85 не знает --body-file.\n"
        '      - name: Гвардия "--body-file непортируем у gh secret/variable set" (#786)\n'
        "        run: |\n"
        "          pip install --quiet pytest\n"
        "          python -m pytest scripts/lib/test_gh_body_file_guard.py -q\n"
        "\n"
        '      - name: "Хвост"\n'
        "        run: echo tail\n"
    )
    repo_root, repo_ci, catalog_dir = _write(tmp_path, text)

    result = gst.translate_repo_ci(
        repo_root, repo_ci=repo_ci, catalog_dir=catalog_dir,
        allowlist=frozenset({"База", "Хвост"}),
    )

    slugs = sorted(m.guard_path.name for m in result.migrated)
    assert slugs == ["anthropic-oauth-import-guard.sh", "gh-body-file-guard.sh"]
    new_text = repo_ci.read_text(encoding="utf-8")
    assert "anthropic_oauth_import" not in new_text
    assert "gh_body_file_guard" not in new_text
    assert '- name: "База"' in new_text
    assert '- name: "Хвост"' in new_text
    assert "\n\n\n" not in new_text


# ── Fail loud: ключ вне name/run/working-directory — перенос НЕ выполняется ──


def test_translate_repo_ci_raises_on_unsupported_env_key(tmp_path):
    text = (
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        '      - name: "База"\n'
        "        run: echo base\n"
        "\n"
        "      - name: Гвардия с переменной окружения\n"
        "        env:\n"
        "          GH_TOKEN: ${{ github.token }}\n"
        "        run: echo ${GH_TOKEN}\n"
    )
    repo_root, repo_ci, catalog_dir = _write(tmp_path, text)
    original = repo_ci.read_text(encoding="utf-8")

    with pytest.raises(gst.UnsupportedStepError, match="env"):
        gst.translate_repo_ci(
            repo_root, repo_ci=repo_ci, catalog_dir=catalog_dir, allowlist=frozenset({"База"}),
        )

    # Атомарность: файл repo-ci.yml не тронут, каталог не пополнился.
    assert repo_ci.read_text(encoding="utf-8") == original
    assert list(catalog_dir.glob("*.sh")) == []


# ── Fail loud: run: без распознаваемого файла (прод-форма PR #241, DO-тесты) ─


def test_translate_repo_ci_raises_when_no_extractable_target(tmp_path):
    text = (
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        '      - name: "База"\n'
        "        run: echo base\n"
        "\n"
        "      - name: Тесты DO журнала (vitest + typecheck)\n"
        "        working-directory: cf-worker\n"
        "        run: |\n"
        "          npm ci --no-audit --no-fund\n"
        "          npx tsc --noEmit\n"
        "          npx vitest run\n"
    )
    repo_root, repo_ci, catalog_dir = _write(tmp_path, text)
    original = repo_ci.read_text(encoding="utf-8")

    with pytest.raises(gst.UnsupportedStepError, match="исполняемого файла"):
        gst.translate_repo_ci(
            repo_root, repo_ci=repo_ci, catalog_dir=catalog_dir, allowlist=frozenset({"База"}),
        )

    assert repo_ci.read_text(encoding="utf-8") == original
    assert list(catalog_dir.glob("*.sh")) == []


# ── Fail loud: коллизия производного имени с уже существующим файлом ────────


def test_translate_repo_ci_raises_on_filename_collision(tmp_path):
    text = (
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        '      - name: "База"\n'
        "        run: echo base\n"
        "\n"
        "      - name: Тесты дубликата\n"
        "        run: |\n"
        "          pip install --quiet pytest\n"
        "          python -m pytest scripts/lib/test_pm_dispatch.py -q\n"
    )
    repo_root, repo_ci, catalog_dir = _write(tmp_path, text)
    (catalog_dir / "pm-dispatch-guard.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\ntrue\n", encoding="utf-8")
    original = repo_ci.read_text(encoding="utf-8")

    with pytest.raises(gst.UnsupportedStepError, match="уже занято"):
        gst.translate_repo_ci(
            repo_root, repo_ci=repo_ci, catalog_dir=catalog_dir, allowlist=frozenset({"База"}),
        )

    assert repo_ci.read_text(encoding="utf-8") == original


# ── _slug_from_target: правило именования детерминировано ───────────────────


@pytest.mark.parametrize("target, expected", [
    ("scripts/orchestra/test_pm_dispatch.py", "pm-dispatch-guard"),
    ("scripts/lib/test/dsh-worker-success-gate.smoke.sh", "dsh-worker-success-gate-guard"),
    ("scripts/lib/workflow_glob_suffix_guard.py", "workflow-glob-suffix-guard"),
    ("dsh-edge/test/smoke-edge-plugins.test.mjs", "smoke-edge-plugins-guard"),
])
def test_slug_from_target_is_deterministic(target, expected):
    assert gst._slug_from_target(target) == expected
