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
покраснеет (проверено живьём: с фикстурой, где `run:` несёт распознаваемую
pytest-цель, а не голый `echo`, отсутствие проверки топит вызов в
`_step_signature`/`Counter` на нехешируемом значении `env:` — `TypeError:
unhashable type: 'dict'`, не тихая успешная миграция — но и не громкий
`UnsupportedStepError`, а именно ЭТУ регрессию проверка `extra_keys`
предотвращает). Убери проверку `targets` — покраснеет
test_translate_repo_ci_raises_when_no_extractable_target тем же способом.
Убери `_verify_removal` (issue #897, находка ревью PR #902) — краснеют
test_translate_repo_ci_raises_when_blank_line_inside_run_corrupts_neighbor и
test_translate_repo_ci_raises_on_duplicate_step_name. Убери из
`_verify_removal` deep-сверку всего документа (проверка (2), находка ревью
PR #902, второй круг) — краснеет
test_translate_repo_ci_raises_when_blank_line_inside_run_corrupts_neighbor
(имя перенесённого шага при этой порче отсутствует, проверка (1) по именам
порчу не видит). Верни глобальное схлопывание пустых строк по всему файлу
(вместо стыка удалённых диапазонов) — краснеет
test_translate_repo_ci_preserves_unrelated_job_with_blank_lines_in_heredoc
(чужой job теряет пустую строку в heredoc). Убери проверку `${{` в
run_text — краснеет
test_translate_repo_ci_raises_when_run_contains_actions_expression. Убери
проверку `catalog_invocations` (цель `run:` лежит в scripts/ci/guards/ —
находка ревью PR #902, третий круг) — краснеет
test_translate_repo_ci_raises_when_step_invokes_existing_catalog_file
(DID NOT RAISE: обёртка вокруг файла каталога заводится молча, гвардия
стала бы исполняться дважды).

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
    """Прод-форма НАРОЧНО несёт распознаваемую pytest-цель в `run:` (находка
    ревью PR #902): фикстура с `run: echo ${GH_TOKEN}` без узнаваемого
    файла и так падает громко через ДРУГУЮ проверку (`targets`), мутация
    (снятие проверки `extra_keys`) на ней ничего не доказывает — тест
    остался бы зелёным по случайной причине. С pytest-целью снятие
    `extra_keys` даёт РОВНО обещанный докстрингом модуля тихий класс: шаг
    мигрирует, `GH_TOKEN` в файле каталога отсутствует."""
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
        "        run: |\n"
        "          pip install --quiet pytest\n"
        "          python -m pytest scripts/lib/test_env_dependent.py -q\n"
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


# ── Самопроверка после удаления (находка ревью PR #902) ──────────────────────


def test_translate_repo_ci_raises_when_blank_line_inside_run_corrupts_neighbor(tmp_path):
    """`_find_step_line_range` резал диапазон удаления «до первой пустой
    строки» — пустая строка ВНУТРИ блока `run: |` (обычный стиль этого
    репозитория) обрывала диапазон раньше конца переносимого шага, а хвост
    его `run:`-блока молча приклеивался к `run:` СОСЕДНЕГО шага («База»
    начинала выполнять чужой pytest). YAML при этом синтаксически валиден
    (многострочный скаляр) — только структурная самопроверка ловит это."""
    text = (
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        '      - name: "База"\n'
        "        run: echo base\n"
        "\n"
        "      - name: Гвардия с пустой строкой в run\n"
        "        run: |\n"
        "          pip install --quiet pytest\n"
        "\n"
        "          python -m pytest scripts/lib/test_something.py -q\n"
        "\n"
        '      - name: "Хвост"\n'
        "        run: echo tail\n"
    )
    repo_root, repo_ci, catalog_dir = _write(tmp_path, text)
    original = repo_ci.read_text(encoding="utf-8")

    with pytest.raises(gst.UnsupportedStepError, match="самопроверка"):
        gst.translate_repo_ci(
            repo_root, repo_ci=repo_ci, catalog_dir=catalog_dir,
            allowlist=frozenset({"База", "Хвост"}),
        )

    # Атомарность: ни repo-ci.yml, ни каталог не тронуты найденной порчей.
    assert repo_ci.read_text(encoding="utf-8") == original
    assert list(catalog_dir.glob("*.sh")) == []


def test_translate_repo_ci_raises_on_duplicate_step_name(tmp_path):
    """Дублирующееся имя шага: `added` — множество, `_find_step_dict`/
    `_find_step_line_range` берут только ПЕРВОЕ текстовое вхождение — второй
    одноимённый шаг остался бы в файле нетронутым, но с тем же именем,
    которое считается перенесённым. Самопроверка ловит это по имени."""
    text = (
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        '      - name: "База"\n'
        "        run: echo base\n"
        "\n"
        "      - name: Гвардия дубликата\n"
        "        run: python -m pytest scripts/lib/test_dup_a.py -q\n"
        "\n"
        "      - name: Гвардия дубликата\n"
        "        run: python -m pytest scripts/lib/test_dup_b.py -q\n"
    )
    repo_root, repo_ci, catalog_dir = _write(tmp_path, text)
    original = repo_ci.read_text(encoding="utf-8")

    with pytest.raises(gst.UnsupportedStepError, match="самопроверка"):
        gst.translate_repo_ci(
            repo_root, repo_ci=repo_ci, catalog_dir=catalog_dir, allowlist=frozenset({"База"}),
        )

    assert repo_ci.read_text(encoding="utf-8") == original
    assert list(catalog_dir.glob("*.sh")) == []


def test_translate_repo_ci_preserves_unrelated_job_with_blank_lines_in_heredoc(tmp_path):
    """Находка ревью PR #902 (второй круг): схлопывание задвоенных пустых
    строк шло по ВСЕМУ файлу, а структурная сверка смотрела только на job
    `test` — чужой job с `run: |`, внутри которого heredoc с ДВУМЯ пустыми
    строками подряд (легальное содержимое, прод-форма archive-fixup-подобных
    job), молча терял пустую строку: опубликованный файл отличался от
    «старые шаги минус перенесённые», и ни одна проверка этого не видела.
    Схлопывание теперь идёт только в окрестности удалённых диапазонов:
    содержимое чужого job сохраняется БАЙТ В БАЙТ.

    Мутация, доказывающая класс: верни глобальное схлопывание (проход
    «убрать все задвоенные пустые строки» по всему файлу после удаления) —
    тест краснеет: `\n\n\n` в теле heredoc чужого job исчезает.

    Замечание: на дату ревью deep-проверка `_verify_removal` расширена на
    ВЕСЬ документ, так что глобальное схлопывание ловилось бы и ею (громко,
    отказом переноса); тест фиксирует более сильный контракт — чужие части
    файла не перекрашиваются ВООБЩЕ, перенос идёт без ложного отказа."""
    other_job_body = (
        "  archive-fixup:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: Архивация отчёта\n"
        "        run: |\n"
        "          cat > report.txt <<'EOF'\n"
        "          строка1\n"
        "\n"
        "\n"
        "          строка2\n"
        "          EOF\n"
        "          cat report.txt\n"
    )
    text = (
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        '      - name: "База"\n'
        "        run: echo base\n"
        "\n"
        "      - name: Гвардия рядом с чужим heredoc\n"
        "        run: |\n"
        "          pip install --quiet pytest\n"
        "          python -m pytest scripts/lib/test_near_heredoc.py -q\n"
        "\n"
        '      - name: "Хвост"\n'
        "        run: echo tail\n"
        f"{other_job_body}"
    )
    repo_root, repo_ci, catalog_dir = _write(tmp_path, text)

    result = gst.translate_repo_ci(
        repo_root, repo_ci=repo_ci, catalog_dir=catalog_dir,
        allowlist=frozenset({"База", "Хвост"}),
    )

    assert [m.step_name for m in result.migrated] == ["Гвардия рядом с чужим heredoc"]
    new_text = repo_ci.read_text(encoding="utf-8")
    # Задвоенная пустая строка в heredoc ЧУЖОГО job — на месте, байт в байт.
    assert "строка1\n\n\n          строка2" in new_text
    assert other_job_body in new_text
    # Рядом с удалённым шагом задвоения нет — схлопывание работает там, где
    # оно нужно (стык удаления), и только там.
    assert "\n\n\n" not in new_text.replace("строка1\n\n\n          строка2", "")


def test_translate_repo_ci_raises_when_run_contains_actions_expression(tmp_path):
    """Некритичное замечание ревью PR #902, поднятое до громкого отказа:
    `${{ … }}` вычисляет GitHub Actions при прогоне workflow — в файле
    каталога оно осталось бы дословным текстом («bad substitution» от
    shell), причём исходный шаг к тому моменту уже удалён из repo-ci.yml,
    чинить было бы негде. Отказ ДО любых записей, как и остальные
    UnsupportedStepError."""
    text = (
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        '      - name: "База"\n'
        "        run: echo base\n"
        "\n"
        "      - name: Гвардия с выражением Actions\n"
        "        run: |\n"
        "          pip install --quiet pytest\n"
        "          python -m pytest scripts/lib/test_expr.py -q --run-id=${{ github.run_id }}\n"
    )
    repo_root, repo_ci, catalog_dir = _write(tmp_path, text)
    original = repo_ci.read_text(encoding="utf-8")

    with pytest.raises(gst.UnsupportedStepError, match=r"\$\{\{"):
        gst.translate_repo_ci(
            repo_root, repo_ci=repo_ci, catalog_dir=catalog_dir, allowlist=frozenset({"База"}),
        )

    assert repo_ci.read_text(encoding="utf-8") == original
    assert list(catalog_dir.glob("*.sh")) == []


# ── Fail loud: run: вызывает уже существующий файл каталога (ревью ──────────
# ── PR #902, третий круг) ────────────────────────────────────────────────────


def test_translate_repo_ci_raises_when_step_invokes_existing_catalog_file(tmp_path):
    """Рукописный шаг, чей run: вызывает УЖЕ СУЩЕСТВУЮЩИЙ файл каталога с
    нестем-`-guard` именем (класс обхода (б) из #771 — «Проверка окружения»
    → `bash scripts/ci/guards/ci-guard-registration.sh`), раньше переносился
    «успешно»: транслятор заводил в каталоге обёртку
    `ci-guard-registration-guard.sh` с телом `bash scripts/ci/guards/
    ci-guard-registration.sh`, шаг из repo-ci.yml удалялся, CI зелёный — а
    настоящая гвардия отныне исполнялась ДВАЖДЫ
    (`check_catalog_handwritten_overlap` это не видит: цель обёртки — путь
    каталога, пересечения с рукописными шагами нет), и мутация-критерий
    #749 («удали файл каталога → должно покраснеть») молча не срабатывала.
    На стемах `-guard` тот же класс спасала только проверка коллизии —
    живые стемы каталога (`ci-guard-registration.sh`,
    `run-guards-mechanism.sh`) не всегда `-guard`.

    Мутация, доказывающая класс: убери проверку `catalog_invocations` в
    translate_repo_ci — тест краснеет (DID NOT RAISE), в каталоге
    появляется обёртка, repo-ci.yml теряет рукописный шаг."""
    text = (
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        '      - name: "База"\n'
        "        run: echo base\n"
        "\n"
        "      - name: Проверка окружения\n"
        "        run: bash scripts/ci/guards/ci-guard-registration.sh\n"
    )
    repo_root, repo_ci, catalog_dir = _write(tmp_path, text)
    (catalog_dir / "ci-guard-registration.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "python scripts/lib/ci_guard_registration_guard.py\n",
        encoding="utf-8",
    )
    original = repo_ci.read_text(encoding="utf-8")

    with pytest.raises(gst.UnsupportedStepError, match="уже зарегистрирована"):
        gst.translate_repo_ci(
            repo_root, repo_ci=repo_ci, catalog_dir=catalog_dir, allowlist=frozenset({"База"}),
        )

    # Атомарность: repo-ci.yml не тронут, в каталоге только исходный файл —
    # никакой обёртки не появилось.
    assert repo_ci.read_text(encoding="utf-8") == original
    assert sorted(p.name for p in catalog_dir.glob("*.sh")) == ["ci-guard-registration.sh"]


# ── _slug_from_target: правило именования детерминировано ───────────────────


@pytest.mark.parametrize("target, expected", [
    ("scripts/orchestra/test_pm_dispatch.py", "pm-dispatch-guard"),
    ("scripts/lib/test/dsh-worker-success-gate.smoke.sh", "dsh-worker-success-gate-guard"),
    ("scripts/lib/workflow_glob_suffix_guard.py", "workflow-glob-suffix-guard"),
    ("dsh-edge/test/smoke-edge-plugins.test.mjs", "smoke-edge-plugins-guard"),
])
def test_slug_from_target_is_deterministic(target, expected):
    assert gst._slug_from_target(target) == expected
