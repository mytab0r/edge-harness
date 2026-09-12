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
  8. Пригодность task_issue (issue открыта, метка `task`) проверяется тем же
     контрактом, что `contract_check.py::task_eligibility_problems`, ДО
     сборки — иначе задача, закрытая приёмкой (`accept_merged_tasks`)
     быстрее, чем форж успевает собраться (~20-60 минут), навсегда вешает
     `contract:failed` на авто-PR, а часовой крон плодит новый мёртвый PR
     каждый час (класс #320/#325, находка ревью PR #952, п.1).
  9. Причина отказа машинно-читаема (`SKIP_REASON`): по ней вызывающая
     ветка различает «задача непригодна» от «уже есть открытый PR того же
     дрейфа» — у них РАЗНЫЙ исход (находка ревью PR #952, п.2).
 10. schedule-страховка валит prepare ГРОМКО на найденном, но ничейном
     дрейфе (задача закрыта/непригодна, сбой gh): молча зелёный прогон там
     ровно в штатном случае крона (задача закрыта ~15 мин после мержа,
     сборка 20-60 мин) прятал бы зависший навсегда дрейф; тихий скип —
     ТОЛЬКО дедуп (`SKIP_REASON=duplicate-pr`) (находка ревью PR #952, п.2).
 11. Дедуп по открытым PR не принимает сбой gh за «дублей нет»: код возврата
     `gh pr list` разбирается (`SKIP_REASON=pr-list-failed`), `--limit`
     задан явно (дефолт gh — 30) (находка ревью PR #952, чеклист).
 12. `workflow_dispatch` делает дешёвый pre-flight пригодности задачи до
     сборки — ручной диспатч с закрытым номером не тратит 20-60 минут
     раннера на мёртвый PR (находка ревью PR #952, чеклист).

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
    assert "task_issue_usable" in text, (
        "prepare обязан проверять пригодность task_issue (issue открыта, "
        "метка task) ДО сборки — без этого триггеры форжат на задачу, "
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
    assert 'auto_forge_allowed "$task_issue" "push $AFTER_SHA"' in text, (
        "push-ветка prepare обязана проверять пригодность задачи и дедуп "
        "(auto_forge_allowed) перед тем, как класть элементы состава в "
        "матрицу"
    )
    assert 'auto_forge_allowed "$task_issue" "schedule-дрейф $src_dir"' in text, (
        "schedule-ветка prepare обязана проверять пригодность задачи и дедуп "
        "(auto_forge_allowed), прежде чем форжить найденный дрейф"
    )


def test_skip_reason_is_machine_readable():
    """Правило 9 (находка ревью PR #952, п.2): причина отказа в SKIP_REASON,
    не только в человекочитаемом ::error — вызывающая ветка различает по ней
    «задача непригодна» (громко) от «дедуп» (тихий скип)."""
    text = _text()
    assert "SKIP_REASON=" in text, (
        "проверки пригодности/дедупа обязаны писать машинно-читаемую причину "
        "в SKIP_REASON — без неё вызывающая ветка не различит «задача "
        "непригодна» от «уже есть открытый PR», а у них разный исход"
    )
    assert 'SKIP_REASON=""' in text, (
        "SKIP_REASON обязан сбрасываться на входе проверки — иначе в него "
        "попадает причина ПРЕДЫДУЩЕЙ задачи и следующая ветка решает по "
        "чужой причине"
    )
    for reason in ("issue-unreadable", "not-an-issue", "task-closed",
                   "no-task-label", "pr-list-failed", "duplicate-pr"):
        assert f'SKIP_REASON="{reason}"' in text, (
            f"причина «{reason}» обязана иметь своё значение SKIP_REASON — "
            "это носитель различения исходов для вызывающих веток"
        )


def test_schedule_fails_loud_on_ownerless_drift():
    """Правило 10 (находка ревью PR #952, п.2, блокирующая): в
    schedule-ветке тихий `continue` допустим ТОЛЬКО для дедупа
    (SKIP_REASON=duplicate-pr); любой другой отказ на найденном дрейфе —
    громкое падение prepare. Молча зелёный прогон в её штатном случае
    (задача закрыта приёмкой к моменту крона) прятал бы зависший навсегда
    дрейф."""
    text = _text()
    branch = _schedule_branch(text)
    assert 'if [ "$SKIP_REASON" = "duplicate-pr" ]; then' in branch, (
        "schedule обязана пропускать дрейф молча ТОЛЬКО по явному признаку "
        "дедупа (SKIP_REASON=duplicate-pr) — не по любому отказу проверки"
    )
    assert re.search(
        r'::error::schedule-дрейф .*(не форжится|владельца нет).*', branch
    ) and "exit 1" in branch, (
        "найденный, но ничейный дрейф (закрытая/непригодная задача, сбой "
        "gh) обязан валить prepare громко (::error:: + exit 1) с названным "
        "газом (ручной форж с явным task_issue) — молча зелёный прогон "
        "прячет зависший дрейф (находка ревью PR #952, п.2)"
    )


def test_dedup_checks_gh_exit_code_and_limit():
    """Правило 11 (находка ревью PR #952, чеклист): сбой `gh pr list` — не
    «дублей нет»; лимит задан явно, дефолт gh (30) молча видел бы только
    первые 30 открытых PR."""
    text = _text()
    assert re.search(
        r"gh pr list .*--limit\s+\d+", text
    ), "gh pr list обязан нести явный --limit — дефолт 30 искажает дедуп"
    assert 'SKIP_REASON="pr-list-failed"' in text, (
        "сбой gh pr list обязан попадать в SKIP_REASON=pr-list-failed, а не "
        "молча считаться «дублей нет»"
    )
    assert "grep -c" not in text, (
        "дедуп не должен считать совпадения через `grep -c … || true` — "
        "это маскирует код возврата gh за «пустой список»"
    )


def test_dispatch_preflights_task_eligibility():
    """Правило 12 (находка ревью PR #952, чеклист): ручной диспатч проверяет
    пригодность задачи ДО сборки — дедуп при этом сознательно не проверяется
    (человек, назвавший задачу явно, может форжить второй плагин под ту же
    задачу)."""
    text = _text()
    assert re.search(
        r'task_issue_usable "\$INPUT_TASK_ISSUE" "workflow_dispatch"\s*\|\|\s*exit 1',
        text,
    ), (
        "workflow_dispatch обязан отказывать громко ДО сборки на "
        "непригодной задаче (task_issue_usable … || exit 1) — иначе "
        "20-60 минут раннера сгорают на мёртвом contract:failed PR"
    )


def _schedule_branch(text: str) -> str:
    """Тело `schedule)` case-ветки job'а prepare — носитель правила 10."""
    match = re.search(r"\n            schedule\)\n(.*?)\n              ;;", text, re.S)
    assert match, (
        "case-ветка schedule) в job prepare исчезла или переименована — "
        "обнови носитель гвардии сознательной правкой, а не обходом"
    )
    return match.group(1)


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
#   М8 (правило 8): убрать проверку `[ "$issue_state" != "open" ]` из
#     `task_issue_usable` или вызов auto_forge_allowed из push/schedule —
#     красен test_prepare_checks_task_eligibility_before_build.
#   М9 (правило 9): стереть присваивания SKIP_REASON (оставить голые return 1)
#     — красен test_skip_reason_is_machine_readable.
#   М10 (правило 10): в schedule-ветке заменить
#     `[ "$SKIP_REASON" = "duplicate-pr" ]` на `[ "$SKIP_REASON" = "never" ]`
#     (любой отказ снова скипается тихо) или убрать `exit 1` — красен
#     test_schedule_fails_loud_on_ownerless_drift.
#   М11 (правило 11): вернуть `grep -c "^agent/${task_issue}-forge-" || true`
#     без --limit и без разбора кода возврата — красен
#     test_dedup_checks_gh_exit_code_and_limit.
#   М12 (правило 12): убрать pre-flight `task_issue_usable … || exit 1` из
#     ветки workflow_dispatch — красен test_dispatch_preflights_task_eligibility.
