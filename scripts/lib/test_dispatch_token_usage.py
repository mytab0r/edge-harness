#!/usr/bin/env python3
"""Гвардия разделения GitHub-токенов (задача #6, ADR 0008).

Класс: репо-секрет GH_DISPATCH_TOKEN — узкий fine-grained PAT (Contents+Actions
на этот репозиторий), живущий в интернет-морде. Исторически в нём лежал широкий
личный PAT, и на него же молча подсели воркер, оркестратор, замер задержки и
runner-bridge — сузили токен, сломали конвейер. Разделение обязано быть
заметным: регресс в `secrets.GH_DISPATCH_TOKEN` вне deploy-пути красит CI, а не
молча возвращает широкий PAT туда, откуда его вывели.

Правила:
  1. `secrets.GH_DISPATCH_TOKEN` в .github/workflows/ упоминает ТОЛЬКО
     deploy-worker.yml (единственный потребитель узкого токена: синхронизация
     значения в секрет воркера).
  2. deploy-worker.yml обязан сохранять саму синхронизацию
     (`wrangler secret put GH_DISPATCH_TOKEN`) — иначе узкий токен не доедет
     до воркера, и морда молча останется на старом значении.
  3. Бывшие широкие потребители (worker, orchestra, deploy-dsh-edge,
     dispatch-latency-probe) обязаны читать `secrets.GH_PIPELINE_PAT` —
     исчезновение источника PAT замечается здесь, а не 401 на первом пуше.

Запуск: python -m pytest scripts/lib/test_dispatch_token_usage.py -q
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

DISPATCH_SECRET = "secrets.GH_DISPATCH_TOKEN"
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

ALL_WORKFLOWS = sorted(path.name for path in WORKFLOWS.glob("*.yml"))


def workflow_text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_every_workflow_file_is_known_to_the_guard():
    """Список workflows зафиксирован: новый файл с секретами не проскочит мимо гвардии."""
    for name in ALL_WORKFLOWS:
        assert (WORKFLOWS / name).is_file(), name
    assert DISPATCH_CONSUMER in ALL_WORKFLOWS
    assert set(PIPELINE_CONSUMERS) <= set(ALL_WORKFLOWS)


def test_dispatch_token_secret_used_only_by_deploy_worker():
    """Узкий токен живёт только в deploy-пути морды (правило 1)."""
    squatters = [
        name
        for name in ALL_WORKFLOWS
        if name != DISPATCH_CONSUMER and DISPATCH_SECRET in workflow_text(name)
    ]
    assert not squatters, (
        f"secrets.GH_DISPATCH_TOKEN вне {DISPATCH_CONSUMER}: {squatters} — "
        "это узкий fine-grained PAT морды (задача #6, ADR 0008); широким "
        "потребителям нужен secrets.GH_PIPELINE_PAT"
    )


def test_deploy_worker_still_syncs_dispatch_token():
    """Деплой обязан доставлять узкий токен в секрет воркера (правило 2)."""
    text = workflow_text(DISPATCH_CONSUMER)
    assert DISPATCH_SECRET in text, (
        f"{DISPATCH_CONSUMER} больше не читает {DISPATCH_SECRET} — синхронизация "
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
