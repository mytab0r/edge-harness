#!/usr/bin/env python3
"""Сборщик назначений «потребитель -> цепочка провайдеров» (задача #823,
openspec/changes/llm-provider-usage-manifest).

Тот же класс проблемы, что уже закрыт для меток (docs/agents/LABELS.md,
задача #207): манифест использования (config/provider-usage.json) правится
отдельно от документации, которая его описывает человеку — реестр
docs/agents/LLM-PROVIDER-USAGE.md отстанет от файла ровно тем же образом, если
не собирать таблицу СКРИПТОМ. Список потребителей здесь не хардкодится
случайно — CONSUMERS фиксирован (осознанная правка при появлении нового
канала, тот же приём, что EXPECTED_WORKFLOWS в
scripts/lib/test_dispatch_token_usage.py).

Различает ТРИ состояния на потребителя (design.md, «Видимость», п.2 — тот же
довод, что критерий #158 dsh-edge-provider-registry: not_configured/
configured_but_rejected/all_providers_failed лечатся по-разному, значит не
сливаются в один зелёный флаг):
  - "missing"  — в usage-карте манифеста нет записи потребителя;
  - "dangling" — запись есть, но .chains не содержит такого имени, или
                 цепочка пуста;
  - "ok"       — назначена непустая цепочка; для ok дополнительно печатается
                 число провайдеров и то, что механизм потребителя реально
                 умеет failover (все три канала на 2026-09-09 — #727/#797/#805,
                 см. tasks.md «Находки при апробации»).

Морда (id "morda") в манифесте не участвует (design.md, «Потребители»: один
слот адаптера, failover туда не помещается) — печатается отдельной, ВСЕГДА
статической строкой видимости, не через CONSUMERS/usage.

Запуск как модуль: python -m scripts.lib.collect_provider_usage (или файлом,
как остальные скрипты этого репозитория).
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "config" / "provider-usage.json"

# Канонический список потребителей, обязанных нести валидное назначение
# (совпадает с check_provider_usage_manifest в
# scripts/orchestra/repo_invariants.py — одно место правды на список,
# гвардия test_provider_usage_registry.py сверяет оба источника не тавтологией,
# а прямым импортом ЭТОГО модуля).
CONSUMERS: tuple[str, ...] = ("ai-review", "worker", "hands")

# Механизм каждого потребителя — статический факт о КОДЕ (не о манифесте):
# все три канала сейчас реально исполняют dsh_run_with_provider_chain
# (#727/#797/#805, найдено на apply — proposal.md называл hands.yml без
# failover, это устарело относительно кода, см. tasks.md «Находки»).
#
# self-review (#1025, scripts/orchestra/self_review.py) НЕ добавлен сюда
# сознательно: этот канонический список совпадает с
# check_provider_usage_manifest в scripts/orchestra/repo_invariants.py —
# файлом, который параллельно правят другие PR (см. AGENTS.md этого change,
# «не трогать»). self-review читает свою запись `.usage["self-review"]`
# НАПРЯМУЮ через dsh_load_provider_chain_from_manifest (scripts/lib/
# dsh-ci.sh) — bash-путь смотрит в config/provider-usage.json по ключу, не
# через CONSUMERS: назначение работает и без строки в этом списке.
# Видимость self-review в реестре docs/agents/LLM-PROVIDER-USAGE.md — из
# CONSUMERS, поэтому её ЗДЕСЬ пока нет; это честный пробел, не скрытая
# правка чужого инварианта — впиши, когда repo_invariants.py освободится
# от параллельных PR (#944/#831/#1020/#811).
CONSUMER_MECHANISM: dict[str, str] = {
    "ai-review": "dsh_run_with_provider_chain (scripts/review/ai_dsh.sh) — полный failover",
    "worker": "dsh_run_with_provider_chain (scripts/worker/task.sh) — полный failover",
    "hands": "dsh_run_with_provider_chain (scripts/hands/dsh_task.sh) — полный failover (#805)",
}

MORDA_VISIBILITY_NOTE = (
    "1 слот адаптера через Settings -> Models (plugins-src/provider-registry, #378) — "
    "failover туда не помещается, вне манифеста принципиально "
    "(docs/runbooks/switch-llm-provider.md, «Морда — вне цепочки принципиально»)"
)


def load_manifest(path: Path = MANIFEST_PATH) -> dict | None:
    """None — файла нет вовсе (отличается от валидного, но пустого манифеста:
    вызывающий обязан различать «не подключено» и «подключено с дырой»)."""
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_consumer(manifest: dict | None, consumer: str) -> dict:
    """Одна запись отчёта на потребителя. Форма:
    {"consumer": str, "state": "missing"|"dangling"|"ok"|"no_manifest",
     "chain_name": str|None, "provider_count": int|None, "mechanism": str}
    """
    mechanism = CONSUMER_MECHANISM.get(consumer, "неизвестный механизм")
    if manifest is None:
        return {"consumer": consumer, "state": "no_manifest", "chain_name": None,
                 "provider_count": None, "mechanism": mechanism}
    usage = manifest.get("usage", {}) or {}
    chain_name = usage.get(consumer)
    if not chain_name:
        return {"consumer": consumer, "state": "missing", "chain_name": None,
                 "provider_count": None, "mechanism": mechanism}
    chains = manifest.get("chains", {}) or {}
    chain = chains.get(chain_name)
    if not chain:
        return {"consumer": consumer, "state": "dangling", "chain_name": chain_name,
                 "provider_count": None, "mechanism": mechanism}
    return {"consumer": consumer, "state": "ok", "chain_name": chain_name,
             "provider_count": len(chain), "mechanism": mechanism}


def collect_provider_usage(path: Path = MANIFEST_PATH) -> list[dict]:
    manifest = load_manifest(path)
    return [resolve_consumer(manifest, consumer) for consumer in CONSUMERS]


def format_row(entry: dict) -> str:
    consumer = entry["consumer"]
    if entry["state"] == "no_manifest":
        return f"| `{consumer}` | (манифеста нет) | — | {entry['mechanism']} |"
    if entry["state"] == "missing":
        return f"| `{consumer}` | 🚨 не назначена | — | {entry['mechanism']} |"
    if entry["state"] == "dangling":
        return f"| `{consumer}` | 🚨 `{entry['chain_name']}` (цепочка не найдена/пуста) | — | {entry['mechanism']} |"
    return f"| `{consumer}` | `{entry['chain_name']}` | {entry['provider_count']} | {entry['mechanism']} |"


if __name__ == "__main__":
    for row in collect_provider_usage():
        print(format_row(row))
    print(f"| `morda` | (вне манифеста) | — | {MORDA_VISIBILITY_NOTE} |")
