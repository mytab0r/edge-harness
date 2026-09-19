#!/usr/bin/env python3
"""Гвардия #1371: в `deploy-dsh-edge.yml` HTTP ходит только через
`scripts/lib/canary_http.sh`, голого `curl` в шагах нет.

Зачем отдельная гвардия к уже написанному фиксу («решение — это механизм, а
не текст», AGENTS.md). Перевод пяти канареек и снимка версии на библиотеку —
это починенный СЛУЧАЙ. Класс («шаг CI, знающий и код, и тело ответа, печатает
только код») закрыт только тогда, когда вернуть голый `curl` в этот файл
нельзя: без гвардии следующий агент, добавляя шестую канарейку, напишет
привычное `curl -fsS` — и диагностика снова ослепнет ровно там, где уже
ослепла однажды (прогон 35387441612).

Чем эта гвардия отличается от `test_canary_http_guard.py`: та держит
ПОВЕДЕНИЕ библиотеки (реальный сервер, реальный curl, реальный вывод), эта —
ЕДИНСТВЕННОСТЬ входа в неё. Свойство «в этом файле нет голого curl» текстовое
по своей природе: его нельзя доказать исполнением, потому что несуществующий
шаг не исполняется. Поэтому проверка читает файл — и это НЕ тот случай, от
которого предостерегает правило про ложно-зелёные структурные гвардии
(#891/#893): там структурная проверка подменяла собой проверку поведения,
здесь поведение проверено соседней гвардией, а эта закрывает вход.

Газ назван (AGENTS.md, «тормоз без газа не принимается»): если шагу
действительно нужен голый `curl` — вписать его в `ALLOWED_RAW_CURL` вместе с
причиной. Пустой список — не догма, а текущее состояние: сейчас ни одному
шагу это не нужно.

Запуск: python -m pytest scripts/lib/test_deploy_canary_curl_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import re

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / ".github" / "workflows" / "deploy-dsh-edge.yml"
LIB_RELATIVE = "scripts/lib/canary_http.sh"

# Шаги, которым голый `curl` разрешён, и ПОЧЕМУ. Ключ — значение `name` шага,
# значение — причина, которая пойдёт человеку в сообщение отказа гвардии.
# Пусто: на 2026-09-19 ни одному шагу этого файла голый curl не нужен.
ALLOWED_RAW_CURL: dict[str, str] = {}

# `curl` как СЛОВО: `canary_http`/`canary_probe` внутри себя зовут curl, но
# они живут в библиотеке, не в шаге. Подстрока поймала бы и «curl» в
# комментарии — это осознанно: комментарий, рассказывающий «тут был curl»,
# должен быть переписан вместе с кодом, а не пережить его.
_CURL_RE = re.compile(r"\bcurl\b")


def _steps() -> list[dict]:
    assert DEPLOY.exists(), (
        f"{DEPLOY} исчез или переименован — обнови путь в гвардии сознательной "
        "правкой, а не молчаливым обходом"
    )
    with DEPLOY.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    out: list[dict] = []
    for job_name, job in (doc.get("jobs") or {}).items():
        for step in (job.get("steps") or []):
            step = dict(step)
            step["_job"] = job_name
            out.append(step)
    assert out, "в deploy-dsh-edge.yml не разобрано ни одного шага — разбор сломан"
    return out


def test_no_raw_curl_in_deploy_steps():
    """Главное требование: вход в HTTP один."""
    offenders = []
    for step in _steps():
        script = step.get("run") or ""
        if not _CURL_RE.search(script):
            continue
        name = step.get("name") or "<без имени>"
        if name in ALLOWED_RAW_CURL:
            continue
        offenders.append(f"{step['_job']} / {name}")

    assert not offenders, (
        "голый curl в шагах deploy-dsh-edge.yml: " + ", ".join(offenders) + ". "
        f"HTTP в этом файле ходит через {LIB_RELATIVE}: canary_http (успех → "
        "тело в stdout) или canary_probe <метка> <ожидаемый-код> (в stdout "
        "только код). Причина — issue #1371: `curl -f` выбрасывает тело "
        "ответа, и отказ канарейки приходит без причины. Газ: если голый curl "
        "шагу действительно нужен — впиши имя шага и причину в "
        "ALLOWED_RAW_CURL этой гвардии, чтобы исключение было названо, а не "
        "молчаливо"
    )


def test_every_step_using_the_library_sources_it_before_first_call():
    """Порядок, а не только наличие: `source` обязан стоять ДО первого вызова.
    Живой повод — собственная ошибка первой итерации PR #1372: три шага звали
    `canary_probe` строкой выше `source`, и в реальном прогоне упали бы на
    «command not found». Проверка дешёвая, класс ошибки — нет."""
    broken = []
    for step in _steps():
        script = step.get("run") or ""
        call = re.search(r"\bcanary_(?:http|probe)\b", script)
        if call is None:
            continue
        source = re.search(re.escape(LIB_RELATIVE), script)
        name = step.get("name") or "<без имени>"
        if source is None:
            broken.append(f"{step['_job']} / {name}: вызов есть, source библиотеки отсутствует")
        elif source.start() > call.start():
            broken.append(f"{step['_job']} / {name}: source стоит ПОСЛЕ первого вызова")

    assert not broken, (
        "шаги, которые упадут в прогоне на «canary_http: command not found»: "
        + "; ".join(broken)
        + f". Строка `source \"$GITHUB_WORKSPACE/{LIB_RELATIVE}\"` обязана стоять "
        "до первого вызова функции библиотеки"
    )


def test_library_is_sourced_by_workspace_path_not_by_relative_guess():
    """`$GITHUB_WORKSPACE` — единственный путь, который job гарантирует.
    Относительный (`./scripts/...`) зависел бы от `working-directory` шага и
    сломался бы молча при её смене — ровно класс silent-wrong."""
    wrong = []
    for step in _steps():
        script = step.get("run") or ""
        for line in script.splitlines():
            if LIB_RELATIVE not in line or "source" not in line:
                continue
            if "$GITHUB_WORKSPACE" not in line:
                name = step.get("name") or "<без имени>"
                wrong.append(f"{step['_job']} / {name}: {line.strip()}")

    assert not wrong, (
        "библиотека подключается не по $GITHUB_WORKSPACE: " + "; ".join(wrong)
        + ". Относительный путь зависит от working-directory шага и сломается "
        "молча при её смене"
    )
