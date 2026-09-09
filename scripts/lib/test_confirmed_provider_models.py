#!/usr/bin/env python3
"""Гвардия реестра подтверждённых id моделей (#737) — закрывает класс #798:
«первый элемент живой цепочки провайдеров не подтверждён реестром, и это
осталось незамеченным, пока не проявилось замером времени гейта».

Модель-id не хардкодится литералом в этом файле (класс #153,
scripts/lib/test/provider-default.guard.sh сканирует scripts/** на литералы
прежних дефолтов провайдеров/моделей — единственное место, где реальным id
разрешено жить буквально, это config/provider-usage.json, вне scope этой
гвардии, см. её собственный "$comment"): обе проверки читают модель из
МАНИФЕСТА по имени провайдера/по списку моделей, а не по вписанной сюда строке.

Два теста:
  1. Провайдер `NVIDIA-nano` (#798) — конкретный факт: живой первый элемент
     цепочки `config/provider-usage.json` обязан быть подтверждён реестром.
     Мутация: снять его запись из `confirmed-provider-models.json` — тест
     краснеет (проверено вручную при подготовке PR #798).
  2. Общий класс: КАЖДАЯ модель, перечисленная в любой цепочке
     `config/provider-usage.json` (актуальный источник правды для
     `ai-review`/`worker`/`hands`, приоритетнее `vars.DSH_PROVIDER_CHAIN` —
     `docs/runbooks/switch-llm-provider.md`, «Цепочка провайдеров»), обязана
     быть подтверждена реестром — гвардия ловит СЛЕДУЮЩИЙ такой же случай ДО
     того, как элемент цепочки станет мёртвым грузом (докстринг
     `dsh_model_confirmed`, scripts/lib/dsh-ci.sh), сравнением хэшей напрямую
     (тот же алгоритм — sha256 строки id), без прогона самого dsh-ci.sh (bash).

Запуск: python -m pytest scripts/lib/test_confirmed_provider_models.py -q
"""

import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "scripts" / "lib" / "confirmed-provider-models.json"
MANIFEST_PATH = REPO_ROOT / "config" / "provider-usage.json"

# Живой первый элемент, чей пропуск замерен и заведён задачей #798 — сверяем
# по ИМЕНИ провайдера в манифесте, не по вписанному сюда id.
LIVE_FAST_PROVIDER_NAME = "NVIDIA-nano"


def _sha256(model_id: str) -> str:
    return hashlib.sha256(model_id.encode("utf-8")).hexdigest()


def _confirmed_hashes() -> set[str]:
    entries = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return {entry["model_sha256"] for entry in entries}


def _manifest_chains() -> dict[str, list[dict]]:
    """{имя цепочки: [элемент, ...]} — пусто, если манифеста нет вовсе (тогда
    оба теста этого файла тривиально проходят: не их забота, вместо fallback
    на vars.DSH_PROVIDER_CHAIN, который здесь не читается)."""
    if not MANIFEST_PATH.is_file():
        return {}
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return manifest.get("chains", {}) or {}


def _find_model_by_provider_name(name: str) -> str | None:
    for chain in _manifest_chains().values():
        for entry in chain:
            if entry.get("name") == name:
                return entry["model"]
    return None


def test_live_fast_provider_confirmed():
    """#798: живой (не гипотетический) риск — цепочка каждый прогон молча
    пропускала первый (быстрый) элемент, потому что его id не был подтверждён
    реестром, хотя строка верна (сверено #798 живым ответом каталога моделей)."""
    model_id = _find_model_by_provider_name(LIVE_FAST_PROVIDER_NAME)
    if model_id is None:
        return  # манифест не содержит этого провайдера в этой среде — не наша забота
    confirmed = _confirmed_hashes()
    assert _sha256(model_id) in confirmed, (
        f"провайдер {LIVE_FAST_PROVIDER_NAME!r} из {MANIFEST_PATH} не подтверждён "
        f"{REGISTRY_PATH} — dsh_model_confirmed() пропустит его в "
        f"dsh_run_with_provider_chain (#737), как это уже произошло в #798"
    )


def test_every_manifest_model_is_confirmed():
    """Класс #798, не частный случай: ни одна цепочка config/provider-usage.json
    не должна нести модель, не подтверждённую реестром — иначе она мёртвый груз
    в цепочке, невидимый, пока кто-то не измерит время гейта."""
    confirmed = _confirmed_hashes()
    unconfirmed: list[str] = []
    for chain_name, chain in _manifest_chains().items():
        for entry in chain:
            model_id = entry["model"]
            if _sha256(model_id) not in confirmed:
                unconfirmed.append(f"{chain_name}/{entry.get('name', '?')}")
    assert not unconfirmed, (
        "провайдеры цепочек config/provider-usage.json без подтверждения id в "
        f"{REGISTRY_PATH} (класс #798 — dsh_model_confirmed пропустит их "
        "молча, каждый прогон переходя сразу к следующему, часто более "
        "медленному, элементу): " + ", ".join(unconfirmed)
    )
