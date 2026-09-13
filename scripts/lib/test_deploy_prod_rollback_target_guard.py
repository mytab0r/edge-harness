#!/usr/bin/env python3
"""Гвардия #1123 (находка 1 из #1121): автооткат прода обязан откатываться
на последнюю ИЗВЕСТНО-ХОРОШУЮ версию, а не на «предыдущую по created_on».

Класс проблемы: `wrangler rollback` без явного version-id (апстрим,
packages/wrangler/src/versions/rollback/index.ts::fetchDefaultRollbackVersionId)
сортирует ВСЕ деплои по created_on, отбрасывает самый свежий и берёт первый
оставшийся с percentage:100. `wrangler secret put` (девять вызовов после
`wrangler deploy`, тот же job) создаёт по новой 100%-версии на каждый секрет,
поэтому «предыдущая версия» после красной канарейки — версия ЭТОГО ЖЕ прогона
на пару секунд старше деплоя, не прошлый релиз. Живая улика: run 34750000094 —
деплой создал версию 4fe52372 в 09:42:19, секрет GH_RUNNER_TOKEN — версию
ffcfd8b4 в 09:42:28, откат (09:44:44) уехал на ffcfd8b4. Шаг при этом печатал
«has been deployed to 100% of traffic» — ложное подтверждение.

Фикс: захват ID активной 100%-версии ДО деплоя этого прогона (шаг
«Снимок версии прода до деплоя»), передача его `wrangler rollback` ЯВНЫМ
аргументом, и сверка ФАКТИЧЕСКИ раскатанной версии с запрошенной — не веря
одной лишь строке «deployed to 100% of traffic».

Правила:

  1. Шаг «Снимок версии прода до деплоя» (id: pre_deploy_version) идёт РАНЬШЕ
     шага «Деплой» (id: deploy) в списке шагов job'а — иначе снимок берётся
     уже ПОСЛЕ того, как этот прогон начал что-то мутировать, и защита не
     закрывает ровно ту дыру, для которой заведена.
  2. jq-выбор версии в снимке — ПО ПРИЗНАКУ (последняя по created_on среди
     100%-деплоев), не по индексу массива: доказывается исполнением РЕАЛЬНОГО
     jq-фрагмента из workflow против сфабрикованного JSON, где самый свежий
     деплой НЕ первый в массиве.
  3. Шаг «Автооткат прода» передаёт version-id ЯВНО в `wrangler rollback`
     (не голый `wrangler rollback --yes`, который вернул бы старую эвристику
     апстрима).
  4. Шаг «Автооткат прода» ИСПОЛНЯЕТСЯ (не пересказывается) против сфабрико-
     ванного лога отката: совпадение раскатанной версии с запрошенной →
     успех и `rolled_back=true`; расхождение → explicit `exit 1` и НЕ
     `rolled_back=true` (класс: раньше шаг верил только строке «deployed to
     100% of traffic», которая печаталась и при откате не туда).
  5. Пустой TARGET_VERSION_ID (снимок не смог/первый деплой воркера) —
     явный отказ ДО вызова wrangler, а не тихий проброс в старую эвристику.

Запуск: python -m pytest scripts/lib/test_deploy_prod_rollback_target_guard.py -q
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
import re
import shutil
import stat
import subprocess
import tempfile

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / ".github" / "workflows" / "deploy-dsh-edge.yml"


def _job_steps() -> list:
    assert DEPLOY.exists(), (
        f"{DEPLOY} исчез или переименован — обнови путь в гвардии "
        "сознательной правкой, а не молчаливым обходом"
    )
    with DEPLOY.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    return doc["jobs"]["deploy"]["steps"]


def _step_by_id(step_id: str) -> dict:
    for step in _job_steps():
        if step.get("id") == step_id:
            return step
    raise AssertionError(
        f"шаг с id={step_id!r} не найден в jobs.deploy.steps — "
        "переименовали id? обнови гвардию сознательно"
    )


def test_snapshot_step_runs_before_deploy_step():
    """Правило 1: снимок версии обязан идти РАНЬШЕ `wrangler deploy` этого
    прогона — иначе он видит уже собственную (только что созданную) версию,
    и вся защита схлопывается в тот же класс бага, что и раньше."""
    steps = _job_steps()
    ids = [s.get("id") for s in steps]
    assert "pre_deploy_version" in ids, (
        "шаг снимка версии до деплоя (id: pre_deploy_version) отсутствует"
    )
    assert "deploy" in ids, "шаг деплоя (id: deploy) отсутствует"
    assert ids.index("pre_deploy_version") < ids.index("deploy"), (
        "шаг «Снимок версии прода до деплоя» обязан идти РАНЬШЕ шага «Деплой» "
        "в списке шагов job'а — иначе снимок берётся уже после того, как этот "
        "прогон начал мутировать версии, и это тот же класс бага (#1121, находка 1)"
    )


def _pre_deploy_jq_program() -> str:
    run = _step_by_id("pre_deploy_version")["run"]
    m = re.search(r"jq -r '(.*?)' /tmp/pre-deploy-deployments\.json", run, re.S)
    assert m, (
        "не нашёл jq-программу выбора версии в шаге pre_deploy_version — "
        "изменилась форма вызова, обнови гвардию сознательно"
    )
    return m.group(1)


def _run_jq(program: str, fixture: dict) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(fixture, f)
        path = f.name
    try:
        proc = subprocess.run(
            ["jq", "-r", program, path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    finally:
        os.unlink(path)
    assert proc.returncode == 0, (
        f"jq упал (код {proc.returncode}) на программе из pre_deploy_version:\n"
        f"{program}\nstderr: {proc.stderr}"
    )
    return proc.stdout.strip()


def test_jq_picks_latest_100pct_by_created_on_not_by_array_index():
    """Правило 2 (доказательство МУТАЦИЕЙ поведения, класс #891/#893 — не
    грепаем исходник, реально исполняем jq-фрагмент из workflow).

    Фикстура нарочно кладёт САМЫЙ СВЕЖИЙ 100%-деплой НЕ первым элементом
    массива: наивный выбор «по индексу» (.[0]) взял бы старую версию;
    правильный выбор «по признаку created_on» обязан вернуть свежую."""
    program = _pre_deploy_jq_program()
    fixture = {
        "result": {
            "deployments": [
                {
                    "created_on": "2026-09-10T08:00:00Z",
                    "versions": [{"version_id": "old-real-prev-version", "percentage": 100}],
                },
                {
                    "created_on": "2026-09-13T09:42:19Z",
                    "versions": [{"version_id": "newest-active-version", "percentage": 100}],
                },
                {
                    "created_on": "2026-09-05T00:00:00Z",
                    "versions": [{"version_id": "ancient-version", "percentage": 100}],
                },
            ]
        }
    }
    assert _run_jq(program, fixture) == "newest-active-version", (
        "jq обязан выбирать версию с максимальным created_on среди "
        "percentage:100, а не первый элемент массива по порядку — иначе "
        "выбор снова «по индексу», а не «по признаку» (см. AGENTS.md: "
        "«Реши класс, не случай»)"
    )


def test_jq_ignores_partial_rollout_percentages():
    """Правило 2, дополнение: деплой с percentage < 100 (частичный роллаут)
    не должен приниматься за «стабильную» версию, даже если он свежее."""
    program = _pre_deploy_jq_program()
    fixture = {
        "result": {
            "deployments": [
                {
                    "created_on": "2026-09-10T08:00:00Z",
                    "versions": [{"version_id": "last-stable-100pct", "percentage": 100}],
                },
                {
                    "created_on": "2026-09-13T09:00:00Z",
                    "versions": [{"version_id": "gradual-rollout-partial", "percentage": 10}],
                },
            ]
        }
    }
    assert _run_jq(program, fixture) == "last-stable-100pct", (
        "частичный роллаут (percentage < 100) не должен выбираться как цель "
        "отката, даже если он создан позже последней 100%-версии"
    )


def test_jq_returns_empty_on_no_prior_deployments():
    """Правило 5 (первый деплой воркера вообще): пустой список деплоев —
    пустой результат, не ошибка jq и не произвольная строка."""
    program = _pre_deploy_jq_program()
    fixture = {"result": {"deployments": []}}
    assert _run_jq(program, fixture) == "", (
        "на пустом списке деплоев (первый деплой воркера) jq обязан вернуть "
        "пусто — вызывающий шаг сам решает, что делать с отсутствием версии"
    )


def test_rollback_step_passes_explicit_version_id():
    """Правило 3: явный version-id обязателен — иначе мы вернулись к "
    "эвристике апстрима, которая и была причиной бага."""
    run = _step_by_id("auto_rollback")["run"]
    assert 'wrangler rollback "$TARGET_VERSION_ID"' in run, (
        "шаг автооткота обязан вызывать `wrangler rollback \"$TARGET_VERSION_ID\"` "
        "с ЯВНЫМ version-id — голый `wrangler rollback --yes` снова доверяет "
        "эвристике апстрима (created_on минус самый свежий), которая и "
        "откатывала прод на версию этого же прогона (#1121, находка 1)"
    )
    assert "steps.pre_deploy_version.outputs.version_id" in _step_by_id("auto_rollback")["env"].get(
        "TARGET_VERSION_ID", ""
    ), (
        "TARGET_VERSION_ID обязан браться из захваченного ДО деплоя снимка "
        "(steps.pre_deploy_version.outputs.version_id), не из другого места"
    )


def _make_fake_npx(bin_dir: Path, rollback_log_line: str) -> None:
    """Стаб `npx`, который вместо реального wrangler печатает каноническую
    строку успеха апстрима с заданным version-id — ровно то, что реальный
    `wrangler rollback` печатает в конце своей работы."""
    npx_path = bin_dir / "npx"
    npx_path.write_text(
        "#!/usr/bin/env bash\n"
        "echo '├ Finding latest stable Worker Version to rollback to'\n"
        f"echo '{rollback_log_line}'\n",
        encoding="utf-8",
    )
    npx_path.chmod(npx_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_rollback_step(target_version_id: str, rollback_log_line: str, tmp_path: Path):
    """Исполняет РЕАЛЬНЫЙ (не пересказанный) bash-скрипт шага auto_rollback,
    подменив только `npx` фиктивным выводом, и возвращает (returncode,
    stdout+stderr, содержимое GITHUB_OUTPUT)."""
    run_script = _step_by_id("auto_rollback")["run"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if rollback_log_line is not None:
        _make_fake_npx(bin_dir, rollback_log_line)
    else:
        # Ветка «TARGET_VERSION_ID пуст» обязана упасть ДО вызова npx —
        # если тест-стаб всё же позвался, значит шаг не защитился.
        npx_path = bin_dir / "npx"
        npx_path.write_text(
            "#!/usr/bin/env bash\necho 'npx НЕ должен был вызываться при пустом TARGET_VERSION_ID' >&2\nexit 99\n",
            encoding="utf-8",
        )
        npx_path.chmod(npx_path.stat().st_mode | stat.S_IEXEC)

    github_output = tmp_path / "github_output.txt"
    github_output.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["TARGET_VERSION_ID"] = target_version_id
    env["GITHUB_OUTPUT"] = str(github_output)
    # Плейсхолдер run_id из YAML-выражения ${{ github.run_id }} — в реальном
    # прогоне подставляется GitHub Actions ДО того, как bash увидит скрипт;
    # здесь подставляем руками, чтобы исполнить дословно тот же bash.
    script = run_script.replace("${{ github.run_id }}", "TEST_RUN_ID")

    # Явный абсолютный путь к bash, не голое имя "bash": на Windows порядок
    # поиска CreateProcess проверяет System32 РАНЬШЕ PATH, а System32\bash.exe
    # — заглушка WSL (без настроенного дистрибутива здесь), не Git bash;
    # `shutil.which` читает PATH напрямую и находит правильный бинарник
    # (проверено живьём: без этого тест валился на "execvpe(/bin/bash)
    # failed", а не на логике самого шага).
    bash_path = shutil.which("bash") or "bash"
    proc = subprocess.run(
        [bash_path, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    output_content = github_output.read_text(encoding="utf-8")
    return proc.returncode, proc.stdout + proc.stderr, output_content


# Реальные Worker Version ID — UUID (пример из живого прогона 34750000094:
# ffcfd8b4-3b11-48f5-8ecf-f0250c1603a9), не человекочитаемые слова — сама
# гвардия шага (grep -oiE "[0-9a-fA-F]{8}-...") распознаёт только эту форму,
# поэтому фикстуры ниже несут настоящие UUID, не пересказ.
_KNOWN_GOOD_UUID = "aaaaaaaa-1111-4222-8333-444444444444"
_WRONG_UUID_FROM_THIS_RUN = "ffcfd8b4-3b11-48f5-8ecf-f0250c1603a9"


def test_rollback_step_succeeds_when_deployed_version_matches_target(tmp_path):
    """Правило 4, зелёная ветка: раскатанная версия == запрошенная."""
    code, out, gh_output = _run_rollback_step(
        target_version_id=_KNOWN_GOOD_UUID,
        rollback_log_line=(
            f"╰  SUCCESS  Worker Version {_KNOWN_GOOD_UUID} has been "
            "deployed to 100% of traffic."
        ),
        tmp_path=tmp_path,
    )
    assert code == 0, f"ожидался успех, получен код {code}:\n{out}"
    assert "rolled_back=true" in gh_output, (
        f"GITHUB_OUTPUT обязан нести rolled_back=true при совпадении версий, получено: {gh_output!r}"
    )


def test_rollback_step_fails_loud_when_deployed_version_mismatches_target(tmp_path):
    """Правило 4, красная ветка — ЭТО и есть воспроизведение живого бага
    #1121 находки 1 (буквальный UUID из run 34750000094): просили
    _KNOWN_GOOD_UUID, а wrangler (в реальности — по старой эвристике; здесь —
    смоделировано стабом) раскатал _WRONG_UUID_FROM_THIS_RUN. Шаг обязан
    упасть и НЕ писать rolled_back=true, а не рапортовать ложный успех."""
    code, out, gh_output = _run_rollback_step(
        target_version_id=_KNOWN_GOOD_UUID,
        rollback_log_line=(
            f"╰  SUCCESS  Worker Version {_WRONG_UUID_FROM_THIS_RUN} has been "
            "deployed to 100% of traffic."
        ),
        tmp_path=tmp_path,
    )
    assert code != 0, (
        f"шаг обязан упасть, когда раскатанная версия НЕ совпадает с запрошенной "
        f"(живой класс #1121, находка 1), получен код {code}:\n{out}"
    )
    assert "rolled_back=true" not in gh_output, (
        f"откат не туда НЕ должен писать rolled_back=true в GITHUB_OUTPUT, получено: {gh_output!r}"
    )
    assert "не на ту версию" in out, (
        f"сообщение об ошибке обязано называть факт расхождения версий, вывод: {out!r}"
    )


def test_rollback_step_refuses_without_captured_target(tmp_path):
    """Правило 5: пустой TARGET_VERSION_ID — явный отказ ДО вызова npx, не
    тихий проброс дальше в старую эвристику апстрима."""
    code, out, gh_output = _run_rollback_step(
        target_version_id="",
        rollback_log_line=None,
        tmp_path=tmp_path,
    )
    assert code != 0, f"пустой TARGET_VERSION_ID обязан провалить шаг, получен код {code}:\n{out}"
    assert "НЕ должен был вызываться" not in out, (
        "npx не должен вызываться вовсе при пустом TARGET_VERSION_ID — "
        f"стаб зафиксировал вызов:\n{out}"
    )
    assert "rolled_back=true" not in gh_output


# Мутации, которыми доказана гвардия (каждая — красный тест, откат — зелёный):
#
#   М1 (правило 1): переставить шаг pre_deploy_version ПОСЛЕ шага deploy —
#     красен test_snapshot_step_runs_before_deploy_step.
#   М2 (правило 2): вернуть в jq наивный `.[0]` вместо `sort_by(.created_on)
#     | reverse | .[0]` — красен test_jq_picks_latest_100pct_by_created_on_not_by_array_index.
#   М3 (правило 3): убрать фильтр `percentage == 100` — красен
#     test_jq_ignores_partial_rollout_percentages.
#   М4 (правило 3): заменить `wrangler rollback "$TARGET_VERSION_ID"` на
#     голый `wrangler rollback --yes` — красен
#     test_rollback_step_passes_explicit_version_id.
#   М5 (правило 4): убрать сверку deployed_id != TARGET_VERSION_ID (доверять
#     только "deployed to 100% of traffic") — красен
#     test_rollback_step_fails_loud_when_deployed_version_mismatches_target
#     (воспроизводит живой баг #1121 находки 1 буквально).
#   М6 (правило 5): убрать проверку `[ -z "$TARGET_VERSION_ID" ]` — красен
#     test_rollback_step_refuses_without_captured_target.
