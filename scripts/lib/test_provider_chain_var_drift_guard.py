#!/usr/bin/env python3
"""Гвардия дрейфа `vars.DSH_PROVIDER_CHAIN` против `config/provider-usage.json`
(#1289, доводка задачи про HTTP_410 — найдена координатором СРАЗУ после
починки NVIDIA-NIM: восьмипровайдерный манифест против девятипровайдерной
переменной, слот ZAI молча потерялся при переходе на манифест).

## Класс

Манифест (`config/provider-usage.json`, #823) и переходная переменная
`vars.DSH_PROVIDER_CHAIN` — два места с одним и тем же по смыслу списком
провайдеров. По `docs/runbooks/switch-llm-provider.md` (строки «Цепочка
провайдеров») манифест ПРИОРИТЕТНЕЕ переменной, когда файл присутствует — а
он присутствует в этом репозитории ВСЕГДА (закоммичен, не .gitignore).
Значит переменная — исторический фоллбэк на случай отсутствия файла, но
живёт своей жизнью: правки манифеста (замена мёртвого id, подтверждённый
`max_output_tokens`) НЕ обязаны попадать в переменную и наоборот.

Это создаёт ДВА разных класса расхождения с разной ценой:
  - расхождение ДЕТАЛЕЙ записи (model id, max_output_tokens, base_url) у
    провайдера, присутствующего в ОБОИХ местах, — ОЖИДАЕМО и не гейтится
    здесь: манифест эволюционирует живыми проверками (#1062, #1289), а
    переменная умышленно не поддерживается на этот уровень детализации
    (иначе КАЖДАЯ правка манифеста требовала бы `gh variable set` — то, от
    чего манифест #823 и должен был избавить, design.md «Problem»);
  - расхождение МНОЖЕСТВА ИМЁН провайдеров — слот есть в одном месте и
    отсутствует в другом — ДРУГОЙ класс: значит слот либо молча потерялся
    при переносе (живой случай — запись ZAI: введена #857, снята #1067 из
    манифеста, но НЕ снята из vars.DSH_PROVIDER_CHAIN — никто не заметил
    восемь дней, пока цепочка ревью не исчерпалась целиком и не понадобился
    резерв), либо решение снять слот принято, но причина не записана рядом
    с манифестом (а не в чьей-то голове/в generated_by, который слишком
    длинный, чтобы его реально читали). Этот скрипт гейтит ИМЕННО это —
    множество имён, не поля.

## Почему не repo_invariants.py

`scripts/orchestra/repo_invariants.py` (#244) — общий дом для инвариантов
над состоянием issues/PR/openspec, занят несколькими параллельными PR
прямо сейчас (см. `scripts/lib/invariant_numbering.py`, живой арбитраж
коллизий номеров) и требует сетевых вызовов gh() для большинства проверок.
Эта проверка — ЧИСТАЯ (файл + строка окружения, без сети) и по жанру ближе
к `scripts/lib/test_provider_quota_state_guard.py`/
`scripts/lib/test/provider-default.guard.sh` — свой файл, свой CI-шаг,
не встраивание в перегруженный общий реестр.

## Носитель

CI-шаг `repo-ci.yml` («Гвардия дрейфа vars.DSH_PROVIDER_CHAIN vs манифеста,
#1289») запускает `main()` с `DSH_PROVIDER_CHAIN: ${{ vars.DSH_PROVIDER_CHAIN }}`
в env — читает переменную КОНТЕКСТОМ workflow (тот же путь без токена, что
уже применяет `dsh_require_provider_chain`), не `gh variable get` (не тратит
API-квоту, #454). Переменная не задана в окружении локального прогона —
шаг НЕ падает (честный пропуск «нет данных», не гадание) — тот же принцип,
что применяет `dsh_quota_state_validate` при отсутствии `DSH_PROVIDER_QUOTA_
UNTIL`.

Запуск (нужен файл манифеста в дереве, сеть не нужна):
    DSH_PROVIDER_CHAIN='[{"name":"X",...}]' python scripts/lib/test_provider_chain_var_drift_guard.py
Тесты (без сети, фейковые/реальные фикстуры прод-формы):
    python -m pytest scripts/lib/test_provider_chain_var_drift_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import os
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST_PATH = REPO_ROOT / "config" / "provider-usage.json"


def collect_manifest_names(manifest_path: Path = DEFAULT_MANIFEST_PATH) -> set[str]:
    """Union имён провайдеров по ВСЕМ цепочкам манифеста (не только
    default-chain) — новая цепочка с уникальным именем не обязана дублировать
    имя в var, но если у неё то же имя, что уже есть в var, это тоже
    учитывается."""
    if not manifest_path.exists():
        return set()
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for chain in raw.get("chains", {}).values():
        for entry in chain:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.add(entry["name"])
    return names


def collect_var_names(var_chain_raw: str) -> set[str] | None:
    """None — вход не распарсился (не JSON, не список) — вызывающий обязан
    отличить «нет валидных данных» от «валидный пустой список», иначе
    поломанная переменная превратилась бы в ложное «все имена потеряны»."""
    try:
        parsed = json.loads(var_chain_raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    names = set()
    for entry in parsed:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            names.add(entry["name"])
    return names


def find_name_drift(var_names: set[str], manifest_names: set[str]) -> dict:
    """Множества имён, не поля (см. докстринг модуля, «Класс»). Возврат —
    {"only_in_var": [...], "only_in_manifest": [...]}, отсортировано для
    детерминированного вывода."""
    return {
        "only_in_var": sorted(var_names - manifest_names),
        "only_in_manifest": sorted(manifest_names - var_names),
    }


def main() -> int:
    raw = os.environ.get("DSH_PROVIDER_CHAIN", "")
    if not raw:
        print("гвардия дрейфа vars.DSH_PROVIDER_CHAIN: переменная не передана этому "
              "прогону — сверка пропущена (нет данных, не нарушение), #1289")
        return 0

    var_names = collect_var_names(raw)
    if var_names is None:
        print("::error::гвардия дрейфа vars.DSH_PROVIDER_CHAIN: значение переменной "
              "не парсится как JSON-массив — dsh_require_provider_chain отклонит его "
              "тем же способом на реальном прогоне (#1289)", file=sys.stderr)
        return 1

    # DEFAULT_MANIFEST_PATH передаётся ЯВНО, не через дефолт параметра —
    # дефолт связывается один раз при определении функции, monkeypatch
    # модульной переменной в тестах его не видит (тот же класс, что уже
    # решён в provider_model_discovery.py::main() через явную передачу
    # DSH_CI_SH). Явная передача читает актуальное значение на каждый вызов.
    manifest_names = collect_manifest_names(DEFAULT_MANIFEST_PATH)
    if not manifest_names:
        print("гвардия дрейфа vars.DSH_PROVIDER_CHAIN: config/provider-usage.json "
              "не найден или пуст — сверка пропущена, инвариант 11 "
              "(check_provider_usage_manifest) уже покрывает этот факт отдельно, #1289")
        return 0

    drift = find_name_drift(var_names, manifest_names)
    if not drift["only_in_var"] and not drift["only_in_manifest"]:
        print(f"гвардия дрейфа vars.DSH_PROVIDER_CHAIN: множества имён совпадают "
              f"({len(manifest_names)} провайдер(ов)) — расхождения нет, #1289")
        return 0

    if drift["only_in_var"]:
        print(f"::error::гвардия дрейфа vars.DSH_PROVIDER_CHAIN: имена "
              f"{drift['only_in_var']} есть в vars.DSH_PROVIDER_CHAIN, но отсутствуют "
              f"в config/provider-usage.json — слот молча потерялся при правке "
              f"манифеста, либо решение снять его не записано рядом с массивом "
              f"(AGENTS.md, «Утверждение о готовом артефакте обязано нести его "
              f"адрес») — либо верни слот в манифест, либо убери из переменной "
              f"(`gh variable set DSH_PROVIDER_CHAIN`) с причиной в этом же коммите. "
              f"(#1289)", file=sys.stderr)
    if drift["only_in_manifest"]:
        print(f"::error::гвардия дрейфа vars.DSH_PROVIDER_CHAIN: имена "
              f"{drift['only_in_manifest']} есть в config/provider-usage.json, но "
              f"отсутствуют в vars.DSH_PROVIDER_CHAIN — фоллбэк на случай "
              f"отсутствия манифеста окажется УЖЕ протухшим в момент, когда "
              f"понадобится; допиши в переменную (`gh variable set "
              f"DSH_PROVIDER_CHAIN`) или объясни, почему фоллбэк намеренно не "
              f"полон. (#1289)", file=sys.stderr)
    return 1


# ══════════════════════════════════════════════════════════════════════════
# Тесты — прод-форма (живой снимок vars.DSH_PROVIDER_CHAIN, run gh variable
# get 2026-09-15) + синтетические фикстуры для граничных случаев.
# ══════════════════════════════════════════════════════════════════════════

# Дословный живой снимок `gh variable get DSH_PROVIDER_CHAIN` (2026-09-15,
# ДО фикса этой задачи) — прод-форма, не пересказ (AGENTS.md «Тест кормит
# прод-форму данных»). Секретов не несёт (список публично известных имён/
# base_url/model id, ключи — только ИМЕНА переменных окружения).
LIVE_VAR_SNAPSHOT_2026_09_15 = (
    '[{"name":"GLM","base_url":"https://api.z.ai/api/coding/paas/v4",'
    '"model":"glm-5.3-flash","secret_env":"DEEPSEEK_API_KEY","max_output_tokens":131072},'
    '{"name":"ZAI","base_url":"https://api.z.ai/api/coding/paas/v4",'
    '"model":"glm-5","secret_env":"ZAI_1_API_KEY","max_output_tokens":131072},'
    '{"name":"OpenRouter-2","base_url":"https://openrouter.ai/api/v1",'
    '"model":"nvidia/nemotron-3-super-120b-a12b:free","secret_env":"OPENROUTER_2_API_KEY",'
    '"max_output_tokens":131072},'
    '{"name":"Ollama-2","base_url":"https://ollama.com/v1","model":"nemotron-3-ultra",'
    '"secret_env":"OLLAMA_CLOUD_2_API_KEY","max_output_tokens":131072},'
    '{"name":"Ollama-3","base_url":"https://ollama.com/v1","model":"nemotron-3-ultra",'
    '"secret_env":"OLLAMA_CLOUD_3_API_KEY","max_output_tokens":131072},'
    '{"name":"Ollama-1","base_url":"https://ollama.com/v1","model":"nemotron-3-ultra",'
    '"secret_env":"OLLAMA_CLOUD_1_API_KEY","max_output_tokens":131072},'
    '{"name":"NVIDIA-NIM-1","base_url":"https://integrate.api.nvidia.com/v1",'
    '"model":"deepseek-ai/deepseek-v4-pro-0813","secret_env":"NVIDIA_NIM_1_API_KEY",'
    '"max_output_tokens":131072},'
    '{"name":"OpenRouter-1","base_url":"https://openrouter.ai/api/v1",'
    '"model":"nvidia/nemotron-3-super-120b-a12b:free","secret_env":"OPENROUTER_1_API_KEY",'
    '"max_output_tokens":131072},'
    '{"name":"NVIDIA-NIM-2","base_url":"https://integrate.api.nvidia.com/v1",'
    '"model":"deepseek-ai/deepseek-v4-pro-0813","secret_env":"NVIDIA_NIM_2_API_KEY",'
    '"max_output_tokens":131072}]'
)


def test_live_snapshot_before_fix_shows_zai_only_in_var():
    """Мутационное доказательство ЖИВЫМ снимком: ДО фикса этой задачи (ZAI
    ещё не в манифесте) сверка живой var против ТЕКУЩЕГО дерева манифеста
    (уже с ZAI, после фикса) обязана дать пустой дрейф — регрессия теста
    доказывается тем, что до восстановления ZAI в манифесте этот же тест
    красил бы (найдено координатором live-замером vars vs файла)."""
    var_names = collect_var_names(LIVE_VAR_SNAPSHOT_2026_09_15)
    manifest_names = collect_manifest_names()
    drift = find_name_drift(var_names, manifest_names)
    assert drift["only_in_var"] == [], (
        f"ZAI обязан быть в манифесте после фикса #1289: {drift}"
    )


def test_matching_sets_no_drift():
    var_names = {"A", "B", "C"}
    manifest_names = {"A", "B", "C"}
    assert find_name_drift(var_names, manifest_names) == {
        "only_in_var": [], "only_in_manifest": [],
    }


def test_slot_lost_from_manifest_detected():
    """Класс живого случая #1289: ZAI есть в var, пропал из манифеста."""
    var_names = {"GLM", "ZAI", "OpenRouter-1"}
    manifest_names = {"GLM", "OpenRouter-1"}
    drift = find_name_drift(var_names, manifest_names)
    assert drift["only_in_var"] == ["ZAI"]
    assert drift["only_in_manifest"] == []


def test_slot_added_to_manifest_not_yet_in_var_detected():
    var_names = {"GLM"}
    manifest_names = {"GLM", "NEW-PROVIDER"}
    drift = find_name_drift(var_names, manifest_names)
    assert drift["only_in_var"] == []
    assert drift["only_in_manifest"] == ["NEW-PROVIDER"]


def test_field_level_drift_not_flagged_only_name_set_matters():
    """Честная граница класса (см. докстринг «Класс»): модель/max_output_tokens
    расходятся у ОБЩЕГО имени — это НЕ дрейф множества имён, гвардия молчит.
    Живой пример — NVIDIA-NIM-1/2 несут разные model/max_output_tokens в var
    (старый мёртвый id) и в манифесте (новый живой id, #1289) — оба места
    ЗНАЮТ про этого провайдера, только детали устарели в фоллбэке."""
    var_names = {"NVIDIA-NIM-1"}
    manifest_names = {"NVIDIA-NIM-1"}
    assert find_name_drift(var_names, manifest_names) == {
        "only_in_var": [], "only_in_manifest": [],
    }


def test_var_not_json_returns_none_not_empty_set():
    """None ≠ пустое множество — поломанная переменная не должна выглядеть
    как «все имена потеряны» (ложные N нарушений)."""
    assert collect_var_names("не json вовсе") is None
    assert collect_var_names('{"not": "a list"}') is None


def test_var_empty_string_main_skips_without_violation(monkeypatch, capsys):
    monkeypatch.delenv("DSH_PROVIDER_CHAIN", raising=False)
    rc = main()
    assert rc == 0
    assert "переменная не передана" in capsys.readouterr().out


def test_var_malformed_main_fails_loud(monkeypatch, capsys):
    monkeypatch.setenv("DSH_PROVIDER_CHAIN", "not json")
    rc = main()
    assert rc == 1
    assert "не парсится" in capsys.readouterr().err


def test_var_matching_manifest_main_succeeds(monkeypatch, capsys, tmp_path):
    manifest = tmp_path / "provider-usage.json"
    manifest.write_text(json.dumps({"chains": {"c": [{"name": "A"}]}}), encoding="utf-8")
    monkeypatch.setenv("DSH_PROVIDER_CHAIN", '[{"name":"A","base_url":"x","model":"y","secret_env":"K"}]')
    monkeypatch.setattr(sys.modules[__name__], "DEFAULT_MANIFEST_PATH", manifest)
    rc = main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "расхождения нет" in out


def test_var_drift_main_fails_loud_and_names_both_directions(monkeypatch, capsys, tmp_path):
    manifest = tmp_path / "provider-usage.json"
    manifest.write_text(
        json.dumps({"chains": {"c": [{"name": "A"}, {"name": "NEW"}]}}), encoding="utf-8")
    monkeypatch.setenv(
        "DSH_PROVIDER_CHAIN",
        '[{"name":"A","base_url":"x","model":"y","secret_env":"K"},'
        '{"name":"LOST","base_url":"x","model":"y","secret_env":"K"}]',
    )
    monkeypatch.setattr(sys.modules[__name__], "DEFAULT_MANIFEST_PATH", manifest)
    rc = main()
    err = capsys.readouterr().err
    assert rc == 1
    assert "'LOST'" in err
    assert "'NEW'" in err


if __name__ == "__main__":
    sys.exit(main())
