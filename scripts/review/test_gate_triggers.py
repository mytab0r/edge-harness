#!/usr/bin/env python3
"""Гвардия триггерной проводки обоих гейтов ревью (#208).

Баг #208 (живой прогон 2026-09-02, PR #177): ботовский update-branch
(`scheduler.merge_queue`, PR behind) пушил merge-коммит в ветку, `synchronize`
перезапускал `check_pr.py`, тот снимал только что поставленную `ai:ok` — и
второй гейт НЕ перезапускался вовсе: `ai-review.yml` был тогда
workflow_dispatch-only. Вердикт восстанавливался только ручным диспатчем
владельца; на PR это выглядело как «ai-review не сработал».

Класс закрыт (решение владельца в #208 — вариант (б), внедрён PR #138, плюс
отпечаток диффа #252/#294) связкой двух триггеров:

  1. `pr-review.yml` триггерится на `pull_request: synchronize` — ботовский
     пуш перезапускает первый гейт. Без этого звена пуш менял бы head молча:
     не было бы ни сверки отпечатка (ai:ok при неизменном диффе сохраняется),
     ни честного сброса при изменившемся — протухший вердикт со старого head
     открывал бы слияние непроверенного дерева (зеркальный silent-wrong).
  2. `ai-review.yml` триггерится на `workflow_run` завершения `pr-review` —
     ЕДИНСТВЕННЫЙ автоматический перезапуск второго гейта. Живая сработка уже
     после закрытия класса (PR #248, 2026-09-06): ai:ok сброшен ботовским
     update-branch в 03:44:48 (сброс честный — merge main изменил блоб
     пересекающегося файла `.github/workflows/repo-ci.yml`, отпечаток диффа
     разошёлся), прогон первого гейта завершился в 03:44:54, автоперезапуск
     второго стартовал в 03:44:55 — через секунду, без ручного диспатча.

  3. Автозапуск по `workflow_run` обязан требовать `conclusion == 'success'`:
     прогон первого гейта с находками (exit 1, conclusion=failure) не должен
     жечь дорогое ревью — оно начнётся, когда автор доработает и первый гейт
     позеленеет.

Ни один из этих блоков не читается ни скриптами, ни другими тестами — их
читает только GitHub. Снять или переименовать любой — и класс #208
возвращается молча: все юнит-тесты зелёные, метки ведут себя правильно, а
восстанавливать второй гейт больше нечему. Эти три теста краснеют при правке
триггеров, а не при первом живом инциденте (AGENTS.md: «закрыл случай —
закрой класс… оставь тест-гвардию и докажи её мутацией»).

Инцидент оставляет инвариант, а не только гвардию проводки. Условие, при
котором случай #177 невозможен: «у открытого PR с review:ok и без ai:*-
вердикта перезапуск второго гейта происходит сам, без ручного диспатча».
Проверка состояния (медленная сеть) уже существует — инвариант 3
`check_stuck_review_gate` в scripts/orchestra/repo_invariants.py: review:ok
дольше UNHEALTHY_PR_AFTER_MINUTES без единой ai:*-метки, газ — авто-повтор
scheduler.trigger_ai_review (#196) плюс эскалация пульса оркестратора.
Эта гвардия — быстрая сеть того же класса: ломает красным repo-ci на первом
же пуш/PR после правки триггеров, за минуты до того, как застрявшее
состояние накопится до порога инварианта.

Тест кормится прод-формой: yaml.safe_load РЕАЛЬНЫХ workflow-файлов, без
пересказа (тот же приём, что test_ai_review_gate.py для шага facts).

Запуск: python -m pytest scripts/review/test_gate_triggers.py -q
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
AI_REVIEW = ROOT / ".github" / "workflows" / "ai-review.yml"
PR_REVIEW = ROOT / ".github" / "workflows" / "pr-review.yml"


def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict, path: Path) -> dict:
    """Блок `on:` разобранного workflow. PyYAML (YAML 1.1) парсит голый ключ
    `on` как булево True — прод-файлы пишут его без кавычек, поэтому честно
    принимаем оба вида и не требуем переписывания workflow ради теста."""
    triggers = doc.get("on", doc.get(True))
    assert isinstance(triggers, dict), (
        f"{path}: нет блока on: — триггеры гейта не читаются, "
        "проводка второго гейта не доказуема")
    return triggers


def test_pr_review_reruns_first_gate_on_synchronize():
    # Звено 1: ботовский пуш (update-branch оркестратора, #252) обязан
    # перезапускать первый гейт — без synchronize проверка нового head не
    # стартует вовсе, и вердикты старого head остаются на месте.
    doc = _doc(PR_REVIEW)
    pr_trigger = _triggers(doc, PR_REVIEW).get("pull_request")
    assert isinstance(pr_trigger, dict), "pr-review.yml: нет триггера pull_request"
    types = pr_trigger.get("types") or []
    assert "synchronize" in types, (
        f"pr-review.yml: pull_request.types {types} без synchronize — пуш в ветку "
        "(включая ботовский update-branch, #208) больше не перезапускает первый гейт")


def test_ai_review_restarts_second_gate_on_pr_review_completion():
    # Звено 2 — сам фикс класса #208 (вариант (б), PR #138): завершение прогона
    # pr-review — единственный событийный перезапуск второго гейта. Именно оно
    # восстанавливает ai:ok после честного сброса на ботовском update-branch
    # (живая сработка: PR #248, сброс 03:44:48 → автоперезапуск 03:44:55).
    doc = _doc(AI_REVIEW)
    run_trigger = _triggers(doc, AI_REVIEW).get("workflow_run")
    assert run_trigger, (
        "ai-review.yml: нет триггера workflow_run — второму гейту некому "
        "перезапуститься после сброса ai:ok (класс #208 возвращается)")
    # GitHub принимает обе формы записи, прод-файл использует одну из них:
    # отображение с одним набором workflows/types (наш случай) или список таких
    # отображений. Нормализуем к списку, чтобы гвардия не зависела от формы.
    entries = run_trigger if isinstance(run_trigger, list) else [run_trigger]
    entry = next(
        (t for t in entries
         if t.get("workflows") == ["pr-review"] and "completed" in (t.get("types") or [])),
        None,
    )
    assert entry is not None, (
        "ai-review.yml: workflow_run не слушает завершение pr-review "
        "(workflows == ['pr-review'], types содержит 'completed') — сброшенный "
        "ai:ok больше не восстанавливается автоматически (#208)")


def test_ai_review_workflow_run_name_matches_pr_review_workflow_name():
    # Находка ревью #442: GitHub матчит workflow_run.workflows по значению
    # `name:` ПОРОДИВШЕГО workflow (pr-review.yml), не по имени файла — тест
    # выше сравнивал литерал ai-review.yml с жёстко зашитой строкой "pr-review"
    # с ОБЕИХ сторон и оставался зелёным, даже если реальный `name:` в
    # pr-review.yml переименован (звено 2 при этом молча умирает: GitHub
    # больше не находит workflow_run с этим именем). Читаем `name:` из уже
    # разобранного pr-review.yml — единственное место правды (файл первого
    # гейта), не второй литерал здесь.
    pr_review_doc = _doc(PR_REVIEW)
    pr_review_name = pr_review_doc.get("name")
    assert isinstance(pr_review_name, str) and pr_review_name, (
        "pr-review.yml: нет name: — workflow_run.workflows в ai-review.yml "
        "матчится GitHub'ом по этому полю, без него звено 2 не проверяемо")

    ai_review_doc = _doc(AI_REVIEW)
    run_trigger = _triggers(ai_review_doc, AI_REVIEW).get("workflow_run")
    entries = run_trigger if isinstance(run_trigger, list) else [run_trigger]
    matching = [t for t in entries if pr_review_name in (t.get("workflows") or [])]
    assert matching, (
        f"ai-review.yml: workflow_run.workflows не содержит {pr_review_name!r} "
        f"(реальный name: pr-review.yml) — GitHub сверяет по этому полю, "
        "переименование pr-review.yml молча рвёт автоперезапуск второго "
        "гейта (#208), даже если строковый литерал здесь совпадёт сам с собой")


def test_ai_review_auto_start_requires_successful_pr_review():
    # Звено 3 (fail-closed в другую сторону): автозапуск дорогого второго
    # гейта — только после УСПЕШНОГО прогона первого. Прогон с находками
    # завершается exit 1 (conclusion=failure, review:changes-requested) и
    # дорогой прогон не жжёт: ревью придёт, когда первый гейт позеленеет.
    doc = _doc(AI_REVIEW)
    condition = (doc.get("jobs") or {}).get("review", {}).get("if") or ""
    assert "github.event_name == 'workflow_run'" in condition, (
        f"ai-review.yml: job review.if не различает workflow_run: {condition!r}")
    assert "conclusion == 'success'" in condition, (
        f"ai-review.yml: job review.if требует success породившего прогона, "
        f"иначе второй гейт стартует и на проваленном первом: {condition!r}")
