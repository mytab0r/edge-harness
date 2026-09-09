#!/usr/bin/env python3
"""Тесты scripts/gh/wake_orchestra.sh (#456).

Класс: pr-review.yml/ai-review.yml дёргали `gh workflow run orchestra.yml`
безусловно после каждого своего прогона — 35.6% из 500 продиспатченных
прогонов orchestra (замер #456) так и не стартовали (concurrency-группа
`orchestra` держит только один прогон в очереди на группу). Скрипт экономит
диспатч, только когда он заведомо избыточен (прогон уже идёт/ждёт очереди —
следующий старт увидит то же событие), и не трогает случай ADR 0012
(диспатч обязан оставаться безусловным по СОБЫТИЮ) — единственное отличие
от прежнего поведения: НЕ дублировать диспатч, когда он ничего не изменит.

Фейковый `gh` — исполняемый скрипт на PATH, который по аргументам решает,
что вернуть (`api ...status=X...` → JSON с 0/1 прогоном; `workflow run` —
пишет факт вызова в лог-файл, чтобы тест мог его проверить).

Запуск: python -m pytest scripts/gh/test_wake_orchestra.py -q
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).with_name("wake_orchestra.sh")
REPO = "o/r"

# shutil.which (не голое "bash" в subprocess.run) — на части Windows-машин
# в PATH раньше резолвится WSL-обёртка bash.exe, которая падает на
# execvpe(/bin/bash) в сломанном WSL-дистрибутиве (тот же класс дефекта
# среды, что test_redact_* в scripts/review/test_ai_review.py); shutil.which
# находит настоящий git-bash. На Linux CI оба пути совпадают.
BASH = shutil.which("bash") or "bash"


def make_fake_gh(tmp_path: Path, *, active_status: str | None, api_fails: bool = False) -> tuple[Path, Path]:
    """Пишет фейковый `gh` в tmp_path/bin/gh. active_status — "in_progress",
    "queued" или None (нет активных прогонов вовсе). api_fails — симулирует
    сетевой/лимитный сбой самого `gh api` (не пустой ответ, а НЕудачу вызова).
    Возвращает (bin_dir, dispatch_log)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dispatch_log = tmp_path / "dispatch_calls.log"
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(f"""#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "api" ]; then
  if {"true" if api_fails else "false"}; then
    echo "gh: rate limit exceeded" >&2
    exit 1
  fi
  case "$2" in
    *status=in_progress*)
      if [ "{active_status}" = "in_progress" ]; then
        echo '{{"workflow_runs": [{{"id": 1}}]}}'
      else
        echo '{{"workflow_runs": []}}'
      fi
      ;;
    *status=queued*)
      if [ "{active_status}" = "queued" ]; then
        echo '{{"workflow_runs": [{{"id": 1}}]}}'
      else
        echo '{{"workflow_runs": []}}'
      fi
      ;;
    *)
      echo '{{"workflow_runs": []}}'
      ;;
  esac
  exit 0
fi
if [ "$1" = "workflow" ] && [ "$2" = "run" ]; then
  echo "dispatched $*" >> "{dispatch_log}"
  exit 0
fi
echo "фейковый gh не знает команду: $*" >&2
exit 1
""", encoding="utf-8")
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir, dispatch_log


def run_wake(tmp_path: Path, *, active_status: str | None, api_fails: bool = False) -> subprocess.CompletedProcess:
    bin_dir, dispatch_log = make_fake_gh(tmp_path, active_status=active_status, api_fails=api_fails)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    result = subprocess.run(
        [BASH, str(SCRIPT), REPO],
        capture_output=True, text=True, encoding="utf-8", env=env,
    )
    result.dispatch_calls = dispatch_log.read_text(encoding="utf-8").splitlines() if dispatch_log.exists() else []
    return result


def test_skips_dispatch_when_orchestra_already_in_progress(tmp_path):
    result = run_wake(tmp_path, active_status="in_progress")
    assert result.returncode == 0, result.stderr
    assert result.dispatch_calls == [], "прогон уже идёт — диспатч должен быть пропущен"
    assert "уже in_progress" in result.stdout


def test_skips_dispatch_when_orchestra_already_queued(tmp_path):
    result = run_wake(tmp_path, active_status="queued")
    assert result.returncode == 0, result.stderr
    assert result.dispatch_calls == [], "прогон уже в очереди — диспатч должен быть пропущен"
    assert "уже queued" in result.stdout


def test_dispatches_when_nothing_active(tmp_path):
    result = run_wake(tmp_path, active_status=None)
    assert result.returncode == 0, result.stderr
    assert len(result.dispatch_calls) == 1, "нет активного прогона — диспатч обязан случиться (ADR 0012)"
    assert "orchestra.yml" in result.dispatch_calls[0]


def test_falls_back_to_dispatch_when_check_itself_fails(tmp_path):
    """Газ: сбой самой проверки (сеть/лимит) не должен молча съедать событие —
    fail-safe обязан диспатчить, как будто проверки не было вовсе (ADR 0012)."""
    result = run_wake(tmp_path, active_status=None, api_fails=True)
    assert result.returncode == 0, result.stderr
    assert len(result.dispatch_calls) == 1, "сбой проверки обязан диспатчить, не молчать"
    assert "::warning::" in result.stdout


def test_workflows_call_shared_script_not_raw_dispatch():
    """Регрессия: обе точки вызова (#456) обязаны идти через один и тот же
    скрипт, а не звать `gh workflow run orchestra.yml` напрямую — иначе
    экономия диспатча реализована только в одном из двух мест."""
    repo_root = Path(__file__).resolve().parents[2]
    for name in ("pr-review.yml", "ai-review.yml"):
        text = (repo_root / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "scripts/gh/wake_orchestra.sh" in text, f"{name}: не вызывает wake_orchestra.sh"
        assert "run: gh workflow run orchestra.yml" not in text, (
            f"{name}: всё ещё зовёт gh workflow run orchestra.yml напрямую — "
            "экономия диспатча (#456) обойдена"
        )
