#!/usr/bin/env python3
"""Тесты эскалации автооткота `deploy-worker.yml` (#614).

Проводка — на моке `pulse_guard.gh` (тот же приём, что
`test_branch_protection_watch.py`): `escalate()` постит комментарий в
задачу-статус #120 и best-effort шлёт Telegram; в тестовой среде без секретов
Telegram честно «не доставлен», место правды — факт POST-комментария.

Запуск: python -m pytest scripts/orchestra/test_deploy_worker_rollback_alert.py -q
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

SCRIPT = _DIR / "deploy_worker_rollback_alert.py"
spec = importlib.util.spec_from_file_location("deploy_worker_rollback_alert", SCRIPT)
dwra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dwra)  # type: ignore[union-attr]

pg = sys.modules["pulse_guard"]

REPO = "mytab0r/edge-harness"

WORKFLOW = _DIR.parents[1] / ".github" / "workflows" / "deploy-worker.yml"


class FakeGh:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, *args):
        self.calls.append(" ".join(args))
        return None


@pytest.fixture()
def offline_telegram(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


# ── parse_bool_env: форма вывода GitHub Actions ("true"/"false", регистр, пусто) ──


def test_parse_bool_env_true_variants():
    assert dwra.parse_bool_env("true") is True
    assert dwra.parse_bool_env("True") is True
    assert dwra.parse_bool_env("  true ") is True


def test_parse_bool_env_false_and_missing_default_to_false():
    assert dwra.parse_bool_env("false") is False
    assert dwra.parse_bool_env("") is False
    assert dwra.parse_bool_env(None) is False
    assert dwra.parse_bool_env("garbage") is False


# ── Чистая логика: три исхода текста, от тише к громче (см. докстринг модуля) ──


def test_rollback_not_confirmed_is_the_loudest_case():
    text = dwra.rollback_alert_text(REPO, "123", rollback_confirmed=False, post_rollback_ok=False)
    assert "НЕМЕДЛЕННО" in text
    assert "АВТООТКАТ НЕ ПОДТВЕРЖДЁН" in text


def test_rollback_confirmed_but_post_check_still_red_is_the_worst_case():
    # Задача #614, п.1: "если и она красная — это худший случай, он обязан быть громким".
    text = dwra.rollback_alert_text(REPO, "123", rollback_confirmed=True, post_rollback_ok=False)
    assert "НЕМЕДЛЕННО" in text
    assert "откатанная версия не отвечает" in text


def test_rollback_confirmed_and_post_check_green_is_informational_not_alarming():
    text = dwra.rollback_alert_text(REPO, "123", rollback_confirmed=True, post_rollback_ok=True)
    assert "НЕМЕДЛЕННО" not in text
    assert text.startswith("🔙")


def test_two_loud_outcomes_both_carry_the_siren_and_the_same_marker():
    loud_a = dwra.rollback_alert_text(REPO, "1", rollback_confirmed=False, post_rollback_ok=False)
    loud_b = dwra.rollback_alert_text(REPO, "1", rollback_confirmed=True, post_rollback_ok=False)
    quiet = dwra.rollback_alert_text(REPO, "1", rollback_confirmed=True, post_rollback_ok=True)
    for text in (loud_a, loud_b, quiet):
        assert dwra.MARKER in text
    assert loud_a.startswith("🚨")
    assert loud_b.startswith("🚨")
    assert not quiet.startswith("🚨")


def test_alert_text_carries_run_link_built_from_server_url_repo_and_run_id():
    text = dwra.rollback_alert_text(
        REPO, "999", rollback_confirmed=True, post_rollback_ok=True,
        server_url="https://github.example",
    )
    assert "https://github.example/mytab0r/edge-harness/actions/runs/999" in text


def test_alert_text_without_run_id_is_honest_not_a_broken_link():
    text = dwra.rollback_alert_text(REPO, "", rollback_confirmed=True, post_rollback_ok=True)
    assert "без ссылки" in text


# ── Проводка: escalate_rollback идёт тем же каналом, что предохранитель (#120) ──


def test_escalate_rollback_posts_to_watchdog_issue(monkeypatch, offline_telegram):
    fake = FakeGh()
    monkeypatch.setattr(pg, "gh", fake)
    result = dwra.escalate_rollback(REPO, "42", rollback_confirmed=True, post_rollback_ok=False)
    posted = [c for c in fake.calls if "-X POST" in c and "comments" in c]
    assert any(f"repos/{REPO}/issues/{pg.WATCHDOG_ISSUE}/comments" in c for c in posted), \
        f"место правды сигнала — комментарий в #{pg.WATCHDOG_ISSUE}: {fake.calls}"
    assert "Telegram: НЕ доставлен" in result  # секретов нет в тесте — честный исход
    assert f"след в #{pg.WATCHDOG_ISSUE}: оставлен" in result


def test_escalate_rollback_comment_body_names_the_actual_verdict(monkeypatch, offline_telegram):
    fake = FakeGh()
    monkeypatch.setattr(pg, "gh", fake)
    dwra.escalate_rollback(REPO, "42", rollback_confirmed=False, post_rollback_ok=False)
    posted = [c for c in fake.calls if "-X POST" in c and "comments" in c]
    assert any("НЕ ПОДТВЕРЖДЁН" in c for c in posted)


def test_escalate_rollback_does_not_mutate_anything_besides_the_comment(monkeypatch, offline_telegram):
    fake = FakeGh()
    monkeypatch.setattr(pg, "gh", fake)
    dwra.escalate_rollback(REPO, "42", rollback_confirmed=True, post_rollback_ok=True)
    non_comment_mutations = [
        c for c in fake.calls
        if c.startswith(("-X POST", "-X PUT", "-X DELETE", "-X PATCH")) and "comments" not in c
    ]
    assert non_comment_mutations == [], f"сигнал не имеет права мутировать что-то ещё: {fake.calls}"


# ── main(): чтение окружения ровно так, как его передаёт workflow ────────────


def test_main_reads_env_and_escalates_the_worst_case(monkeypatch, offline_telegram):
    fake = FakeGh()
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_RUN_ID", "555")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("ROLLBACK_CONFIRMED", "true")
    monkeypatch.setenv("POST_ROLLBACK_OK", "false")
    assert dwra.main() == 0
    posted = [c for c in fake.calls if "-X POST" in c and "comments" in c]
    assert any("откатанная версия не отвечает" in c for c in posted)


def test_main_reads_env_and_escalates_the_recovered_case(monkeypatch, offline_telegram):
    fake = FakeGh()
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_RUN_ID", "556")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("ROLLBACK_CONFIRMED", "true")
    monkeypatch.setenv("POST_ROLLBACK_OK", "true")
    assert dwra.main() == 0
    posted = [c for c in fake.calls if "-X POST" in c and "comments" in c]
    assert any("🔙" in c for c in posted)


def test_main_requires_github_repository_env(monkeypatch, offline_telegram):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    with pytest.raises(KeyError):
        dwra.main()


# ── Гвардия YAML: повторная канарейка ставит браузер сама, не полагаясь ──────
# ── на то, что предыдущий шаг успел это сделать (находка второго гейта PR #617:
# ── "Канарейка UI на проде" могла упасть до/во время playwright install, либо
# ── шаг секретов после неё — в обоих случаях без переустановки здесь
# ── chromium.launch() падает по средовой причине, и красная повторная
# ── канарейка перестаёт отличаться от "версия не отвечает").
#
# Проверка по структуре YAML (шаг найден по id), не по подстроке всего файла —
# подстрока прошла бы и от комментария, не только от реального run-блока.


def _post_rollback_canary_step():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["deploy"]["steps"]
    matches = [s for s in steps if s.get("id") == "post_rollback_canary"]
    assert len(matches) == 1, "шаг post_rollback_canary не найден или задвоен — структура workflow изменилась"
    return matches[0]


def test_post_rollback_canary_restores_deps_browser_then_runs():
    step = _post_rollback_canary_step()
    run = step.get("run") or ""
    npm_ci_pos = run.find("npm ci")
    install_pos = run.find("playwright install")
    canary_pos = run.find("scripts/canary-ui.mjs")
    assert npm_ci_pos != -1, (
        "шаг «Канарейка после автооткота» не восстанавливает node_modules (npm ci) — "
        "если канарейка выше упала на собственном npm ci (он сносит node_modules до "
        "установки), повторный прогон падает на import \"playwright\" по средовой "
        "причине, а не по реальной недоступности откатанной версии"
    )
    assert install_pos != -1, (
        "шаг «Канарейка после автооткота» не переустанавливает браузер Playwright — "
        "если предыдущая канарейка упала до/во время playwright install (или шаг "
        "секретов упал раньше неё), повторный прогон падает на chromium.launch() по "
        "средовой причине, а не по реальной недоступности откатанной версии"
    )
    assert canary_pos != -1, "шаг post_rollback_canary больше не запускает canary-ui.mjs"
    assert npm_ci_pos < install_pos < canary_pos, (
        "порядок шага обязан быть: npm ci → playwright install → запуск канарейки"
    )


# ── Гвардия сериализации прод-деплоев (блокирующая находка второго гейта ─────
# ── PR #617): класс «прод-деплой без concurrency» — два перекрывающихся
# ── прогона мутируют прод одновременно; после #614 в deploy-worker.yml есть
# ── третий мутирующий игрок (wrangler rollback при красной канарейке), который
# ── на гонке может откатить свежую хорошую версию другого прогона.
#
# Проверяются ВСЕ прод-деплои, найденные сканом исходников workflow (шаг с
# `wrangler deploy` в run-блоке), а не захардкоженный список (замечание
# третьего гейта PR #617): завтрашний новый прод-деплой без сериализации
# обязан упасть на гвардии сам, без того чтобы кто-то вспомнил дописать
# его в список. KNOWN_PROD_DEPLOYS ниже — не источник проверки, а пол против
# ослепления сканера: если сканер перестал находить хотя бы эти два файла,
# проверка пуста и это ошибка сканера, а не «нарушений нет».

WORKFLOWS_DIR = _DIR.parents[1] / ".github" / "workflows"

# Известные на 2026-09-11 прод-деплои; worker-ci.yml сюда не входит — только
# `wrangler dev` (локальный дев-сервер, прод не мутирует).
KNOWN_PROD_DEPLOYS = {"deploy-worker.yml", "deploy-dsh-edge.yml"}


def _prod_deploy_workflow_names() -> list[str]:
    found = []
    for path in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        runs = []
        for job in (doc.get("jobs") or {}).values():
            if not isinstance(job, dict):
                continue
            for step in (job.get("steps") or []):
                if isinstance(step, dict) and isinstance(step.get("run"), str):
                    runs.append(step["run"])
        if any("wrangler deploy" in run for run in runs):
            found.append(path.name)
    return found


def test_prod_deploy_scanner_still_sees_the_known_deploys():
    found = set(_prod_deploy_workflow_names())
    missing = KNOWN_PROD_DEPLOYS - found
    assert not missing, (
        f"сканер прод-деплоев перестал находить {missing} — гвардия сериализации "
        f"проверяет пустоту, а не состояние (ослепление сканера = ошибка сканера)"
    )


@pytest.mark.parametrize("workflow_name", _prod_deploy_workflow_names())
def test_prod_deploy_workflows_serialize_with_concurrency(workflow_name):
    doc = yaml.safe_load((WORKFLOWS_DIR / workflow_name).read_text(encoding="utf-8"))
    conc = doc.get("concurrency")
    assert isinstance(conc, dict) and conc.get("group"), (
        f"{workflow_name}: прод-деплой без concurrency — два перекрывающихся "
        f"прогона деплоя мутируют прод одновременно (находка второго гейта PR #617)"
    )
    assert conc.get("cancel-in-progress") is False, (
        f"{workflow_name}: cancel-in-progress обязан быть false — обрыв уже идущего "
        f"деплоя оставляет частично применённые секреты/версии (тот же приём, что "
        f"у deploy-dsh-edge.yml)"
    )


def test_post_rollback_canary_stays_loud_no_continue_on_error():
    # Задача #614, п.1: это худший случай, он обязан остаться громким —
    # мутация могла бы тихо добавить continue-on-error вместе с фиксом выше.
    step = _post_rollback_canary_step()
    assert "continue-on-error" not in step


# ── Гвардия проводки workflow↔скрипт (некритичное замечание второго гейта ────
# ── PR #617): env-имена, которые задаёт шаг эскалации в deploy-worker.yml, и
# ── env-имена, которые ЧИТАЕТ main() в deploy_worker_rollback_alert.py, живут
# ── в двух файлах и ничем не связаны — переименование в одном месте молчит,
# ── а не падает (пустая строка → parse_bool_env=False → «АВТООТКАТ НЕ
# ── ПОДТВЕРЖДЁН» на каждый инцидент). Приём — тот же, что
# ── test_telegram_callback_format_sync.py: читаем оба исходника РЕАЛЬНО, не
# ── повторяем литерал в двух местах.


def _escalation_step():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["deploy"]["steps"]
    matches = [
        s for s in steps
        if "deploy_worker_rollback_alert.py" in str(s.get("run") or "")
    ]
    assert len(matches) == 1, "шаг эскалации (запуск deploy_worker_rollback_alert.py) не найден или задвоен"
    return matches[0]



# GITHUB_REPOSITORY/GITHUB_RUN_ID/GITHUB_SERVER_URL — стандартные переменные,
# которые раннер GitHub Actions прокидывает в КАЖДЫЙ шаг сам, без явного
# `env:` (docs: "Default environment variables"). Их отсутствие в блоке
# `env:` шага — не разрыв проводки, поэтому из проверки исключены; ровно
# ROLLBACK_CONFIRMED/POST_ROLLBACK_OK — прикладные имена, которые шаг обязан
# задать явно из outputs соседних шагов.
_RUNNER_DEFAULT_ENV = {"GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_SERVER_URL"}


def _main_env_keys() -> set[str]:
    """Имена, которые реально читает main() через os.environ/.get — не список,
    переписанный вручную рядом (тот класс расхождения и ловим)."""
    source = SCRIPT.read_text(encoding="utf-8")
    keys = set(re.findall(r'os\.environ\.get\(\s*["\'](\w+)["\']', source))
    keys |= set(re.findall(r'os\.environ\[\s*["\'](\w+)["\']\s*\]', source))
    return keys - _RUNNER_DEFAULT_ENV


def test_escalation_step_env_names_match_what_main_actually_reads():
    step = _escalation_step()
    env = step.get("env") or {}
    required = _main_env_keys()
    missing = required - set(env)
    assert not missing, (
        f"deploy-worker.yml: шаг эскалации не задаёт env {missing}, которые читает "
        f"main() в {SCRIPT.name} — main() увидит пустую строку и тихо решит "
        "'АВТООТКАТ НЕ ПОДТВЕРЖДЁН' вместо реального исхода"
    )


def test_rollback_confirmed_env_points_at_the_real_auto_rollback_step_id():
    step = _escalation_step()
    expr = str((step.get("env") or {}).get("ROLLBACK_CONFIRMED") or "")
    assert "steps.auto_rollback.outputs.rolled_back" in expr, (
        f"ROLLBACK_CONFIRMED больше не ссылается на steps.auto_rollback.outputs.rolled_back "
        f"(нашли: {expr!r}) — переименование шага отката смолчится тем же путём"
    )
    step_ids = {
        s.get("id")
        for s in yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["deploy"]["steps"]
        if s.get("id")
    }
    assert "auto_rollback" in step_ids, "шаг с id=auto_rollback пропал из workflow"


def test_post_rollback_ok_env_points_at_the_real_post_rollback_canary_step_id():
    step = _escalation_step()
    expr = str((step.get("env") or {}).get("POST_ROLLBACK_OK") or "")
    assert "steps.post_rollback_canary.outcome" in expr, (
        f"POST_ROLLBACK_OK больше не ссылается на steps.post_rollback_canary.outcome "
        f"(нашли: {expr!r}) — переименование шага повторной канарейки смолчится тем же путём"
    )
