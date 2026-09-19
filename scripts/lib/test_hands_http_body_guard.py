#!/usr/bin/env python3
"""Гвардия #1373: клиенты рук и воркера печатают ТЕЛО ответа при отказе.

Класс тот же, что #1371 закрыл для канареек деплоя: шаг, знающий и код, и
тело ответа, не имеет права печатать только код. Поверхность другая — руки и
воркер, — и там он жил своей жизнью: `curl -fsS` выбрасывал тело на каждом
не-2xx, а в `scripts/lib/dsh-edge-session.sh` рядом успела завестись ВТОРАЯ,
рукописная копия разбора кода и тела (с тем же комментарием «БЕЗ curl -f»).
Две копии одного правила расходятся молча — поэтому обе сведены в
`scripts/lib/canary_http.sh`.

Проверка ПОВЕДЕНЧЕСКАЯ: поднимается настоящий HTTP-сервер, `journal_post_event`
из `scripts/lib/journal_status.sh` зовётся как есть (не пересказ), и читается
настоящий stderr. Структурная проверка исходника доказала бы орфографию, а не
поведение (класс #891/#893) — она здесь тоже есть, но ВТОРЫМ рубежом: она
закрывает вход (нельзя вернуть голый `curl -f`), а не подменяет поведение.

Тело отказа взято в прод-форме журнала — `cf-worker/src/harness.ts:756`,
`JSON.stringify({ error: { code, message } })`, — а не придумано.

Запуск: python -m pytest scripts/lib/test_hands_http_body_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Прод-форма отказа журнала: ровно то, что отдаёт ApiError в
# cf-worker/src/harness.ts:756.
JOURNAL_ERROR_BODY = {"error": {"code": "unauthorized", "message": "owner token required"}}

# Голый `curl -f` разрешён ТОЛЬКО там, где тело ответа не диагностика, а
# мусор, который иначе ляжет на диск вместо артефакта. Ключ — путь, значение —
# причина, которая уйдёт человеку в текст отказа гвардии.
ALLOWED_FAIL_FLAG = {
    "scripts/lib/dsh-ci.sh": (
        "скачивание релизных ассетов в файл (-o): без -f HTML-страница 404 легла бы "
        "на диск как .tgz и упала бы позже на несовпадении sha256 — причина отказа "
        "стала бы ДАЛЬШЕ от места отказа, а не ближе"
    ),
}

_CURL_FAIL_RE = re.compile(r"curl\s+-[a-zA-Z]*f")


class _Handler(BaseHTTPRequestHandler):
    status = 503
    payload = json.dumps(JOURNAL_ERROR_BODY).encode("utf-8")

    def _respond(self):
        self.send_response(type(self).status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(type(self).payload)))
        self.end_headers()
        self.wfile.write(type(self).payload)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, *args):
        pass


@pytest.fixture
def journal():
    started = []

    def factory(status: int, payload: bytes):
        handler = type("Bound", (_Handler,), {"status": status, "payload": payload})
        httpd = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_port}"

    yield factory
    for httpd in started:
        httpd.shutdown()


def _run_journal_status(url: str, tmp_path: Path, final: str = "0"):
    """Зовёт НАСТОЯЩИЙ journal_post_event против стаб-журнала.

    `sleep` глушится намеренно и только он: цикл ретраев остаётся полным (пять
    попыток), но тест не ждёт двадцать секунд реального времени. Подменять
    что-то ещё нельзя — иначе проверялся бы не тот код.
    """
    script = (
        f'set -uo pipefail\n'
        f'sleep() {{ :; }}\n'
        f'source "{REPO_ROOT}/scripts/lib/journal_status.sh"\n'
        f'TASK_ID=issue-1 KIND=plugin_status DATA_JSON=\'{{"state":"ready"}}\' '
        f'SOURCE=deploy FINAL={final} '
        f'HARNESS_URL="{url}" HANDS_TOKEN=stub-token journal_post_event\n'
        f'echo "RC=$?"\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          encoding="utf-8", cwd=tmp_path, timeout=120)


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_journal_failure_prints_code_and_body(journal, tmp_path):
    """Главное требование: причина отказа журнала видна, а не выброшена."""
    url = journal(503, json.dumps(JOURNAL_ERROR_BODY).encode("utf-8"))
    result = _run_journal_status(url, tmp_path)

    assert "HTTP 503" in result.stderr, result.stderr
    assert "owner token required" in result.stderr, (
        "тело ответа обязано доехать до лога — ровно его выбрасывал curl -f "
        f"(#1371/#1373); вывод: {result.stderr!r}"
    )
    assert "Журнал" in result.stderr, (
        f"метка вызова обязана называть, ЧТО именно отказало: {result.stderr!r}"
    )


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_journal_failure_is_warning_not_error_inside_retry_loop(journal, tmp_path):
    """Повторяемая попытка не имеет права кричать `::error::`.

    Обёртки зовутся внутри цикла ретраев: `::error::` на каждой попытке
    заставил бы читателя гадать, сломалось ли что-то, хотя следующая попытка
    может пройти. Итоговый отказ красит вызывающий код своим сообщением —
    его и проверяем отдельно ниже.
    """
    url = journal(503, json.dumps(JOURNAL_ERROR_BODY).encode("utf-8"))
    result = _run_journal_status(url, tmp_path, final="0")

    wrapper_lines = [line for line in result.stderr.splitlines() if "Журнал " in line]
    assert wrapper_lines, result.stderr
    assert all(line.startswith("::warning::") for line in wrapper_lines), (
        "внутри цикла ретраев обёртка обязана быть warning, а не error: "
        f"{wrapper_lines!r}"
    )


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_final_status_still_fails_loud_when_journal_is_down(journal, tmp_path):
    """Понижение уровня обёртки не имеет права утащить за собой fail loud:
    финальный статус, не доехавший до журнала, обязан красить job."""
    url = journal(503, json.dumps(JOURNAL_ERROR_BODY).encode("utf-8"))
    result = _run_journal_status(url, tmp_path, final="1")

    assert "RC=1" in result.stdout, (
        "финальный статус обязан вернуть 1 — деплой без статуса это silent-wrong; "
        f"stdout: {result.stdout!r}, stderr: {result.stderr!r}"
    )
    assert "::error::" in result.stderr, result.stderr


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_healthy_journal_stays_quiet(journal, tmp_path):
    """На исправном журнале обёртки не шумят — иначе лог деплоя утонет."""
    ok_body = json.dumps({"events": [{"seq": 7}], "has_more": False, "next_after": 7})
    url = journal(200, ok_body.encode("utf-8"))
    result = _run_journal_status(url, tmp_path)

    assert "::warning::Журнал" not in result.stderr, result.stderr
    assert "::error::" not in result.stderr, result.stderr


def test_no_bare_fail_flag_outside_allowlist():
    """Вход закрыт: вернуть `curl -f` в клиенты рук нельзя молча.

    Это ВТОРОЙ рубеж поверх поведенческого выше, а не замена ему: свойство
    «голого curl -f здесь нет» исполнением не доказывается — несуществующий
    вызов не исполняется. Газ назван: если новому месту флаг действительно
    нужен, впиши путь и причину в ALLOWED_FAIL_FLAG.
    """
    offenders = []
    for area in ("scripts/hands", "scripts/worker", "scripts/lib", "scripts/plugins"):
        for path in sorted((REPO_ROOT / area).rglob("*.sh")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in ALLOWED_FAIL_FLAG:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue  # комментарий, рассказывающий про класс, — не вызов
                if _CURL_FAIL_RE.search(line):
                    offenders.append(f"{rel}:{number}: {stripped}")

    assert not offenders, (
        "голый curl с флагом -f (--fail) в клиентах рук/воркера:\n  "
        + "\n  ".join(offenders)
        + "\nОн выбрасывает тело ответа на не-2xx, и отказ приходит без причины "
        "(#1371/#1373). Ходи через scripts/lib/canary_http.sh: canary_http "
        "(успех → тело в stdout) или canary_probe <метка> <ожидаемый-код>. "
        "Газ: если флаг действительно нужен — впиши путь и причину в "
        "ALLOWED_FAIL_FLAG этой гвардии, чтобы исключение было названо."
    )


def test_allowlist_entries_still_exist_and_still_need_the_flag():
    """Разрешение не переживает свою причину: путь из ALLOWED_FAIL_FLAG обязан
    существовать и обязан всё ещё содержать флаг. Иначе запись — мусор,
    молча разрешающий то, чего давно нет."""
    for rel, reason in ALLOWED_FAIL_FLAG.items():
        path = REPO_ROOT / rel
        assert path.exists(), f"ALLOWED_FAIL_FLAG указывает на исчезнувший {rel} — удали запись"
        assert _CURL_FAIL_RE.search(path.read_text(encoding="utf-8")), (
            f"{rel} больше не содержит curl -f — разрешение («{reason}») стало "
            "мусором, удали запись из ALLOWED_FAIL_FLAG"
        )
