#!/usr/bin/env python3
"""Гвардия #1170: шаг «Канарейка после автооткота» обязан падать (exit != 0),
когда прод после автоотката НЕ восстановился (HTTP-код root не 200) — не
только печатать `::error::` в лог.

Класс проблемы («Тормоз без газа», AGENTS.md): ветка `else` шага писала
аннотацию, но не завершала шаг ненулевым кодом — шаг оставался `success`
даже если восстановление прода после отката провалилось. Единственный
видимый сигнал был бы строкой в логе, которую легко принять за эхо
исходного текста шага (GitHub Actions печатает исходный код `run:` блока в
лог ДО его исполнения — сама строка `::error::...` присутствует в сыром
логе дважды: один раз как код, и только при реальном срабатывании — как
вывод).

Живой повод (не инцидент — false positive при чтении лога): прогон
34788480952 (2026-09-13T23:01Z) напечатал ✅ (прод ответил 200, автооткат
сработал верно) — в логе тем не менее видна строка `::error::...` как ЧАСТЬ
исходного текста шага, которую легко перепутать со сработавшей аннотацией.

Доказательство — поведенческое (класс #891/#893, не грепаем исходник, а
реально исполняем bash-фрагмент шага из workflow), не текстовый анализ:
`curl` подменяется стабом, возвращающим управляемый HTTP-код.

Запуск: python -m pytest scripts/lib/test_deploy_post_rollback_canary_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import os
import shutil
import stat
import subprocess

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / ".github" / "workflows" / "deploy-dsh-edge.yml"
STEP_NAME = "Канарейка после автооткота"


def _step_by_name(name: str) -> dict:
    assert DEPLOY.exists(), (
        f"{DEPLOY} исчез или переименован — обнови путь в гвардии "
        "сознательной правкой, а не молчаливым обходом"
    )
    with DEPLOY.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    for step in doc["jobs"]["deploy"]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(
        f"шаг {name!r} не найден в jobs.deploy.steps — переименовали? "
        "обнови гвардию сознательно"
    )


def _make_fake_curl(bin_dir: Path, http_code: str) -> None:
    """Стаб `curl`, печатающий заданный HTTP-код туда, куда просит `-w`,
    вне зависимости от аргументов вызова — реальный шаг вызывает curl ровно
    один раз, второй бинарник не нужен."""
    curl_path = bin_dir / "curl"
    curl_path.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s' '{http_code}'\n",
        encoding="utf-8",
    )
    curl_path.chmod(curl_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_canary_step(http_code: str, tmp_path: Path):
    """Исполняет РЕАЛЬНЫЙ bash-скрипт шага «Канарейка после автооткота» из
    workflow (не пересказ), подменив только `curl` фиктивным HTTP-кодом."""
    run_script = _step_by_name(STEP_NAME)["run"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _make_fake_curl(bin_dir, http_code)

    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")

    bash_path = shutil.which("bash") or "bash"
    proc = subprocess.run(
        [bash_path, "-c", run_script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_canary_step_succeeds_when_prod_answers_200(tmp_path):
    code, out = _run_canary_step("200", tmp_path)
    assert code == 0, f"ожидался успех при HTTP 200, получен код {code}:\n{out}"
    assert "восстановлен на прошлой версии" in out, (
        f"ожидалось сообщение об успешном восстановлении, вывод: {out!r}"
    )


def test_canary_step_fails_loud_when_prod_does_not_answer_200(tmp_path):
    """Живой класс #1170: без `exit 1` эта ветка была бы success с одной
    лишь аннотацией `::error::` в логе — неотличимой по статусу шага от
    нормального прогона."""
    code, out = _run_canary_step("503", tmp_path)
    assert code != 0, (
        "шаг обязан падать (exit != 0), когда прод после автоотката НЕ "
        f"отвечает 200 — иначе провал восстановления виден только в логе, "
        f"не в статусе шага/job (issue #1170); получен код {code}:\n{out}"
    )
    assert "автоматика не справилась" in out, (
        f"сообщение об ошибке обязано называть факт неудачи восстановления, вывод: {out!r}"
    )
