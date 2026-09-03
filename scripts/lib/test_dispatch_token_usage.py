#!/usr/bin/env python3
"""Гвардия разделения GitHub-токенов (задача #6, ADR 0008).

Класс: репо-секрет GH_DISPATCH_TOKEN — узкий fine-grained PAT (Contents+Actions
на этот репозиторий), живущий в интернет-морде. Исторически в нём лежал широкий
личный PAT, и на него же молча подсели воркер, оркестратор, замер задержки и
runner-bridge — сузили токен, сломали конвейер. Разделение обязано быть
заметным: регресс в `secrets.GH_DISPATCH_TOKEN` вне deploy-пути красит CI, а не
молча возвращает широкий PAT туда, откуда его вывели.

Правила:
  1. `secrets.GH_DISPATCH_TOKEN` в .github/workflows/ (в любой форме записи —
     dot и bracket; файлы .yml И .yaml) упоминает ТОЛЬКО deploy-worker.yml
     (единственный потребитель узкого токена: синхронизация значения в секрет
     воркера).
  2. deploy-worker.yml обязан сохранять саму синхронизацию
     (`wrangler secret put GH_DISPATCH_TOKEN`) — иначе узкий токен не доедет
     до воркера, и морда молча останется на старом значении.
  3. Бывшие широкие потребители (worker, orchestra, deploy-dsh-edge,
     dispatch-latency-probe) обязаны читать `secrets.GH_PIPELINE_PAT` —
     исчезновение источника PAT замечается здесь, а не 401 на первом пуше.

Запуск: python -m pytest scripts/lib/test_dispatch_token_usage.py -q
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# GitHub Actions грузит workflows из .yml И .yaml — гвардия обязана видеть оба
# (находка AI-ревью #146: файл evil.yaml со скваттером проходил все тесты зелёно).
DISPATCH_SECRET_RE = r"secrets[.\[]\s*['\"]?GH_DISPATCH_TOKEN\b"
PIPELINE_SECRET = "secrets.GH_PIPELINE_PAT"

# Единственный законный потребитель узкого dispatch-токена.
DISPATCH_CONSUMER = "deploy-worker.yml"

# Бывшие скваттеры широкого PAT: их миграция на GH_PIPELINE_PAT — часть задачи #6.
PIPELINE_CONSUMERS = [
    "deploy-dsh-edge.yml",
    "dispatch-latency-probe.yml",
    "orchestra.yml",
    "worker.yml",
]

# Фиксированный список workflows: появление/переименование файла — сознательная
# правка этого списка, а не тихий обход гвардии (находка AI-ревью #146: glob сам
# по себе ничего не фиксирует). Содержимое сканируется динамически в правилах ниже.
EXPECTED_WORKFLOWS = frozenset({
    "ai-review.yml",
    "codeql.yml",
    "deploy-dsh-edge.yml",
    "deploy-worker.yml",
    "dispatch-latency-probe.yml",
    "hands.yml",
    "orchestra.yml",
    "pr-review.yml",
    "repo-ci.yml",
    "worker.yml",
})

ALL_WORKFLOWS = sorted(
    path.name for path in WORKFLOWS.iterdir()
    if path.suffix in {".yml", ".yaml"}
)


def workflow_text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_workflow_set_is_pinned():
    """Новый/переименованный workflow требует правки EXPECTED_WORKFLOWS — и осознания,
    что файлы с секретами попадают под правила ниже (список зафиксирован, не glob)."""
    assert set(ALL_WORKFLOWS) == EXPECTED_WORKFLOWS, (
        f"набор workflows изменился: {set(EXPECTED_WORKFLOWS) ^ set(ALL_WORKFLOWS)} — "
        "внеси файл в EXPECTED_WORKFLOWS сознательно (или обнови список)"
    )
    assert DISPATCH_CONSUMER in EXPECTED_WORKFLOWS
    assert set(PIPELINE_CONSUMERS) <= EXPECTED_WORKFLOWS


def test_dispatch_token_secret_used_only_by_deploy_worker():
    """Узкий токен живёт только в deploy-пути морды (правило 1).

    Матчинг — regex: `secrets.GH_DISPATCH_TOKEN` и bracket-формы
    `secrets['GH_DISPATCH_TOKEN']` — один класс, оба — скваттерство
    (обходы из ревью #146 / задачи #151)."""
    squatters = [
        name
        for name in ALL_WORKFLOWS
        if name != DISPATCH_CONSUMER and re.search(DISPATCH_SECRET_RE, workflow_text(name))
    ]
    assert not squatters, (
        f"secrets.GH_DISPATCH_TOKEN (в любой форме записи) вне {DISPATCH_CONSUMER}: "
        f"{squatters} — это узкий fine-grained PAT морды (задача #6, ADR 0008); "
        "широким потребителям нужен secrets.GH_PIPELINE_PAT"
    )


def test_deploy_worker_still_syncs_dispatch_token():
    """Деплой обязан доставлять узкий токен в секрет воркера (правило 2)."""
    text = workflow_text(DISPATCH_CONSUMER)
    assert re.search(DISPATCH_SECRET_RE, text), (
        f"{DISPATCH_CONSUMER} больше не читает secrets.GH_DISPATCH_TOKEN — синхронизация "
        "секрета воркера потеряна, морда застрянет на старом значении"
    )
    assert "wrangler secret put GH_DISPATCH_TOKEN" in text, (
        f"{DISPATCH_CONSUMER} не делает wrangler secret put GH_DISPATCH_TOKEN — "
        "переустановка секрета воркера при деплое потеряна (воркер мог быть "
        "удалён из CF вместе с секретами)"
    )


def test_pipeline_consumers_read_pipeline_pat():
    """Бывшие широкие потребители читают GH_PIPELINE_PAT (правило 3)."""
    missing = [
        name
        for name in PIPELINE_CONSUMERS
        if PIPELINE_SECRET not in workflow_text(name)
    ]
    assert not missing, (
        f"workflows без источника {PIPELINE_SECRET}: {missing} — широкий PAT "
        "потерян, пуш веток/PR/метки/диспетч воркера упадут на первом же вызове"
    )


# ── Права токена аренды (#121, находки AI-ревью #246) ────────────────────────────
# Класс: smoke не моделирует модель прав GitHub, поэтому дыры в правах hands.yml
# зелёные в тестах и видны только в проде. Гвардия по тексту workflow:
#   1. issues: write — claim() после замка назначает исполнителя и ставит след
#      в задаче (_visibility): без этого оба вызова 403, аренда невидима
#      человеку («кто держит» — требование #121), при зелёном job'е.
#   2. persist-credentials: false — checkout по умолчанию пишет github.token в
#      .git/config воркспейса, а DSH работает именно в корне чекаута: без этого
#      агент получает токен с contents:write (инвариант «прав на пуш у агента
#      нет» из ADR 0006 становится ложью; та же гвардия, что в worker.yml).


def test_hands_lease_visibility_needs_issues_write():
    text = workflow_text("hands.yml")
    assert re.search(r"^\s*issues:\s*write\s*$", text, re.M), (
        "hands.yml: нет issues: write — назначение и след аренды (#121) получат "
        "403, аренда станет невидимой в задаче при зелёном job'е"
    )


def test_hands_checkout_does_not_persist_token():
    # Проверка ПО СТРУКТУРЕ YAML, не подстрокой: упоминание в комментарии
    # («persist-credentials: false ниже») не должно закрывать гвардию —
    # поймано мутацией этой же гвардии (#246).
    doc = yaml.safe_load(workflow_text("hands.yml"))
    checkouts = [
        step
        for job in (doc.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
        if str(step.get("uses") or "").startswith("actions/checkout")
    ]
    assert checkouts, "hands.yml: не найден ни один checkout — структура workflow изменилась, обнови гвардию"
    for step in checkouts:
        assert step.get("with", {}).get("persist-credentials") is False, (
            "hands.yml: checkout оставляет github.token в .git/config воркспейса — "
            "DSH прочитает токен с contents:write и получит пуш (ADR 0006 «Права»)"
        )
