#!/usr/bin/env python3
"""Тесты scripts/lib/orchestra_workflow_lint.py (#887).

Запуск: python -m pytest scripts/lib/test_orchestra_workflow_lint.py -q
"""

import importlib.util
from pathlib import Path

import yaml

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "orchestra_workflow_lint.py"
spec = importlib.util.spec_from_file_location("orchestra_workflow_lint", SCRIPT)
owl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owl)  # type: ignore[union-attr]


def doc_with_steps(steps):
    return {"jobs": {"orchestra": {"steps": steps}}}


def test_step_with_continue_on_error_and_id_is_healthy():
    doc = doc_with_steps([
        {"name": "Гвардия X", "id": "guard_x", "continue-on-error": True, "run": "true"},
    ])
    assert owl.continue_on_error_steps_without_id(doc) == []


def test_step_with_continue_on_error_without_id_is_a_violation():
    doc = doc_with_steps([
        {"name": "Гвардия протухшей метки blocked (#334)", "continue-on-error": True, "run": "true"},
    ])
    assert owl.continue_on_error_steps_without_id(doc) == ["Гвардия протухшей метки blocked (#334)"]


def test_step_without_continue_on_error_never_flagged():
    doc = doc_with_steps([
        {"name": "Обычный шаг", "run": "true"},
    ])
    assert owl.continue_on_error_steps_without_id(doc) == []


def test_unnamed_step_uses_positional_fallback():
    doc = doc_with_steps([
        {"continue-on-error": True, "run": "true"},
    ])
    assert owl.continue_on_error_steps_without_id(doc) == ["шаг #0"]


def test_other_job_is_not_scanned():
    # WATCHED_JOB — только 'orchestra': best_effort_outcome_guard.py читает
    # steps ИМЕННО этого job, не 'contract'.
    doc = {"jobs": {"contract": {"steps": [
        {"name": "Что угодно", "continue-on-error": True, "run": "true"},
    ]}}}
    assert owl.continue_on_error_steps_without_id(doc) == []


def test_empty_doc_is_healthy():
    assert owl.continue_on_error_steps_without_id({}) == []
    assert owl.continue_on_error_steps_without_id(None) == []


# ── Проводка читателя реальных outcome: summary_step_violations (#887) ──


def coe_step():
    return {"name": "Гвардия X", "id": "guard_x",
            "continue-on-error": True, "run": "true"}


def summary_step(**overrides):
    step = {
        "name": "Свод реальных исходов",
        "if": "always()",
        "env": {"STEPS_JSON": "${{ toJSON(steps) }}"},
        "run": "python scripts/orchestra/best_effort_outcome_guard.py",
    }
    step.update(overrides)
    return step


def test_wired_summary_step_after_coe_is_healthy():
    assert owl.summary_step_violations(doc_with_steps([coe_step(), summary_step()])) == []


def test_summary_step_without_coe_steps_is_not_required():
    # Нет continue-on-error шагов — нет и читателя, которого обязаны проводить.
    assert owl.summary_step_violations(doc_with_steps([summary_step()])) == []


def test_missing_summary_step_is_a_violation():
    violations = owl.summary_step_violations(doc_with_steps([coe_step()]))
    assert len(violations) == 1
    assert "нет шага" in violations[0]
    assert "best_effort_outcome_guard.py" in violations[0]


def test_summary_before_last_coe_step_is_a_violation():
    # Контекст steps шага содержит только УЖЕ завершившиеся шаги — свод,
    # стоящий ПЕРЕД последним continue-onerror шагом, часть исходов не увидит.
    doc = doc_with_steps([coe_step(), summary_step(), coe_step()])
    violations = owl.summary_step_violations(doc)
    assert len(violations) == 1
    assert "нет шага" in violations[0]


def test_summary_without_always_is_a_violation():
    doc = doc_with_steps([coe_step(), summary_step(**{"if": "success()"})])
    violations = owl.summary_step_violations(doc)
    assert len(violations) == 1
    assert "always()" in violations[0]


def test_summary_with_wrong_env_is_a_violation():
    doc = doc_with_steps([coe_step(), summary_step(env={"STEPS_JSON": "${{ toJSON(job) }}"})])
    violations = owl.summary_step_violations(doc)
    assert len(violations) == 1
    assert "STEPS_JSON" in violations[0]


def test_summary_without_env_at_all_is_a_violation():
    doc = doc_with_steps([coe_step(), summary_step(env=None)])
    violations = owl.summary_step_violations(doc)
    assert len(violations) == 1
    assert "STEPS_JSON" in violations[0]


def test_summary_env_spacing_is_not_a_violation():
    # Форматирование выражения — не смена смысла: гвардия не краснеет от
    # `${{toJSON(steps)}}` без пробелов.
    doc = doc_with_steps([coe_step(), summary_step(env={"STEPS_JSON": "${{toJSON(steps)}}"})])
    assert owl.summary_step_violations(doc) == []


def test_summary_with_continue_on_error_invalidates_itself():
    # Шаг-свод с continue-on-error сам становится «последним таким шагом»,
    # кандидаты после него исчезают — замаскированный провал читателя снова
    # был бы невидим; линт ловит это как «шаг-свод не найден».
    doc = doc_with_steps([coe_step(), summary_step(id="summary", **{"continue-on-error": True})])
    violations = owl.summary_step_violations(doc)
    assert len(violations) == 1
    assert "нет шага" in violations[0]


def test_job_missing_distinguishes_missing_object_from_healthy():
    # Находка ревью PR #888: переименованный/удалённый job раньше читался
    # как здоровое состояние (пустой steps → []), несимметрично с падением
    # на отсутствующем файле.
    assert owl.job_missing(doc_with_steps([])) is False
    assert owl.job_missing({}) is True
    assert owl.job_missing(None) is True
    assert owl.job_missing({"jobs": {"contract": {"steps": []}}}) is True


def test_live_orchestra_workflow_has_no_violations():
    """Живой прогон на настоящем .github/workflows/orchestra.yml — доказывает,
    что фикс (#887: добавлены id ко всем continue-on-error шагам И проведён
    читатель реальных outcome) реально применён, не только описан в тесте
    на синтетике."""
    doc = yaml.safe_load(owl.ORCHESTRA_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert owl.job_missing(doc) is False
    assert owl.continue_on_error_steps_without_id(doc) == []
    assert owl.summary_step_violations(doc) == []


def test_mutation_guard_missing_id_in_live_file_would_be_caught(tmp_path):
    """Мутация: снимаем id с реального шага живого файла (текстом, не через
    YAML round-trip — сохраняет форматирование) — гвардия обязана покраснеть."""
    text = owl.ORCHESTRA_WORKFLOW_PATH.read_text(encoding="utf-8")
    mutated = text.replace("        id: stale_blocked\n", "", 1)
    assert mutated != text, "фикстура мутации не нашла строку — тест устарел вместе с файлом"
    doc = yaml.safe_load(mutated)
    violations = owl.continue_on_error_steps_without_id(doc)
    assert violations, "снятие id с continue-on-error шага обязано быть найдено"


def test_mutation_guard_removed_summary_env_in_live_file_would_be_caught():
    """Мутация проводки: снимаем env STEPS_JSON со шага-свода живого файла —
    линт обязан потребовать проводку заново (класс #887: сломанный читатель
    не должен отзываться здоровьем уже по исходнику, до живого прогона)."""
    text = owl.ORCHESTRA_WORKFLOW_PATH.read_text(encoding="utf-8")
    mutated = text.replace("          STEPS_JSON: ${{ toJSON(steps) }}\n", "", 1)
    assert mutated != text, "фикстура мутации не нашла строку — тест устарел вместе с файлом"
    violations = owl.summary_step_violations(yaml.safe_load(mutated))
    assert len(violations) == 1
    assert "STEPS_JSON" in violations[0]


def test_mutation_guard_removed_summary_step_in_live_file_would_be_caught():
    """Мутация проводки: шаг-свод удалён целиком (его run больше никто не
    зовёт) — линт обязан red'ить «нет шага, заводящего guard». Дополнительно
    проверяем CLI-уровень: job на месте, так что краснеет именно проверка
    проводки, а не job_missing."""
    text = owl.ORCHESTRA_WORKFLOW_PATH.read_text(encoding="utf-8")
    line = "        run: python scripts/orchestra/best_effort_outcome_guard.py\n"
    assert text.count(line) == 1, "фикстура мутации нашла не одну строку — тест устарел вместе с файлом"
    mutated = "\n".join(
        l for l in text.split("\n") if l != line.rstrip("\n")
    )
    doc = yaml.safe_load(mutated)
    assert owl.job_missing(doc) is False
    violations = owl.summary_step_violations(doc)
    assert len(violations) == 1
    assert "нет шага" in violations[0]


def test_mutation_guard_missing_orchestra_job_would_be_caught():
    """Мутация объекта гвардии: job переименован — main()-проверка обязана
    быть громкой (чистая функция job_missing это ловит; main печатает ::error
    и выходит 1 — проверяется формой текста ошибки рядом в test CLI ниже)."""
    doc = yaml.safe_load(owl.ORCHESTRA_WORKFLOW_PATH.read_text(encoding="utf-8"))
    doc = {"jobs": {k: v for k, v in doc["jobs"].items() if k != owl.WATCHED_JOB}}
    assert owl.job_missing(doc) is True
    assert owl.summary_step_violations(doc) == []  # silent-healthy тут ловит job_missing, не свод


def test_main_missing_job_fails_loud(capsys, monkeypatch):
    doc_path = owl.ORCHESTRA_WORKFLOW_PATH
    fake_doc = {"jobs": {"contract": {"steps": []}}}
    calls = {}
    monkeypatch.setattr(owl.yaml, "safe_load", lambda *_a, **_k: calls.setdefault("doc", fake_doc))
    monkeypatch.setattr(owl, "ORCHESTRA_WORKFLOW_PATH", doc_path)  # путь живой, документ подменён
    assert owl.main() == 1
    out = capsys.readouterr().out
    assert "::error" in out
    assert "объект гвардии исчез" in out
