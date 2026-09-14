#!/usr/bin/env python3
"""Тесты одного места правды «прогон workflow относится к main» (issue #925,
scripts/lib/ci_run_on_main.py).

Живой случай, доказывающий класс: три подряд «зелёных» прогона
deploy-worker.yml (2026-09-12T00:06–00:17Z) были `workflow_dispatch` на
ветке `agent/678-rows-written-namespace` (GitHub Compare API
`main...53135c4e2` → `status=diverged`, `main...df00aba58` → тоже
`diverged`), а последний прогон push'а в main (2026-09-11T14:24Z, слияние
PR #943, `head_sha=490ccacd5`) — красный (`main...490ccacd5` →
`status=behind`, `ahead_by=0`). Реальные значения ниже сняты живым `gh api
repos/mytab0r/edge-harness/compare/main...<sha>` 2026-09-12/13 — не пересказ
(AGENTS.md «тест кормит прод-форму данных»).

Мутационное доказательство (тест
`test_head_is_on_branch_mutation_proof_documented_below`, докстринг ниже):
`head_is_on_branch` сведена к одному `in`-сравнению с `_ON_BRANCH_STATUSES`
специально, чтобы «снять тело проверки предка» значило одну правку —
замени `return compare_status in _ON_BRANCH_STATUSES` на `return True`, и
`test_latest_run_on_main_rejects_branch_run_accepts_main_run` (единственный
тест, который различает ветку от main) краснеет; верни — снова зелёный.
Дословный вывод обеих прогонок — в отчёте задачи, не здесь (мутация делается
руками поверх этого файла, не хранится в репозитории как отдельный тест
«тест без тела»).

Запуск: python -m pytest scripts/lib/test_ci_run_on_main.py -q
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "ci_run_on_main.py"
spec = importlib.util.spec_from_file_location("ci_run_on_main", SCRIPT)
crom = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crom)  # type: ignore[union-attr]

REPO = "mytab0r/edge-harness"

# Реальные head_sha живого случая #925 (2026-09-11/12) — см. докстринг модуля.
BRANCH_HEAD_1 = "53135c4e27b894e9cc23b0d8e5c78d62d73b01ad"  # workflow_dispatch, agent/678-...
BRANCH_HEAD_2 = "df00aba586176b1d0e31f481e470da1fb5c8018e"  # тот же, второй прогон
MAIN_HEAD = "490ccacd5e80f57b7ef8f98d04c4aa69beccdebc"       # push в main, слияние PR #943


class FakeGh:
    """Маршрутизатор compare-запросов — {(base, head): прод-форма ответа
    GitHub Compare API}, снятая живым вызовом (см. докстринг модуля)."""

    def __init__(self, responses: dict[str, dict]):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, path: str) -> dict:
        self.calls.append(path)
        for fragment, result in self.responses.items():
            if fragment in path:
                return result
        raise AssertionError(f"нет маршрута для: {path}")


def compare_response(status: str, ahead_by: int = 0, behind_by: int = 0) -> dict:
    return {"status": status, "ahead_by": ahead_by, "behind_by": behind_by}


# ══════════════════════════════════════════════════════════════════════════
# head_is_on_branch — чистая функция над уже прочитанным статусом
# ══════════════════════════════════════════════════════════════════════════


def test_head_is_on_branch_true_for_identical_and_behind():
    assert crom.head_is_on_branch("identical") is True
    assert crom.head_is_on_branch("behind") is True


def test_head_is_on_branch_false_for_ahead_and_diverged():
    assert crom.head_is_on_branch("ahead") is False
    assert crom.head_is_on_branch("diverged") is False


# ══════════════════════════════════════════════════════════════════════════
# run_is_on_main — обвязка на GitHub Compare API
# ══════════════════════════════════════════════════════════════════════════


def test_run_is_on_main_true_when_behind_main_live_case():
    """Живой случай: слияние PR #943 (490ccacd5) — status=behind, ahead_by=0."""
    fake = FakeGh({f"compare/main...{MAIN_HEAD}": compare_response("behind", ahead_by=0, behind_by=25)})
    assert crom.run_is_on_main(REPO, MAIN_HEAD, fake) is True
    assert fake.calls == [f"repos/{REPO}/compare/main...{MAIN_HEAD}"]


def test_run_is_on_main_false_when_diverged_pr_branch_live_case():
    """Живой случай: workflow_dispatch на agent/678-... — status=diverged."""
    fake = FakeGh({f"compare/main...{BRANCH_HEAD_1}": compare_response("diverged", ahead_by=4, behind_by=21)})
    assert crom.run_is_on_main(REPO, BRANCH_HEAD_1, fake) is False


def test_run_is_on_main_same_sha_as_main_short_circuits_without_network():
    fake = FakeGh({})  # ни одного маршрута — сеть не должна понадобиться
    assert crom.run_is_on_main(REPO, "main", fake, main_ref="main") is True
    assert fake.calls == []


def test_run_is_on_main_raises_on_missing_status_fail_loud():
    """Ответ без status (неожиданная форма) — RuntimeError, не «предполагаем
    предок»/«предполагаем не предок» (AGENTS.md «fail loud, не silent-wrong»)."""
    fake = FakeGh({f"compare/main...{MAIN_HEAD}": {"unexpected": True}})
    with pytest.raises(RuntimeError):
        crom.run_is_on_main(REPO, MAIN_HEAD, fake)


# ══════════════════════════════════════════════════════════════════════════
# latest_run_on_main — фикстура с прогоном НЕ-предком main рядом с прогоном-
# предком main (доказательство мутацией, см. докстринг модуля)
# ══════════════════════════════════════════════════════════════════════════


def test_latest_run_on_main_rejects_branch_run_accepts_main_run():
    """Живой случай #925 буквально: список прогонов deploy-worker.yml от
    нового к старому — сначала два «зелёных» прогона на ветке-кандидате
    (НЕ предок main), затем красный прогон, реально триггернутый push'ем в
    main. Без проверки предка `next(run for run in runs if
    run['conclusion']=='success')` вернул бы ветку-кандидата и замаскировал
    бы красное состояние main — ровно инцидент issue #925.
    `latest_run_on_main` обязана пропустить оба зелёных прогона ветки и
    вернуть красный прогон main."""
    runs = [
        {"head_sha": BRANCH_HEAD_1, "conclusion": "success", "html_url": "run/2"},
        {"head_sha": BRANCH_HEAD_2, "conclusion": "success", "html_url": "run/1"},
        {"head_sha": MAIN_HEAD, "conclusion": "failure", "html_url": "run/0"},
    ]
    fake = FakeGh({
        f"compare/main...{BRANCH_HEAD_1}": compare_response("diverged", ahead_by=4, behind_by=21),
        f"compare/main...{BRANCH_HEAD_2}": compare_response("diverged", ahead_by=3, behind_by=21),
        f"compare/main...{MAIN_HEAD}": compare_response("behind", ahead_by=0, behind_by=25),
    })
    run = crom.latest_run_on_main(runs, REPO, fake)
    assert run is not None
    assert run["head_sha"] == MAIN_HEAD
    assert run["conclusion"] == "failure"


def test_latest_run_on_main_returns_none_when_no_run_is_on_main():
    runs = [{"head_sha": BRANCH_HEAD_1, "conclusion": "success"}]
    fake = FakeGh({f"compare/main...{BRANCH_HEAD_1}": compare_response("ahead", ahead_by=2, behind_by=0)})
    assert crom.latest_run_on_main(runs, REPO, fake) is None


def test_latest_run_on_main_skips_runs_without_head_sha():
    runs = [{"conclusion": "success"}, {"head_sha": MAIN_HEAD, "conclusion": "success"}]
    fake = FakeGh({f"compare/main...{MAIN_HEAD}": compare_response("identical")})
    run = crom.latest_run_on_main(runs, REPO, fake)
    assert run["head_sha"] == MAIN_HEAD
