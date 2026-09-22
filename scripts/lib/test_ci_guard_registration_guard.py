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


# ── check_catalog_handwritten_overlap: сверка ПО СОДЕРЖИМОМУ (ревью PR #771, ─
# ── блокирующая 1) ───────────────────────────────────────────────────────────

def test_catalog_and_handwritten_step_running_same_target_is_flagged(tmp_path):
    # Блокирующая 1(а): частичный перенос — файл гвардии уже лежит в
    # каталоге, но старый рукописный шаг (и его запись ALLOWLIST) не убрали.
    # Раньше это проходило мимо ВСЕГО механизма: шаг в ALLOWLIST — не «новый
    # незарегистрированный», гвардия каталога про рукописные шаги не знает
    # вовсе. Мутация-критерий #749 (удалить файл каталога → должно
    # покраснеть) при этом молча не срабатывала бы: тест продолжал бы
    # гоняться из забытого рукописного шага.
    catalog_dir = tmp_path / "guards"
    catalog_dir.mkdir()
    (catalog_dir / "x-guard.sh").write_text(
        "set -euo pipefail\npytest scripts/lib/test_x.py -q\n", encoding="utf-8"
    )
    repo_ci = _write_repo_ci_full(
        tmp_path,
        [{"name": "Тесты X", "run": "pip install --quiet pytest\npytest scripts/lib/test_x.py -q"}],
    )
    problems = crg.check_catalog_handwritten_overlap(repo_ci, catalog_dir)
    assert any("scripts/lib/test_x.py" in p and "x-guard.sh" in p and "Тесты X" in p for p in problems), problems

    # То же самое видно и через полный вход check_no_undeclared_step, даже
    # когда имя шага УЖЕ в ALLOWLIST (частичный перенос не «новый шаг»).
    problems_full = crg.check_no_undeclared_step(
        repo_ci, frozenset({"Тесты X"}), catalog_dir=catalog_dir
    )
    assert any("scripts/lib/test_x.py" in p for p in problems_full), problems_full


def test_catalog_handwritten_overlap_clean_when_no_shared_target(tmp_path):
    catalog_dir = tmp_path / "guards"
    catalog_dir.mkdir()
    (catalog_dir / "x-guard.sh").write_text(
        "set -euo pipefail\npytest scripts/lib/test_x.py -q\n", encoding="utf-8"
    )
    repo_ci = _write_repo_ci_full(
        tmp_path,
        [{"name": "Тесты Y", "run": "pip install --quiet pytest\npytest scripts/lib/test_y.py -q"}],
    )
    assert crg.check_catalog_handwritten_overlap(repo_ci, catalog_dir) == []


def test_handwritten_step_invoking_catalog_path_is_content_registration(tmp_path):
    # Блокирующая 1(б): рукописный шаг под НЕЙТРАЛЬНЫМ именем (вне
    # соглашения Тест/Гвардия/Smoke/Юнит-тест) напрямую вызывает файл
    # каталога — `_is_guard_file_path` путь `scripts/ci/guards/` не ловит
    # (критерий «файл в директории test/» не выполняется). Живая мутация
    # ревью: полный repo-ci.yml с дописанным таким шагом давал
    # `check_no_undeclared_step` → 0 проблем, EXIT=0.
    path = _write_repo_ci_full(
        tmp_path, [{"name": "Проверка окружения", "run": "bash scripts/ci/guards/x-guard.sh"}]
    )
    assert crg.guard_step_names(path) == {"Проверка окружения"}
    problems = crg.check_no_undeclared_step(path, frozenset())
    assert any("Проверка окружения" in p for p in problems), problems


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
    # "Гвардия Y" здесь намеренно заменена на "Smoke Y" (issue #1069, см.
    # test_allowlist_entry_named_guard_is_flagged ниже): ALLOWLIST-запись,
    # самоназванная «Гвардия», сама по себе теперь не «чистое» состояние —
    # check_allowlist_entries_are_migratable красит её как долг каталога,
    # даже если found == allowlist.
    allowlist = frozenset({"Тесты X", "Smoke Y"})
    path = _write_repo_ci(tmp_path, ["Тесты X", "Smoke Y", "checkout"])
    assert crg.check_no_undeclared_step(
        path, allowlist,
        job_state_exempt=frozenset(), infra_exempt=frozenset(), debt=frozenset(),
    ) == []


def test_check_reports_both_directions_at_once(tmp_path):
    allowlist = frozenset({"Тесты старая", "Тесты X"})
    path = _write_repo_ci(tmp_path, ["Тесты X", "Гвардия новая"])
    problems = crg.check_no_undeclared_step(
        path, allowlist,
        job_state_exempt=frozenset(), infra_exempt=frozenset(), debt=frozenset(),
    )
    assert len(problems) == 2
    joined = " ".join(problems)
    assert "Гвардия новая" in joined
    assert "Тесты старая" in joined


# ── Газ (issue #897): сообщение называет точный файл/шаблон/что удалить ─────


def test_check_message_names_exact_guard_filename_when_derivable(tmp_path):
    """Правило AGENTS.md «Тормоз без газа не принимается»: находка обязана
    называть, КУДА именно переносить, а не только сам факт нарушения —
    имя вычислено тем же способом, что реальный перенос
    (guard_step_translator.py::_slug_from_target), одно место правды."""
    path = _write_repo_ci_full(tmp_path, [
        {"name": "Тесты X", "run": "echo hi"},
        {
            "name": "Смоук новой находки (#901)",
            "run": "pip install --quiet pytest\npython -m pytest scripts/lib/test_scratch_thing.py -q\n",
        },
    ])
    problems = crg.check_no_undeclared_step(
        path, frozenset({"Тесты X"}),
        job_state_exempt=frozenset(), infra_exempt=frozenset(), debt=frozenset(),
    )
    assert len(problems) == 1
    assert "scripts/ci/guards/scratch-thing-guard.sh" in problems[0]
    assert "set -euo pipefail" in problems[0]
    assert "удали из repo-ci.yml сам шаг целиком" in problems[0]


def test_check_message_falls_back_to_generic_rule_when_target_not_extractable(tmp_path):
    """`run:` без распознаваемого файла (grep/echo-проверка, класс шага PR
    #241 «Тесты DO журнала») — точное имя вычислить нельзя (честная граница,
    см. guard_step_translator.py), сообщение всё равно называет ПРАВИЛО
    именования, не молчит про «как переносить»."""
    path = _write_repo_ci_full(tmp_path, [
        {"name": "Тесты X", "run": "echo hi"},
        {"name": "Гвардия новая инлайн-проверка", "run": "grep -rn foo scripts/ || exit 1"},
    ])
    problems = crg.check_no_undeclared_step(
        path, frozenset({"Тесты X"}),
        job_state_exempt=frozenset(), infra_exempt=frozenset(), debt=frozenset(),
    )
    assert len(problems) == 1
    assert "scripts/ci/guards/<имя>-guard.sh" in problems[0]
    assert "test_foo.py" in problems[0]  # правило именования объяснено примером


def test_check_message_advises_deletion_when_step_invokes_catalog_file(tmp_path):
    """Находка ревью PR #902 (третий круг): run: шага сам вызывает файл
    каталога с нестем-`-guard` именем — гвардия уже зарегистрирована,
    переносить нечего. Общий газ («создай scripts/ci/guards/<имя>-guard.sh»,
    куда честно падал `_suggest_guard_filename`) здесь советовал бы обёртку,
    исполняющую гвардию ДВАЖДЫ (класс обхода (б) из #771); газ для этого
    класса — удаление шага, каталог не трогается вовсе.

    Мутация, доказывающая класс: убери ветку `catalog_invocations` в
    check_no_undeclared_step — тест краснеет («создай» появляется, совета
    удалить шаг нет)."""
    catalog_dir = tmp_path / "guards"
    catalog_dir.mkdir()
    path = _write_repo_ci_full(tmp_path, [
        {"name": "Тесты X", "run": "echo hi"},
        {"name": "Проверка окружения", "run": "bash scripts/ci/guards/ci-guard-registration.sh"},
    ])
    problems = crg.check_no_undeclared_step(
        path, frozenset({"Тесты X"}), catalog_dir=catalog_dir,
        job_state_exempt=frozenset(), infra_exempt=frozenset(), debt=frozenset(),
        presence=frozenset(),
    )
    assert len(problems) == 1, problems
    assert "УЖЕ зарегистрирована" in problems[0]
    assert "ci-guard-registration.sh" in problems[0]
    assert "удали рукописный шаг целиком" in problems[0]
    assert "создай" not in problems[0]  # совет завести обёртку недопустим


def test_check_message_advises_deletion_when_catalog_already_runs_target(tmp_path):
    """CONTENT-форма (находка ревью PR #902, четвёртый круг): цель шага не
    лежит в scripts/ci/guards/ и стем не совпадает, но её уже исполняет
    существующий файл каталога. Общий газ говорил бы «создай scripts/ci/
    guards/ci-guard-registration-guard.sh» — а overlap-сверка в том же
    отчёте говорила «убери рукописный шаг»: противоречивый совет в одном
    отчёте, и «перенеси под другим именем» ведёт в невидимую двойную
    регистрацию. Единая ветка газа называет факт и советует удаление.

    Мутация, доказывающая класс: убери `content_overlaps` из условия ветки
    — тест краснеет («создай» появляется, совета удалить нет)."""
    catalog_dir = tmp_path / "guards"
    catalog_dir.mkdir()
    (catalog_dir / "ci-guard-registration.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "python scripts/lib/ci_guard_registration_guard.py\n",
        encoding="utf-8",
    )
    path = _write_repo_ci_full(tmp_path, [
        {"name": "Тесты X", "run": "echo hi"},
        {"name": "Гвардия регистрации CI-гвардий", "run": "python scripts/lib/ci_guard_registration_guard.py"},
    ])
    problems = crg.check_no_undeclared_step(
        path, frozenset({"Тесты X"}), catalog_dir=catalog_dir,
        job_state_exempt=frozenset(), infra_exempt=frozenset(), debt=frozenset(),
        presence=frozenset(),
    )
    # Content-форма видна ДВУМ независимым сверкам (overlap по содержимому
    # и ветка газа «уже зарегистрирована») — обе в отчёте, ОБЕ советуют
    # удалить рукописный шаг; недопустимо только противоречие «создай».
    assert len(problems) == 2, problems
    assert all("создай" not in p for p in problems), problems
    assert any("УЖЕ зарегистрирована" in p for p in problems)
    assert any("ci-guard-registration.sh" in p for p in problems)
    assert any("scripts/lib/ci_guard_registration_guard.py" in p for p in problems)
    assert any("удали рукописный шаг целиком" in p for p in problems)
    assert any("частичный перенос: убери рукописный шаг" in p for p in problems)


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


# ── check_allowlist_entries_are_migratable: остаточный класс #1069 ──────────
#
# ci_guard_registration_guard уже не даёт появиться НОВОМУ рукописному шагу
# гвардии (found - allowlist), но раньше ничего не мешало СУЩЕСТВУЮЩЕЙ
# ALLOWLIST-записи молча остаться «гвардией вне каталога» навсегда — именно
# так provider-default.guard.sh дожил в ALLOWLIST до живых падений PR
# #1068/#1095 при зелёном локальном run_guards.sh. Мутация: любая из трёх
# фикстур ниже воспроизводит ЭТОТ класс (запись остаётся в ALLOWLIST,
# found == allowlist, но проверка обязана покраснеть); удаление проверки
# (или увеличение фикстуры до `frozenset()`) красит все три теста — то есть
# check_no_undeclared_step() сам по себе (без этой функции) не ловит
# находку.

def test_allowlist_entry_named_guard_is_flagged(tmp_path):
    allowlist = frozenset({"Гвардия литерала X"})
    path = _write_repo_ci(tmp_path, ["Гвардия литерала X"])
    problems = crg.check_allowlist_entries_are_migratable(path, allowlist)
    assert len(problems) == 1
    assert "Гвардия литерала X" in problems[0]
    assert "самоназванную" in problems[0]
    # Мутация: та же фикстура, но через полный check_no_undeclared_step —
    # found == allowlist (ничего не «добавлено»/«убрано»), но проверка
    # обязана оставаться красной.
    assert crg.check_no_undeclared_step(path, allowlist) != []


def test_allowlist_entry_with_bare_guard_sh_invocation_is_flagged(tmp_path):
    allowlist = frozenset({"Провайдер X"})
    doc = {"jobs": {"test": {"steps": [
        {"name": "Провайдер X", "run": "bash scripts/lib/test/x.guard.sh"},
    ]}}}
    path = tmp_path / "repo-ci.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    problems = crg.check_allowlist_entries_are_migratable(path, allowlist)
    assert len(problems) == 1
    assert "x.guard.sh" in problems[0]


def test_allowlist_entry_with_bare_python_script_is_flagged(tmp_path):
    allowlist = frozenset({"Канарейка X"})
    doc = {"jobs": {"test": {"steps": [
        {"name": "Канарейка X", "run": "python scripts/lib/orphan_test_guard.py"},
    ]}}}
    path = tmp_path / "repo-ci.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    problems = crg.check_allowlist_entries_are_migratable(path, allowlist)
    assert len(problems) == 1
    assert "orphan_test_guard.py" in problems[0]


def test_allowlist_entry_pytest_of_own_module_is_not_flagged(tmp_path):
    """Ordinary unit-test suite (pytest test_X.py тестирует X.py) остаётся
    легальной ALLOWLIST-записью — issue #1069 честно не требует переносить
    unit-тесты конкретного модуля, только «голые» снимки/самоназванные
    гвардии (см. докстринг check_allowlist_entries_are_migratable)."""
    allowlist = frozenset({"Тесты X"})
    doc = {"jobs": {"test": {"steps": [
        {"name": "Тесты X", "run": "pip install --quiet pytest\npython -m pytest scripts/lib/test_x.py -q"},
    ]}}}
    path = tmp_path / "repo-ci.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    assert crg.check_allowlist_entries_are_migratable(path, allowlist) == []


def test_allowlist_job_state_exempt_entry_is_not_flagged(tmp_path):
    """ALLOWLIST_JOB_STATE_EXEMPT — единственная НАЗВАННАЯ оговорка (#1069):
    «Квота GitHub API» несёт `id:`/`if:`-связь со следующим шагом и не может
    жить в каталоге без спекулятивной инфраструктуры (design.md #749).
    Проверке не подлежит — иначе именованное исключение красило бы CI на
    каждом прогоне без возможности его снять."""
    allowlist = frozenset({"Квота GitHub API — ранняя проверка (все шаги, читающие API)"})
    doc = {"jobs": {"test": {"steps": [
        {
            "name": "Квота GitHub API — ранняя проверка (все шаги, читающие API)",
            "run": "python scripts/lib/rate_guard.py --job repo-ci-invariants",
        },
    ]}}}
    path = tmp_path / "repo-ci.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    assert crg.check_allowlist_entries_are_migratable(
        path, allowlist, job_state_exempt=allowlist
    ) == []


# ── check_bare_invocations_are_accounted: слепое пятно детекции ─────────────
#
# Пункты 1–4 модуля видят шаг по имени (Тест/Гвардия/Smoke/Юнит-тест) или по
# «тестовому» содержимому run: (pytest/node --test/тестовый файл). Шаг под
# НЕЙТРАЛЬНЫМ именем с «голым» вызовом файла (node dsh-edge/manifest.mjs,
# python scripts/orchestra/repo_invariants.py) не виден НИЧЕМУ: ни детекции,
# ни ALLOWLIST (туда и не попадал) — рукописная гвардия появлялась мимо всего
# механизма (issue #1069, находка ревью PR #1117; живые жертвы — шесть
# dsh-edge-проверок job `test`, учтённые теперь GUARD_MIGRATION_DEBT;
# второй круг ревью расширил множество до ЛЮБЫХ форм рукописных гвардий
# (grep-инварианты, compileall, bash -n, heredoc) — GUARD_MIGRATION_DEBT).
#
# Мутации, доказывающие гвардию:
#   (1) вырезать вызов check_bare_invocations_are_accounted из
#       check_no_undeclared_step → краснеет
#       test_unaccounted_bare_step_is_flagged_through_check (witness проводки
#       сверки в CI-гейт, а не только в тест);
#   (2) убрать имя из GUARD_MIGRATION_DEBT (или из
#       ALLOWLIST_JOB_STATE_EXEMPT) → краснеет
#       test_live_repo_ci_bare_steps_are_all_accounted;
#   (3) удалить/переименовать шаг «Инварианты состояния репозитория…» в
#       repo-ci.yml → краснеет и presence-тест пары ниже, и та же live-сверка
#       (мёртвое исключение).

def test_unaccounted_bare_step_is_flagged(tmp_path):
    """Шаг под нейтральным именем с «голым» вызовом, не учтённый ни одним
    именованным множеством и не покрытый каталогом, обязан красить."""
    doc = {"jobs": {"test": {"steps": [
        {"name": "Канарейка окружения", "run": "node dsh-edge/manifest.mjs"},
    ]}}}
    path = tmp_path / "repo-ci.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    problems = crg.check_bare_invocations_are_accounted(
        path,
        catalog_dir=tmp_path / "нет-такого-каталога",
        job_state_exempt=frozenset(),
        infra_exempt=frozenset(),
        debt=frozenset(),
        allowlist=frozenset(),
    )
    assert len(problems) == 1
    assert "Канарейка окружения" in problems[0]
    assert "manifest.mjs" in problems[0]


def test_unaccounted_bare_step_is_flagged_through_check(tmp_path):
    """Мутация (1): та же находка обязана проходить и через CI-вход
    check_no_undeclared_step — вырезание вызова сверки из него красит этот
    тест (fixture: имя нейтральное, run: не pytest/node --test — шага не
    видит ни детекция имени, ни содержимого, ни ALLOWLIST-сверка)."""
    doc = {"jobs": {"test": {"steps": [
        {"name": "Канарейка окружения", "run": "node dsh-edge/manifest.mjs"},
    ]}}}
    path = tmp_path / "repo-ci.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    problems = crg.check_no_undeclared_step(
        path, frozenset(), catalog_dir=tmp_path / "нет-такого-каталога",
        job_state_exempt=frozenset(), infra_exempt=frozenset(), debt=frozenset(),
    )
    assert problems != []
    assert any("manifest.mjs" in p for p in problems)


def test_bare_step_covered_by_catalog_is_accounted(tmp_path):
    """«Голое» попадание, цель которого уже исполняет файл каталога, —
    учтённое: локальный run_guards.sh её покрывает (а рукописный дубль
    отдельно красит overlap-сверка — делие труда проверяется тут же)."""
    doc = {"jobs": {"test": {"steps": [
        {"name": "Канарейка окружения", "run": "node dsh-edge/manifest.mjs"},
    ]}}}
    path = tmp_path / "repo-ci.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    catalog_dir = tmp_path / "guards"
    catalog_dir.mkdir()
    (catalog_dir / "manifest-guard.sh").write_text(
        "#!/usr/bin/env bash\nnode dsh-edge/manifest.mjs\n", encoding="utf-8"
    )
    assert crg.check_bare_invocations_are_accounted(
        path, catalog_dir=catalog_dir,
        job_state_exempt=frozenset(), infra_exempt=frozenset(),
        debt=frozenset(), allowlist=frozenset(),
    ) == []
    assert crg.check_catalog_handwritten_overlap(path, catalog_dir) != []


def test_bare_steps_in_named_exempts_are_accounted(tmp_path):
    """Три именованных множества — три НАЗВАННЫЕ причины остаться
    рукописным шагом (job-state / актёр / долг-к-миграции); шаг из любого
    из них сверка не красит — иначе именованное исключение красило бы CI на
    каждом прогоне без возможности его снять."""
    steps = [
        {"name": "Квота X", "run": "python scripts/lib/rate_guard.py --job x"},
        {"name": "Инварианты X", "run": "python scripts/orchestra/x.py"},
        {"name": "Автоперенос X", "run": "python scripts/lib/x.py wire repo"},
        {"name": "Долг X", "run": "node dsh-edge/x.mjs"},
    ]
    doc = {"jobs": {"test": {"steps": steps}}}
    path = tmp_path / "repo-ci.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    assert crg.check_bare_invocations_are_accounted(
        path,
        catalog_dir=tmp_path / "нет-такого-каталога",
        job_state_exempt=frozenset({"Квота X", "Инварианты X"}),
        infra_exempt=frozenset({"Автоперенос X"}),
        debt=frozenset({"Долг X"}),
        allowlist=frozenset(),
    ) == []


def test_stale_exemption_is_flagged(tmp_path):
    """Мёртвое исключение красит так же, как мёртвая ALLOWLIST-запись:
    имя есть в множестве, шага в job `test` больше нет."""
    doc = {"jobs": {"test": {"steps": [
        {"name": "Живой шаг", "run": "echo hi"},
    ]}}}
    path = tmp_path / "repo-ci.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    problems = crg.check_bare_invocations_are_accounted(
        path,
        catalog_dir=tmp_path / "нет-такого-каталога",
        job_state_exempt=frozenset({"Мёртвая квота"}),
        infra_exempt=frozenset({"Мёртвый актёр"}),
        debt=frozenset({"Мёртвый долг"}),
        allowlist=frozenset(),
    )
    assert len(problems) == 3
    assert any("ALLOWLIST_JOB_STATE_EXEMPT" in p and "Мёртвая квота" in p for p in problems)
    assert any("INFRA_EXEMPT_STEP_NAMES" in p and "Мёртвый актёр" in p for p in problems)
    assert any("GUARD_MIGRATION_DEBT" in p and "Мёртвый долг" in p for p in problems)


def test_debt_ratchet_flags_growth(tmp_path):
    """Рэтчет долга — «только вниз», тот же приём, что ALLOWLIST_RATCHET_MAX:
    без потолка текст отказа предлагал бы дописать имя в множество вместо
    переноса гвардии в каталог."""
    debt = frozenset({f"Долг {i}" for i in range(crg.GUARD_MIGRATION_DEBT_MAX + 1)})
    doc = {"jobs": {"test": {"steps": [
        {"name": name, "run": "node dsh-edge/x.mjs"} for name in sorted(debt)
    ]}}}
    path = tmp_path / "repo-ci.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    problems = crg.check_bare_invocations_are_accounted(
        path,
        catalog_dir=tmp_path / "нет-такого-каталога",
        job_state_exempt=frozenset(), infra_exempt=frozenset(),
        debt=debt, allowlist=frozenset(),
    )
    assert len(problems) == 1
    assert "GUARD_MIGRATION_DEBT_MAX" in problems[0]


def test_allowlist_bare_form_is_not_double_reported(tmp_path):
    """ALLOWLIST-запись с «голой» формой называется
    check_allowlist_entries_are_migratable (своё сообщение с критерием
    #1069) — сверка по всем шагам её пропускает, двойной находки нет."""
    allowlist = frozenset({"Канарейка X"})
    doc = {"jobs": {"test": {"steps": [
        {"name": "Канарейка X", "run": "node dsh-edge/x.mjs"},
    ]}}}
    path = tmp_path / "repo-ci.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    assert crg.check_bare_invocations_are_accounted(
        path,
        catalog_dir=tmp_path / "нет-такого-каталога",
        job_state_exempt=frozenset(), infra_exempt=frozenset(),
        debt=frozenset(), allowlist=allowlist,
    ) == []
    assert len(crg.check_allowlist_entries_are_migratable(path, allowlist)) == 1


# ── живой репозиторий: учёт полон, пара quota/invariants на месте ────────────

def test_live_repo_ci_bare_steps_are_all_accounted():
    problems = crg.check_bare_invocations_are_accounted()
    assert problems == [], (
        "в job `test` .github/workflows/repo-ci.yml есть «голые» вызовы файлов "
        f"(живые снимки/гвардии), не учтённые ни каталогом, ни именованными "
        f"множествами: {problems}"
    )


def test_live_debt_is_within_ratchet_ceiling():
    assert len(crg.GUARD_MIGRATION_DEBT) <= crg.GUARD_MIGRATION_DEBT_MAX, (
        f"GUARD_MIGRATION_DEBT ({len(crg.GUARD_MIGRATION_DEBT)}) "
        f"превысил потолок ({crg.GUARD_MIGRATION_DEBT_MAX}) — долг "
        "гвардий-к-миграции обязан убывать, не расти"
    )


def test_quota_invariants_job_state_pair_is_present_in_repo_ci():
    """Presence-тест пары ALLOWLIST_JOB_STATE_EXEMPT (#1069, ревью PR #1117):
    квота несёт `id: quota`, шаг инвариантов читает её `outputs.skip` своим
    `if:`. Удаление/переименование любого из двух шагов или обрыв проводки
    `if:` → `id:` красит тест — исключение держится данными repo-ci.yml, а
    не комментарием (тот же приём, что
    test_catalog_perebor_step_itself_is_present_in_repo_ci)."""
    doc = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "repo-ci.yml").read_text(encoding="utf-8")
    )
    steps = doc["jobs"]["test"]["steps"]
    # Имена читаются из того же модуля, что и множество исключений (#1437):
    # повтор строки здесь делал переименование шага двухместной правкой.
    quota = [s for s in steps if s.get("name") == crg.QUOTA_GATE_STEP_NAME]
    invariants = [s for s in steps if s.get("name") == crg.QUOTA_GATED_STEP_NAME]
    assert len(quota) == 1 and quota[0].get("id") == "quota", (
        f"шаг «{crg.QUOTA_GATE_STEP_NAME}» с id: quota "
        "исчез или потерял id — пара ALLOWLIST_JOB_STATE_EXEMPT мертва, "
        "сними запись из множества или верни шаг"
    )
    assert len(invariants) == 1, (
        "шаг «Инварианты состояния репозитория — живой снимок (7 — required)» "
        "исчез или переименован — запись ALLOWLIST_JOB_STATE_EXEMPT мертва"
    )
    assert invariants[0].get("if") == "steps.quota.outputs.skip != 'true'", (
        "шаг инвариантов не читает steps.quota.outputs.skip своим if: — "
        "межшаговая зависимость пары ALLOWLIST_JOB_STATE_EXEMPT оборвана, "
        "основание исключения больше не действует"
    )


def test_step_with_mixed_bare_invocations_reports_only_uncovered(tmp_path):
    """Носитель сверки — СПИСОК всех «голых» вызовов шага, не первый
    попадание (ревью второго агента PR #1117): шаг «учтённая каталогом цель
    + неучтённая цель» при первом-попадании уходил бы мимо сверки целиком.
    Сообщение называет только НЕПОКРЫТЫЕ вызовы; полностью покрытый шаг
    чист."""
    doc = {"jobs": {"test": {"steps": [
        {
            "name": "Смешанный шаг",
            "run": "node dsh-edge/covered.mjs\nnode dsh-edge/uncovered.mjs",
        },
        {
            "name": "Полностью покрытый шаг",
            "run": "node dsh-edge/covered.mjs",
        },
    ]}}}
    path = tmp_path / "repo-ci.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    catalog_dir = tmp_path / "guards"
    catalog_dir.mkdir()
    (catalog_dir / "covered-guard.sh").write_text(
        "#!/usr/bin/env bash\nnode dsh-edge/covered.mjs\n", encoding="utf-8"
    )
    problems = crg.check_bare_invocations_are_accounted(
        path, catalog_dir=catalog_dir,
        job_state_exempt=frozenset(), infra_exempt=frozenset(),
        debt=frozenset(), allowlist=frozenset(),
    )
    assert len(problems) == 1
    assert "Смешанный шаг" in problems[0]
    assert "uncovered.mjs" in problems[0]
    head = problems[0].split("«голый» вызов ")[1].split(" — ")[0]
    assert "dsh-edge/covered.mjs" not in head  # покрытая цель в находке не названа
    assert head == "['dsh-edge/uncovered.mjs']"
    # Мутация: переведи носитель обратно на «первое попадание» — краснеет
    # («Полностью покрытый шаг» и mixed-шаг становятся чистыми мимо сверки).


# ── GUARD_CATALOG_PRESENCE: обёртки, невидимые канарейке (ревью, находка 2) ──

def test_guard_catalog_presence_flags_missing_wrapper(tmp_path):
    """Файл GUARD_CATALOG_PRESENCE обязан лежать в каталоге: его удаление
    канарейка осиротевших тестов не видит (носитель provider-default
    покрыт ещё и из provider-latency-bench.yml) — membership защищает
    только именованное множество."""
    catalog_dir = tmp_path / "guards"
    catalog_dir.mkdir()
    (catalog_dir / "другая-гвардия.sh").write_text("echo hi\n", encoding="utf-8")
    problems = crg.check_guard_catalog_presence(
        catalog_dir, presence=frozenset({"provider-default-guard.sh"})
    )
    assert len(problems) == 1
    assert "provider-default-guard.sh" in problems[0]


def test_stale_presence_entry_is_flagged(tmp_path):
    """Мёртвая запись presence-множества красит так же, как мёртвая
    ALLOWLIST-запись: имя есть в множестве — файла в каталоге нет."""
    catalog_dir = tmp_path / "guards"
    catalog_dir.mkdir()
    problems = crg.check_guard_catalog_presence(
        catalog_dir, presence=frozenset({"снесённая-обёртка-guard.sh"})
    )
    assert len(problems) == 1
    assert "снесённая-обёртка-guard.sh" in problems[0]


def test_live_guard_catalog_presence_is_satisfied():
    assert crg.check_guard_catalog_presence() == [], (
        "обёртки GUARD_CATALOG_PRESENCE исчезли из scripts/ci/guards/ — "
        "их удаление канарейка не увидела бы, локальный прогон молча "
        "потерял бы гвардии"
    )


def test_live_debt_names_are_present_as_steps():
    """Каждая запись GUARD_MIGRATION_DEBT — живое имя шага job `test`
    (парсенная yaml-форма, включая усечённые «… (класс») — иначе учёт
    вел бы в никуда."""
    import yaml as _yaml
    doc = _yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "repo-ci.yml").read_text(encoding="utf-8")
    )
    names = {
        (s.get("name") or "(без имени)") for s in doc["jobs"]["test"]["steps"]
    }
    missing = sorted(crg.GUARD_MIGRATION_DEBT - names)
    assert missing == [], (
        f"GUARD_MIGRATION_DEBT называет шаги, которых нет в repo-ci.yml: {missing}"
    )
