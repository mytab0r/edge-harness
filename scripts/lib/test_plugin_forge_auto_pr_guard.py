#!/usr/bin/env python3
"""Гвардия #945 (закрывает #661 вариант 1, #664): авто-PR форжа сливаем без человека.

Класс проблемы (issue #661, живые эпизоды): `plugin-forge.yml` открывал
авто-PR из ветки `forge/<id>-v<version>-<ts>`, а контракт «PR↔задача»
(`scripts/lib/task_ref.py::task_from_branch`, решение владельца 2026-09-06)
резолвит номер задачи ТОЛЬКО из имени ветки `agent/<N>-slug` — PR получал
`contract:failed` и не сливался НИКОГДА (runner-bridge 0.1.2: авто-PR #658
CLOSED, вручную #660 MERGED; plugin-manager 0.1.8: #556/#558 CLOSED, вручную
#560 MERGED — 0 из 2 автоматических). Плюс форж запускался только
`workflow_dispatch` руками (21 из 21 прогона истории) и текст авто-PR
(#664) утверждал, что `deploy-dsh-edge.yml` не триггерится на push, хотя
push-триггер по `dsh-edge/**` есть с #374.

Правила, которые эта гвардия делает невозможным нарушить незаметно:

  1. `task_issue` — `required: true` во входе `workflow_dispatch`: без
     номера задачи ветка авто-PR не соберётся по контракту.
  2. Ветка авто-PR строится как `agent/${TASK_ISSUE}-forge-<id>-<ts>`, НЕ
     `forge/<id>-v<version>-<ts>` (класс #658/#556/#558).
  3. Шаг «Валидация входа» отказывает громко при пустом `TASK_ISSUE_INPUT`
     (защита ДАЖЕ если job `prepare` когда-нибудь пропустит пустое значение).
  4. Job `prepare` отказывает громко при `workflow_dispatch` без `task_issue`
     ДО начала 20-минутной сборки, а не тратит CI впустую.
  5. `plugin-forge.yml` слушает `push` по `plugins-src/**` — запуск форжа на
     слияние без человека (мандат #945).
  6. `plugin-forge.yml` несёт `schedule`-страховку (тот же класс, что
     `deploy-dsh-edge.yml`: мерж через оркестратор — `GITHUB_TOKEN`,
     `orchestra.yml`, шаг scheduler — не создаёт push-событие,
     `scripts/orchestra/scheduler.py::dispatch_deploy_on_merge` документирует
     это прямо; push-триггер п.5 НЕ сработает для большинства мержей).
  7. Текст авто-PR (#664) не утверждает, что деплой не триггерится на push —
     `deploy-dsh-edge.yml` имеет push-триггер по `dsh-edge/**` с #374.
  8. push/schedule проверяют пригодность task_issue (issue открыта, метка
     `task`) тем же контрактом, что `contract_check.py::
     task_eligibility_problems`, ДО сборки — иначе задача, закрытая приёмкой
     (`accept_merged_tasks`) быстрее, чем форж успевает собраться (~20-60
     минут), навсегда вешает `contract:failed` на авто-PR, а часовой крон
     плодит новый мёртвый PR каждый час (класс #320/#325, находка ревью
     PR #952, п.1).
  9. push/schedule пропускают элемент состава, если для той же задачи уже
     есть открытый PR форжа (`agent/<N>-forge-*`) — дедуп, не второй мёртвый
     релиз на тот же дрейф (находка ревью PR #952, п.1).

Запуск: python -m pytest scripts/lib/test_plugin_forge_auto_pr_guard.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FORGE = REPO_ROOT / ".github" / "workflows" / "plugin-forge.yml"


def _text() -> str:
    assert FORGE.exists(), (
        f"{FORGE} исчез или переименован — обнови путь в гвардии сознательной "
        "правкой, а не молчаливым обходом"
    )
    return FORGE.read_text(encoding="utf-8")


def test_task_issue_input_is_required():
    """Правило 1."""
    text = _text()
    match = re.search(r"task_issue:\s*\n(?:.*\n)*?\s*required:\s*(\S+)", text)
    assert match, "не нашёл блок входа task_issue в workflow_dispatch.inputs"
    assert match.group(1) == "true", (
        "task_issue обязан быть required: true (#661/#945) — иначе форж можно "
        "запустить без номера задачи и открыть заведомо неслияемый PR"
    )


def test_branch_follows_agent_task_contract():
    """Правило 2."""
    text = _text()
    assert re.search(r'BRANCH="agent/\$\{TASK_ISSUE\}-forge-', text), (
        "авто-PR форжа обязан открываться из ветки "
        "agent/<task_issue>-forge-<id>-<ts> (#661) — только так контракт "
        "PR↔задача резолвит номер задачи"
    )
    assert 'BRANCH="forge/' not in text, (
        "ветка forge/<id>-v<version>-<ts> не проходит контракт PR↔задача "
        "(живые эпизоды: авто-PR #658, #556, #558 — все CLOSED) — не "
        "возвращай этот паттерн"
    )


def test_validate_step_fails_loud_on_missing_task_issue():
    """Правило 3."""
    text = _text()
    assert re.search(
        r'\[\s*-n\s*"\$TASK_ISSUE_INPUT"\s*\]\s*\|\|\s*\{\s*echo\s+"::error::task_issue',
        text,
    ), (
        "шаг «Валидация входа» обязан отказывать громко при пустом "
        "task_issue (#661/#945), а не открывать неслияемый PR"
    )


def test_prepare_job_requires_task_issue_for_dispatch():
    """Правило 4."""
    text = _text()
    assert re.search(
        r'INPUT_TASK_ISSUE"\s*\]\s*\|\|\s*\{\s*echo\s+"::error::task_issue обязателен',
        text,
    ), (
        "job prepare обязан отказывать при workflow_dispatch без task_issue "
        "ДО начала сборки (#661), а не тратить 20+ минут CI впустую"
    )


def test_push_trigger_on_plugins_src():
    """Правило 5."""
    text = _text()
    assert re.search(r"\n  push:\s*\n\s*branches:\s*\[main\]", text), (
        "plugin-forge.yml обязан слушать push (мандат #945: конвейер не "
        "должен требовать человека для запуска форжа)"
    )
    assert '"plugins-src/**"' in text, (
        "push-триггер обязан фильтроваться путями plugins-src/** — иначе "
        "форж будет запускаться на КАЖДЫЙ мерж в main, а не только на "
        "изменения исходников плагинов"
    )


def test_schedule_fallback_present():
    """Правило 6."""
    text = _text()
    assert re.search(r"\n  schedule:\n(?:.*\n)*?\s*-\s*cron:", text), (
        "plugin-forge.yml обязан нести schedule-страховку (тот же класс, "
        "что deploy-dsh-edge.yml): мерж через оркестратор (GITHUB_TOKEN) не "
        "создаёт push-событие — push-триггер (правило 5) НЕ сработает для "
        "большинства мержей plugins-src/** (#946)"
    )


def test_pr_body_deploy_trigger_claim_is_accurate():
    """Правило 7 (#664)."""
    text = _text()
    assert "НЕ триггерится на push" not in text, (
        "#664: deploy-dsh-edge.yml ИМЕЕТ push-триггер по dsh-edge/** (с #374) "
        "— текст авто-PR форжа не должен утверждать обратное"
    )
    assert "ИМЕЕТ push-триггер по `dsh-edge/**`" in text, (
        "#664: текст авто-PR форжа обязан описывать реальный push-триггер "
        "деплоя, а не выдуманное ограничение"
    )


def test_prepare_checks_task_eligibility_before_build():
    """Правило 8 (находка ревью PR #952, п.1)."""
    text = _text()
    assert "task_issue_ready" in text, (
        "prepare обязан проверять пригодность task_issue (issue открыта, "
        "метка task) ДО сборки — без этого push/schedule форжит на задачу, "
        "которую приёмка успела закрыть, и авто-PR вешает contract:failed "
        "навсегда (класс #320/#325)"
    )
    assert re.search(r'\$issue_state"\s*!=\s*"open"', text), (
        "проверка пригодности обязана отвергать закрытую задачу"
    )
    assert re.search(r'index\("task"\)', text), (
        "проверка пригодности обязана требовать метку `task` на задаче "
        "(тот же критерий, что contract_check.py::task_eligibility_problems)"
    )
    assert "task_issue_ready \"$task_issue\" \"push $AFTER_SHA\"" in text, (
        "push-ветка prepare обязана вызывать проверку пригодности перед "
        "тем, как класть элементы состава в матрицу"
    )
    assert 'task_issue_ready "$task_issue" "schedule-дрейф $src_dir" || continue' in text, (
        "schedule-ветка prepare обязана пропускать (continue) конкретный "
        "дрейф при непригодной задаче, не валить весь прогон"
    )


def test_prepare_dedupes_against_open_forge_pr():
    """Правило 9 (находка ревью PR #952, п.1)."""
    text = _text()
    assert re.search(r'grep -c "\^agent/\$\{task_issue\}-forge-"', text), (
        "проверка пригодности обязана искать уже открытый PR форжа этой же "
        "задачи (agent/<N>-forge-*) и пропускать повтор — иначе схема "
        "«крон открывает новый PR каждый час, пока предыдущий не слился» "
        "плодит мёртвые релизы (находка ревью PR #952, п.1)"
    )


# Мутации, которыми доказана гвардия (каждая — красный тест, откат — зелёный):
#
#   М1 (правило 1): заменить `required: true` у task_issue на `required: false`
#     — красен test_task_issue_input_is_required.
#   М2 (правило 2): вернуть `BRANCH="forge/${PLUGIN_ID}-v${PLUGIN_VERSION}-..."`
#     — красен test_branch_follows_agent_task_contract (оба assert внутри).
#   М3 (правило 3): убрать строку с `[ -n "$TASK_ISSUE_INPUT" ] || { echo
#     "::error::task_issue не передан...` из шага «Валидация входа» — красен
#     test_validate_step_fails_loud_on_missing_task_issue.
#   М4 (правило 4): убрать проверку `[ -n "$INPUT_TASK_ISSUE" ] || ...` из
#     job prepare (ветка workflow_dispatch) — красен
#     test_prepare_job_requires_task_issue_for_dispatch.
#   М5 (правило 5): удалить блок `push:` целиком — красен
#     test_push_trigger_on_plugins_src.
#   М6 (правило 6): удалить блок `schedule:` целиком — красен
#     test_schedule_fallback_present.
#   М7 (правило 7): вернуть строку «deploy-dsh-edge.yml НЕ триггерится на
#     push в main» в PR_BODY — красен test_pr_body_deploy_trigger_claim_is_accurate.
#   М8 (правило 8): убрать вызов `task_issue_ready` из push- или
#     schedule-ветки prepare — красен test_prepare_checks_task_eligibility_before_build.
#   М9 (правило 9): убрать блок дедупа (`grep -c "^agent/${task_issue}-forge-"`)
#     из `task_issue_ready` — красен test_prepare_dedupes_against_open_forge_pr.
