#!/usr/bin/env python3
"""Тесты гвардии механизма регистрации CI-гвардий
(scripts/lib/ci_guard_registration_guard.py, #749).

Мутация, доказывающая класс, который эта гвардия закрывает: дописать шаг
`- name: Гвардия <что-то новое>` рукописно в фикстуру repo-ci.yml без
добавления его в ALLOWLIST — `check_no_undeclared_step` обязан вернуть
непустой список; убрать шаг из фикстуры, оставив имя в ALLOWLIST, —
тоже непустой список (устаревшая запись реестра, тот же приём, что
`test_label_registry.py`/`test_infra_gh_inventory.py`).

Ревью PR #771, блокирующая 2 — второй класс мутации: переименовать шаг ВНЕ
соглашения именования (`Проверка …`/`Канарейка …`/`Инвариант …`/
`Мутационный тест …`/без `name:` вовсе), не трогая его `run:` — раньше
`EXIT=0`, гвардия такой шаг не видела вообще. Детекция обязана ловить его по
содержимому `run:` (pytest/node --test/тестовый-guard-файл), не по имени.

Запуск: python -m pytest scripts/lib/test_ci_guard_registration_guard.py -q
"""

import importlib.util
from pathlib import Path

import yaml

_DIR = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "ci_guard_registration_guard", _DIR / "ci_guard_registration_guard.py"
)
crg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crg)  # type: ignore[union-attr]

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_repo_ci_full(tmp_path: Path, steps: list[dict]) -> Path:
    """`steps` — список словарей `{"name": ..., "run": ...}` (name может
    отсутствовать). `yaml.safe_dump`, не собранный вручную текст — реальные
    `run:`-блоки с переносами строк не всегда безопасны как plain-скаляр."""
    doc = {"jobs": {"test": {"steps": steps}}}
    path = tmp_path / "repo-ci.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return path


def _write_repo_ci(tmp_path: Path, step_names: list[str]) -> Path:
    steps_yaml = "\n".join(f'      - name: "{n}"\n        run: echo hi' for n in step_names)
    content = "jobs:\n  test:\n    steps:\n" + steps_yaml + "\n"
    path = tmp_path / "repo-ci.yml"
    path.write_text(content, encoding="utf-8")
    return path


# ── guard_step_names: только совпадающий префикс ────────────────────────────

def test_guard_step_names_keeps_only_matching_prefixes(tmp_path):
    path = _write_repo_ci(tmp_path, ["Тесты X", "Гвардия Y", "checkout", "Smoke Z", "Юнит-тесты W"])
    assert crg.guard_step_names(path) == {"Тесты X", "Гвардия Y", "Smoke Z", "Юнит-тесты W"}


def test_guard_step_names_case_insensitive_prefix():
    path_dummy = None
    # регэксп сам по себе, без файла: гвардия/ГВАРДИЯ/гвардия — один класс
    assert crg._GUARD_NAME_RE.match("гвардия нижний регистр")
    assert crg._GUARD_NAME_RE.match("ГВАРДИЯ верхний регистр")


def test_guard_step_names_does_not_match_infrastructure_steps(tmp_path):
    # "Каталог гвардий scripts/ci/guards — перебор" — имя перебора каталога
    # намеренно не начинается с Тест/Гвардия/Smoke/Юнит-тест, иначе гвардия
    # путала бы сам перебор с рукописной регистрацией.
    path = _write_repo_ci(tmp_path, ["Каталог гвардий scripts/ci/guards — перебор (#749)", "checkout"])
    assert crg.guard_step_names(path) == set()


def test_perebor_step_itself_is_infra_exempt_by_construction(tmp_path):
    # Тот же шаг ещё и явно перечислен в INFRA_EXEMPT_STEP_NAMES (major 5,
    # ревью PR #771) — регресс-тест: если когда-нибудь run: перебора начнёт
    # совпадать с содержимым-критериями (например добавят node --test внутрь
    # него), явное исключение не даст ему просочиться в ALLOWLIST молча.
    step = crg.INFRA_EXEMPT_STEP_NAMES.__iter__().__next__()
    path = _write_repo_ci_full(tmp_path, [{"name": step, "run": "node --test whatever.test.mjs"}])
    assert crg.guard_step_names(path) == set()


# ── guard_step_names: обходной путь по содержимому run: (ревью PR #771, ────
# ── блокирующая 2) — переименование НЕ выводит шаг из-под гвардии ──────────

def test_guard_step_names_catches_pytest_step_named_outside_convention(tmp_path):
    for outside_name in (
        "Проверка чего-то нового (#999)",
        "Канарейка чего-то (#999)",
        "Инвариант чего-то (#999)",
        "Мутационный тест чего-то (#999)",
    ):
        path = _write_repo_ci_full(
            tmp_path,
            [{"name": outside_name, "run": "pip install --quiet pytest\npytest scripts/lib/test_x.py -q"}],
        )
        assert crg.guard_step_names(path) == {outside_name}, (
            f"имя {outside_name!r} вне соглашения обязано ловиться по содержимому run:"
        )


def test_guard_step_names_catches_node_test_step_named_outside_convention(tmp_path):
    path = _write_repo_ci_full(
        tmp_path, [{"name": "Проверка бандла", "run": "node --test scripts/lib/test/x.test.mjs"}]
    )
    assert crg.guard_step_names(path) == {"Проверка бандла"}


def test_guard_step_names_catches_bash_test_file_step_named_outside_convention(tmp_path):
    path = _write_repo_ci_full(
        tmp_path, [{"name": "Инвариант чего-то", "run": "bash scripts/git/test/foo.test.sh"}]
    )
    assert crg.guard_step_names(path) == {"Инвариант чего-то"}


def test_guard_step_names_catches_direct_guard_py_invocation_outside_convention(tmp_path):
    path = _write_repo_ci_full(
        tmp_path, [{"name": "Живой снимок чего-то", "run": "python scripts/lib/exec_bit_guard.py"}]
    )
    assert crg.guard_step_names(path) == {"Живой снимок чего-то"}


def test_guard_step_names_ignores_unrelated_inline_step(tmp_path):
    # Инлайн-проверка без вызова pytest/node --test/тестового-guard-файла
    # (белые пятна, bash -n, workflows валидны) — не ловится по содержимому,
    # только именем: это НЕ регресс, тот же класс, что раньше.
    path = _write_repo_ci_full(
        tmp_path, [{"name": "Проверка чего-то инлайн", "run": "gh issue list --label foo"}]
    )
    assert crg.guard_step_names(path) == set()


def test_guard_step_names_catches_step_without_name_at_all(tmp_path):
    # «шаг вообще без name:» — раньше проходил мимо целиком; теперь ловится
    # по содержимому, синтетическая метка гарантированно не в ALLOWLIST.
    path = _write_repo_ci_full(tmp_path, [{"run": "pytest scripts/lib/test_x.py -q"}])
    found = crg.guard_step_names(path)
    assert len(found) == 1
    assert "без name:" in next(iter(found))


def test_double_registration_handwritten_and_catalog_both_run_same_guard(tmp_path):
    # Ревью PR #771, блокирующая 2 — сценарий двойного прогона: гвардия уже
    # перенесена в каталог (scripts/ci/guards/x.sh), но кто-то ВЕРНУЛ её же
    # рукописным шагом под именем вне соглашения («Проверка X»). Раньше это
    # проходило мимо ОБЕИХ сторон (не в ALLOWLIST — но и не обнаружено, имя
    # не матчилось). Теперь содержимое run: (тот же pytest-вызов, что и в
    # файле каталога) ловится вне зависимости от имени и ОБЯЗАНО оказаться
    # в списке "added" (не в ALLOWLIST) — обходной рукописный дубль краснит.
    path = _write_repo_ci_full(
        tmp_path,
        [{"name": "Проверка X — дубль каталога", "run": "pytest scripts/lib/test_x.py -q"}],
    )
    problems = crg.check_no_undeclared_step(path, frozenset())
    assert any("Проверка X — дубль каталога" in p for p in problems)


# ── ALLOWLIST_RATCHET_MAX: только вниз (ревью PR #771, major 5) ─────────────

def test_ratchet_flags_allowlist_grown_past_ceiling(tmp_path):
    grown = frozenset({f"Тесты фиктивная {i}" for i in range(crg.ALLOWLIST_RATCHET_MAX + 1)})
    path = _write_repo_ci(tmp_path, sorted(grown))
    problems = crg.check_no_undeclared_step(path, grown)
    assert any("ALLOWLIST_RATCHET_MAX" in p for p in problems)


def test_live_allowlist_is_within_ratchet_ceiling():
    assert len(crg.ALLOWLIST) <= crg.ALLOWLIST_RATCHET_MAX, (
        f"ALLOWLIST ({len(crg.ALLOWLIST)}) превысил потолок "
        f"ALLOWLIST_RATCHET_MAX ({crg.ALLOWLIST_RATCHET_MAX}) — потолок обязан "
        "убывать, не расти"
    )


# ── check_no_undeclared_step: обе стороны мутации ────────────────────────────

def test_check_flags_new_undeclared_step(tmp_path):
    allowlist = frozenset({"Тесты X"})
    path = _write_repo_ci(tmp_path, ["Тесты X", "Гвардия новая рукописная"])
    problems = crg.check_no_undeclared_step(path, allowlist)
    assert any("Гвардия новая рукописная" in p for p in problems)
    assert not any("Тесты X" in p for p in problems)


def test_check_flags_stale_allowlist_entry(tmp_path):
    allowlist = frozenset({"Тесты X", "Тесты давно мигрированной гвардии"})
    path = _write_repo_ci(tmp_path, ["Тесты X"])
    problems = crg.check_no_undeclared_step(path, allowlist)
    assert any("Тесты давно мигрированной гвардии" in p for p in problems)


def test_check_clean_when_in_sync(tmp_path):
    allowlist = frozenset({"Тесты X", "Гвардия Y"})
    path = _write_repo_ci(tmp_path, ["Тесты X", "Гвардия Y", "checkout"])
    assert crg.check_no_undeclared_step(path, allowlist) == []


def test_check_reports_both_directions_at_once(tmp_path):
    allowlist = frozenset({"Тесты старая", "Тесты X"})
    path = _write_repo_ci(tmp_path, ["Тесты X", "Гвардия новая"])
    problems = crg.check_no_undeclared_step(path, allowlist)
    assert len(problems) == 2
    joined = " ".join(problems)
    assert "Гвардия новая" in joined
    assert "Тесты старая" in joined


# ── Живой снимок: сама гвардия на реальном repo-ci.yml ──────────────────────

def test_live_repo_ci_matches_frozen_allowlist():
    problems = crg.check_no_undeclared_step()
    assert problems == [], (
        "рукописные шаги гвардий в .github/workflows/repo-ci.yml разошлись с "
        f"ALLOWLIST scripts/lib/ci_guard_registration_guard.py: {problems}"
    )


def test_catalog_perebor_step_itself_is_present_in_repo_ci():
    repo_ci = (REPO_ROOT / ".github" / "workflows" / "repo-ci.yml").read_text(encoding="utf-8")
    assert "scripts/ci/run_guards.sh" in repo_ci, (
        "перебор каталога гвардий (#749) не подключён к repo-ci.yml — "
        "каталог scripts/ci/guards/ никогда не исполняется"
    )
