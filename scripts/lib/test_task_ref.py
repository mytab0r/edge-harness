#!/usr/bin/env python3
"""Тесты извлечения номера задачи из текста (scripts/lib/task_ref.py, #187, #195).

Класс #187: `f"#{n}" in text` / `line.split("#")` матчат подстрокой — `#18`
совпадает с `#180`, `#181`, `#5180`. Живой прогон orchestra 33570081734:
контракт спутал PR #185 (задача #182) с задачей #18.

Класс #195/#259 (резолвер «PR → задача»): любое упоминание номера в прозе
тела PR — не то же самое, что «эта задача принадлежит PR». Решение владельца
2026-09-06 (#394, второй заход после `pr_task_candidates`): единственный
источник — имя agent-ветки, тело PR для этого вопроса не читается вовсе
(ни первой строкой, ни любым другим способом). Историю декларации первой
строкой тела (#251, #312, `declared_tasks`/`declares_task`, снятых вместе с
этим решением) см. в git-истории task_ref.py и
openspec/changes/contract-task-from-branch/proposal.md.

Кейсы кормятся прод-формой тела PR, которая реально встречается в
репозитории.

Запуск: python -m pytest scripts/lib/test_task_ref.py -q
"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).with_name("task_ref.py")
spec = importlib.util.spec_from_file_location("task_ref", SCRIPT)
task_ref = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_ref)  # type: ignore[union-attr]


def test_no_false_positive_on_longer_number_suffix():
    # #18 не должен матчить #180, #181, #184, #185 (сам баг 33570081734).
    assert task_ref.references_task("Открыт PR #180", 18) is False
    assert task_ref.references_task("Открыт PR #181", 18) is False
    assert task_ref.references_task("Открыт PR #182", 18) is False
    assert task_ref.references_task("Открыт PR #184", 18) is False
    assert task_ref.references_task("Открыт PR #185", 18) is False


def test_no_false_positive_on_longer_number_prefix():
    # #18 не должен матчить #5180 (граница слева — не только справа).
    assert task_ref.references_task("см. задачу #5180", 18) is False
    assert task_ref.references_task("#518", 18) is False


def test_true_positive_various_positions():
    assert task_ref.references_task("#18", 18) is True
    assert task_ref.references_task("Закрывает #18 в этом PR", 18) is True
    assert task_ref.references_task("см. #18, #182 и #185", 18) is True
    assert task_ref.references_task("(#18)", 18) is True
    assert task_ref.references_task("#18,#182", 18) is True


def test_extract_task_refs_multiple_with_boundaries():
    text = "Сначала #18, потом #180 и снова #18 рядом с #5180."
    assert task_ref.extract_task_refs(text) == [18, 180, 18, 5180]


def test_extract_task_refs_empty_text():
    assert task_ref.extract_task_refs("") == []
    assert task_ref.extract_task_refs(None) == []


def test_real_pr_body_form():
    # Прод-форма тела PR: первая строка ровно "#<N>", дальше пояснительный
    # текст с другими номерами — это упоминания (references_task), не
    # источник резолвера «PR → задача» (тот теперь читает только ветку).
    body = (
        "#18\n\n"
        "Второй гейт ревью (AI). Связано с #180, #181, #185 — но задача одна: #18.\n"
    )
    assert task_ref.references_task(body, 18) is True
    assert task_ref.extract_task_refs(body) == [18, 180, 181, 185, 18]


def test_real_pr_body_no_relation_regression():
    # Регресс из 33570081734: PR #185 про задачу #182, контракт для #18 не
    # должен видеть его как конкурента.
    body = "#182\n\nАрхив сессий раннера падает 403 (см. #174).\n"
    assert task_ref.references_task(body, 18) is False
    assert task_ref.references_task(body, 182) is True


# ── Резолвер «PR → задача» (#259, #394) — прод-форма реальных PR ────────────
#
# Живой замер, который и породил задачу: ai_review.py:353 брал ЛЮБОЕ #N из
# прозы тела, сортировал по возрастанию и судил PR по первому попавшемуся
# открытому issue с меткой task — #253 судили по #120 (упомянут в прозе,
# «issue #120 + Telegram»), хотя ветка объявляет #227. Решение владельца
# 2026-09-06: единственный источник — имя ветки; тело не читается вовсе,
# даже как запасной путь.

_PR_253_BODY = (
    "#227\n\n"
    "## Что сделано\n\n"
    "Стадия приёмки: слитый PR больше не оставляет задачу висеть с "
    "комментарием-напоминанием «исполнителю», которого к тому моменту уже "
    "нет (job воркера завершился).\n\n"
    "3. Три исхода, ни один не тихий: … проверка улики сама сломана "
    "(сеть/секрет/API — не «улики нет», а «возможность сломана») → "
    "эскалация владельцу (issue #120 + Telegram), задача не тронута.\n\n"
    "Прод-форма: фикстуры — реальные тела/списки файлов/check-runs PR #138, "
    "#177, #163 и реальные записи задач #18, #21, #78.\n"
)

_PR_253_PULL = {
    "number": 253,
    "head": {"ref": "agent/227-acceptance-stage-after-merge"},
    "body": _PR_253_BODY,
}

# Тело dependabot-PR #282: в тексте много #N (номера чужих PR из changelog
# pnpm/action-setup — #175, #186, #283…), ветка не agent/ — задачи нет
# вовсе, это ожидаемый, а не ошибочный ответ.
_PR_282_PULL = {
    "number": 282,
    "head": {"ref": "dependabot/github_actions/pnpm/action-setup-6"},
    "body": (
        "Bumps pnpm/action-setup from 4 to 6.\n\n"
        "- fix: update pnpm to v11.19.0 (#283)\n"
        "- docs: Update README (#273)\n"
        "- Additional commits viewable in compare view (#175, #186, #199)\n"
    ),
}

# Живой случай #388 (реворк): ветка называет уже ЗАКРЫТУЮ задачу #256, тело
# первой строкой объявляет открытую-преемницу #391. До решения владельца
# 2026-09-06 резолвер брал бы #391 из тела (`pr_task_candidates`) — теперь
# тело не читается вовсе, и резолвер обязан вернуть #256 (задачу ветки), а
# не переориентироваться неявно.
_PR_388_PULL = {
    "number": 388,
    "head": {"ref": "agent/256-task-rework-loop"},
    "body": (
        "#391\n\n"
        "Related: #256 (закрыта акцептансом 2026-09-05 как «без наблюдаемого "
        "результата»; правило: закрытая задача не переоткрывается, новая "
        "узкая #391 по фактическому содержимому).\n"
    ),
}


def test_resolve_pr_task_prefers_branch_over_prose_mention():
    # Живой случай #253: ветка #227, в прозе #120 — резолвер обязан вернуть
    # 227, не 120 (класс #259, второй экземпляр #187/#195: первое попавшееся
    # число в тексте вместо реального источника).
    assert task_ref.resolve_pr_task(_PR_253_PULL) == 227


def test_resolve_pr_task_bot_pr_has_no_task():
    # dependabot: ветка не agent/ — «задачи нет», не 283/273.
    assert task_ref.resolve_pr_task(_PR_282_PULL) is None


def test_resolve_pr_task_ignores_body_without_agent_branch():
    # Ручной PR без agent-ветки — решение владельца 2026-09-06: тело больше
    # не запасной путь, ответ «задачи нет», даже если первая строка тела
    # называет номер.
    pull = {"head": {"ref": "fix/typo"}, "body": "#42\n\nОпечатка в доке."}
    assert task_ref.resolve_pr_task(pull) is None


def test_resolve_pr_task_does_not_reorient_via_body_when_branch_task_closed():
    # Живой случай #388: ветка называет закрытую #256, тело объявляет
    # открытую #391 — резолвер обязан вернуть 256 (задачу ветки), не 391.
    # Переориентация чтением тела отменена без исключений.
    assert task_ref.resolve_pr_task(_PR_388_PULL) == 256


def test_task_from_branch_matches_agent_convention_only():
    assert task_ref.task_from_branch("agent/227-acceptance-stage-after-merge") == 227
    assert task_ref.task_from_branch("agent/259-pr-task-resolver") == 259
    assert task_ref.task_from_branch("dependabot/github_actions/foo-1") is None
    assert task_ref.task_from_branch("fix/typo") is None
    assert task_ref.task_from_branch("") is None


# Мутация, которой доказан resolve_pr_task (#259, #394): временно замени тело
# функции на `refs = sorted(set(extract_task_refs(pull.get("body") or "")));
# return refs[0] if refs else None` (старая широкая семантика ai_review.py:353)
# — extract_task_refs(_PR_253_BODY) отдаёт {18, 21, 78, 120, 138, 163, 177,
# 227}, наименьший 18 — test_resolve_pr_task_prefers_branch_over_prose_mention
# краснеет с `AssertionError: assert 18 == 227`. Отдельно: верни чтение тела
# (`declared = extract_task_refs(...); return from_branch or (declared[0] if
# declared else None)`) — test_resolve_pr_task_ignores_body_without_agent_branch
# и test_resolve_pr_task_does_not_reorient_via_body_when_branch_task_closed
# краснеют (вернётся 42/391 вместо None/256). Верни чистую `task_from_branch` —
# все снова зелёные.
