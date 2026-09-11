#!/usr/bin/env python3
"""Гвардия #945: деплой обязан доказывать, что плагин runner-bridge реально
работает в чате морды, а не только что RPC-поверхность жива.

Класс проблемы: канарейки ingest-шва (#119) и реестра провайдеров (#378)
доказывают, что RPC dsh-edge отвечает — это НЕ то же самое, что «зарегистри-
рованный плагин отрабатывает при реальном вызове» (issue #100 — молчаливый
provал inject-контракта cordis один раз уже давал именно такой разрыв:
сборка зелёная, тулов нет). Прямого детерминированного RPC «вызови тул X»
у dsh-edge нет (полный список методов UNARY_ROUTES —
docs/research/12-dsh-edge-session-api.md, namespace tools.* отсутствует);
единственный путь — session.prompt (реальный ход, модель решает вызвать тул).

Правила:

  1. Шаг «Канарейка runner-bridge» существует в deploy-dsh-edge.yml.
  2. Канарейка идёт через session.prompt и просит модель вызвать runner_status.
  3. Канарейка НЕ просит модель вызвать runner_task автоматически: запись
     создаёт реальную GitHub issue с меткой task и диспетчит worker.yml —
     на каждый прогон деплоя (push + суточный schedule + ручной dispatch)
     это заспамило бы пул задач фиктивными «канарейками» и жгло минуты
     воркеров (решение #945, задокументировано в самом workflow).
  4. Целевая issue (#2) читается ЖИВЬЁМ у GitHub REST в момент канарейки
     (не хардкод ожидаемой строки) — issue #2 постоянно закрыта правилом
     репозитория «закрытая задача не переоткрывается никогда»
     (scripts/orchestra/scheduler.py::reject_reopened_tasks), стабильная
     цель без завода новой canary-задачи под каждый прогон.
  5. Канарейка падает громко, если за отведённое время не нашла реальный
     ответ инструмента.

Запуск: python -m pytest scripts/lib/test_deploy_runner_bridge_canary_guard.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / ".github" / "workflows" / "deploy-dsh-edge.yml"


def _text() -> str:
    assert DEPLOY.exists(), (
        f"{DEPLOY} исчез или переименован — обнови путь в гвардии "
        "сознательной правкой, а не молчаливым обходом"
    )
    return DEPLOY.read_text(encoding="utf-8")


def test_runner_bridge_canary_step_present():
    """Правило 1."""
    text = _text()
    assert "Канарейка runner-bridge" in text, (
        "деплой обязан нести живую канарейку runner-bridge (#390/#945): "
        "канарейки #119/#378 доказывают только «морда жива», не что "
        "зарегистрированный плагин реально отрабатывает в чате"
    )


def test_canary_invokes_runner_status_via_real_chat_turn():
    """Правило 2."""
    text = _text()
    assert "session.prompt" in text, (
        "канарейка runner-bridge обязана идти через session.prompt — "
        "единственный путь вызова тула (LLM во время реального хода), "
        "прямого RPC «вызови тул» у dsh-edge нет "
        "(docs/research/12-dsh-edge-session-api.md)"
    )
    assert "Вызови сейчас инструмент runner_status" in text, (
        "канарейка обязана просить модель вызвать именно runner_status"
    )


def test_canary_does_not_prompt_runner_task_automatically():
    """Правило 3."""
    text = _text()
    assert "инструмент runner_task" not in text, (
        "канарейка НЕ должна просить модель вызвать runner_task "
        "автоматически на каждом деплое — запись создаёт реальную GitHub "
        "issue с меткой task и диспетчит worker.yml (спам пула задач + "
        "минуты воркеров, решение #945)"
    )


def test_canary_targets_permanently_closed_issue_dynamically():
    """Правило 4."""
    text = _text()
    assert re.search(r"repos/\$\{GITHUB_REPOSITORY\}/issues/2[\"']", text), (
        "канарейка обязана читать заголовок issue #2 ЖИВЬЁМ у GitHub REST "
        "(эталон постоянно закрытой задачи), не хардкодить ожидаемую строку"
    )


def test_canary_fails_loud_on_missing_evidence():
    """Правило 5."""
    text = _text()
    assert re.search(r'\[\s*-n\s*"\$found"\s*\](?:\s*\\)?\s*\n?\s*\|\|\s*\{\s*echo\s+"::error::', text), (
        "канарейка обязана падать громко, если runner_status не отчитался "
        "реальным ответом за отведённое время — не проходить дальше тихо"
    )


# Мутации, которыми доказана гвардия (каждая — красный тест, откат — зелёный):
#
#   М1 (правило 1): переименовать шаг «Канарейка runner-bridge (#390/#945)»
#     во что-то другое — красен test_runner_bridge_canary_step_present.
#   М2 (правило 2): заменить промпт модели на «... вызвать runner_task ...»
#     — красен test_canary_invokes_runner_status_via_real_chat_turn.
#   М3 (правило 3): дописать в промпт «...а затем runner_task...» (буквально
#     «инструмент runner_task») — красен
#     test_canary_does_not_prompt_runner_task_automatically.
#   М4 (правило 4): заменить `issues/2` на хардкод-номер в другом месте
#     (issues/1) — красен test_canary_targets_permanently_closed_issue_dynamically.
#   М5 (правило 5): убрать fail-loud строку `[ -n "$found" ] || { echo
#     "::error::...` — красен test_canary_fails_loud_on_missing_evidence.
