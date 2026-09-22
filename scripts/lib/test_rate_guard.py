#!/usr/bin/env python3
"""Тесты rate_guard.py (#454) — гейт квоты перед дорогими job'ами.

Фикстура REAL_RATE_LIMIT_RESPONSE — дословный вывод `gh api rate_limit`,
захваченный живым вызовом 2026-09-06 (не пересказ формата, прод-форма,
AGENTS.md «тест кормит прод-форму данных»).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rate_guard as rg  # noqa: E402

# Дословный `gh api rate_limit`, живой вызов 2026-09-06 (PAT с полным лимитом
# 5000 — форма ответа для GITHUB_TOKEN идентична, только limit/remaining
# другие числа: 1000/час, см. docs/research/21-github-actions.md).
REAL_RATE_LIMIT_RESPONSE = json.dumps({
    "resources": {
        "core": {"limit": 5000, "used": 0, "remaining": 5000, "reset": 1788676450},
        "search": {"limit": 30, "used": 0, "remaining": 30, "reset": 1788672910},
        "graphql": {"limit": 5000, "used": 0, "remaining": 5000, "reset": 1788676450},
    },
    "rate": {"limit": 5000, "used": 0, "remaining": 5000, "reset": 1788676450},
})


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_gh(monkeypatch, *, stdout="", returncode=0, stderr=""):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Result(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(rg.subprocess, "run", fake_run)
    return calls


# Дословный ответ `gh api -i repos/mytab0r/edge-harness`, живой вызов
# 2026-09-22 10:35:01 UTC. Прод-форма, не пересказ: заголовки в том же
# регистре и порядке, которые реально пришли. Честная оговорка о
# происхождении: снят из агентской сессии (PAT, лимит 15000), не из job'а
# GitHub Actions — значения чисел там будут другие, ФОРМА та же. Ровно на
# этом различии и стоит вся задача #1437, поэтому оно названо здесь, а не
# подразумевается.
REAL_RESPONSE_WITH_HEADERS = """HTTP/1.1 200 OK
Cache-Control: private, max-age=60, s-maxage=60
Content-Type: application/json; charset=utf-8
Date: Tue, 22 Sep 2026 10:35:01 GMT
Server: github.com
X-Accepted-Github-Permissions: metadata=read
X-Github-Api-Version-Selected: 2022-11-28
X-Ratelimit-Limit: 15000
X-Ratelimit-Remaining: 14592
X-Ratelimit-Reset: 1790075485
X-Ratelimit-Resource: core
X-Ratelimit-Used: 408

{"id":1,"full_name":"mytab0r/edge-harness"}
"""


def _patch_gh_two_sources(monkeypatch, *, rate_limit_stdout, headers_stdout,
                          headers_returncode=0, headers_stderr=""):
    """Стенд под ДВА разных вызова: `gh api rate_limit` и `gh api -i repos/...`.

    Прежний `_patch_gh` отдавал один и тот же stdout на любой вызов — для
    гейта, у которого источников остатка стало два и они РАСХОДЯТСЯ (#1437),
    такой стенд отвечает на вопрос «а что если источники разные?» одинаковым
    ответом, то есть не отвечает вовсе."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "-i" in cmd:
            return _Result(returncode=headers_returncode, stdout=headers_stdout,
                           stderr=headers_stderr)
        return _Result(returncode=0, stdout=rate_limit_stdout)

    monkeypatch.setattr(rg.subprocess, "run", fake_run)
    return calls


# ── fetch_core: разбор прод-формы ответа ─────────────────────────────────────


def test_fetch_core_parses_real_rate_limit_shape(monkeypatch):
    calls = _patch_gh(monkeypatch, stdout=REAL_RATE_LIMIT_RESPONSE)

    core = rg.fetch_core()

    assert core == {"limit": 5000, "used": 0, "remaining": 5000, "reset": 1788676450}
    assert calls == [["gh", "api", "rate_limit"]]


def test_fetch_core_raises_quota_check_failed_on_nonzero_exit(monkeypatch):
    _patch_gh(monkeypatch, returncode=1, stderr="HTTP 502: Bad Gateway")

    with pytest.raises(rg.QuotaCheckFailed, match="HTTP 502"):
        rg.fetch_core()


def test_fetch_core_raises_quota_check_failed_on_garbage_json(monkeypatch):
    _patch_gh(monkeypatch, stdout="не json вовсе")

    with pytest.raises(rg.QuotaCheckFailed):
        rg.fetch_core()


def test_fetch_core_raises_on_missing_core_key(monkeypatch):
    # Ответ распарсился, но неожиданной формы (нет .resources.core) — тоже
    # настоящий сбой проверки, не «квоты мало».
    _patch_gh(monkeypatch, stdout=json.dumps({"resources": {}}))

    with pytest.raises(rg.QuotaCheckFailed):
        rg.fetch_core()


# ── should_skip: порог, граница ───────────────────────────────────────────────


def test_should_skip_true_when_remaining_below_threshold():
    assert rg.should_skip({"remaining": 299}, threshold=300) is True


def test_should_skip_false_exactly_at_threshold():
    # Мутация: замена `<` на `<=` в should_skip красит именно этот тест.
    assert rg.should_skip({"remaining": 300}, threshold=300) is False


def test_should_skip_false_well_above_threshold():
    assert rg.should_skip({"remaining": 5000}, threshold=300) is False


def test_reset_human_formats_utc():
    assert rg.reset_human({"reset": 1788676450}) == "2026-09-06 06:34 UTC"


# ── main(): пути «квота ок» / «квота мала» / «настоящий сбой» ────────────────


def test_main_ok_quota_writes_skip_false(monkeypatch, tmp_path, capsys):
    _patch_gh_two_sources(monkeypatch, rate_limit_stdout=REAL_RATE_LIMIT_RESPONSE,
                          headers_stdout=REAL_RESPONSE_WITH_HEADERS)
    monkeypatch.setenv("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    output_file = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(sys, "argv", ["rate_guard.py", "--job", "orchestra"])

    code = rg.main()

    assert code == 0
    text = output_file.read_text(encoding="utf-8")
    assert "skip=false" in text
    assert "skip=true" not in text
    captured = capsys.readouterr()
    assert "::warning::" not in captured.out
    assert "::error::" not in captured.out


def test_main_low_quota_skips_with_warning_not_error(monkeypatch, tmp_path, capsys):
    low = json.dumps({"resources": {"core": {"limit": 1000, "used": 950, "remaining": 50, "reset": 1788676450}}})
    _patch_gh_two_sources(monkeypatch, rate_limit_stdout=low,
                          headers_stdout=REAL_RESPONSE_WITH_HEADERS)
    monkeypatch.setenv("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    output_file = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(sys, "argv", ["rate_guard.py", "--job", "ai-review", "--threshold", "300"])

    code = rg.main()

    # Квота мала — job обязан остаться зелёным (код 0), не упасть.
    assert code == 0
    text = output_file.read_text(encoding="utf-8")
    assert "skip=true" in text
    assert "reset=2026-09-06 06:34 UTC" in text
    captured = capsys.readouterr()
    assert "::warning::" in captured.out
    assert "::error::" not in captured.out
    assert "50/1000" in captured.out
    assert "ai-review" in captured.out


def test_main_hard_failure_is_not_confused_with_low_quota(monkeypatch, tmp_path, capsys):
    # Класс «квоты нет» и класс «запрос не прошёл по другой причине» обязаны
    # различаться и кодом возврата, и аннотацией (граница из докстринга
    # модуля). Мутация: подмена `return 1` на `return 0` в ветке
    # QuotaCheckFailed красит этот тест.
    _patch_gh(monkeypatch, returncode=1, stderr="dial tcp: connection refused")
    monkeypatch.setenv("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    output_file = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(sys, "argv", ["rate_guard.py", "--job", "orchestra"])

    code = rg.main()

    assert code == 1
    assert not output_file.exists() or output_file.read_text(encoding="utf-8") == ""
    captured = capsys.readouterr()
    assert "::error::" in captured.out
    assert "::warning::" not in captured.out
    assert "connection refused" in captured.out


def test_default_threshold_is_300():
    # Число зафиксировано тестом, а не только прозой докстринга (обоснование
    # там же: 150-250 запросов/прогон orchestra + запас ~20%).
    assert rg.DEFAULT_THRESHOLD == 300


def test_main_without_github_output_env_does_not_raise(monkeypatch, capsys):
    # Локальный прогон/дебаг без GITHUB_OUTPUT (не в Actions) — не должен падать.
    # Репозиторий вне Actions передаётся явно: $GITHUB_REPOSITORY там не задан,
    # а применяемый счётчик спрашивают у конкретного репозитория (#1437).
    _patch_gh_two_sources(monkeypatch, rate_limit_stdout=REAL_RATE_LIMIT_RESPONSE,
                          headers_stdout=REAL_RESPONSE_WITH_HEADERS)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setattr(sys, "argv",
                        ["rate_guard.py", "--job", "test", "--repo", "mytab0r/edge-harness"])

    code = rg.main()

    assert code == 0


# ── #1437: rate_limit и применяемый счётчик расходятся ───────────────────────


def test_headers_are_parsed_from_the_real_response_shape(monkeypatch):
    """Прод-форма ответа `gh api -i`, снятая живым вызовом, разбирается целиком
    — включая `resource`, который называет бак прямо."""
    parsed = rg.parse_ratelimit_headers(REAL_RESPONSE_WITH_HEADERS)

    assert parsed == {"limit": 15000, "remaining": 14592, "reset": 1790075485,
                      "used": 408, "resource": "core"}


def test_headers_are_read_case_insensitively():
    """HTTP-заголовки регистронезависимы, и прокси нормализуют их по-разному.
    Разбор, завязанный на один регистр, ослеп бы на смене регистра и сказал
    «прочитать нечем» там, где данные есть — это тот же silent-wrong, только
    с другой стороны."""
    lower = REAL_RESPONSE_WITH_HEADERS.replace("X-Ratelimit-", "x-ratelimit-")

    assert rg.parse_ratelimit_headers(lower)["remaining"] == 14592


def test_missing_ratelimit_headers_are_loud_not_assumed_full():
    """Заголовков нет — это «измерить нечем», а не «квота полная». Подстановка
    полного бака здесь и есть дефект #1437, только перенесённый на этаж ниже."""
    with pytest.raises(rg.QuotaCheckFailed) as error:
        rg.parse_ratelimit_headers("HTTP/1.1 200 OK\nServer: github.com\n\n{}")

    assert "X-Ratelimit" in str(error.value)


def test_decision_takes_the_smaller_of_the_two_sources(monkeypatch, tmp_path, capsys):
    """ЯДРО #1437, воспроизведённое числами живого случая (прогон 35715554412,
    job `test`, PR #1449): `rate_limit` отдал ПОЛНЫЙ бак 5000/5000, а через
    2 мин 41 с тот же токен получил 403 «rate limit exceeded for
    installation». Полный бак и исчерпание одновременно — значит это разные
    счётчики, и решать по `rate_limit` нельзя.

    Здесь применяемый счётчик (заголовки) показывает 12 при пороге 300, а
    `rate_limit` — 5000/5000. Гейт обязан пропустить дорогой путь."""
    full_rate_limit = json.dumps({"resources": {"core": {
        "limit": 5000, "used": 0, "remaining": 5000, "reset": 1790075485}}})
    drained_headers = REAL_RESPONSE_WITH_HEADERS.replace(
        "X-Ratelimit-Remaining: 14592", "X-Ratelimit-Remaining: 12").replace(
        "X-Ratelimit-Limit: 15000", "X-Ratelimit-Limit: 1000")
    _patch_gh_two_sources(monkeypatch, rate_limit_stdout=full_rate_limit,
                          headers_stdout=drained_headers)
    monkeypatch.setenv("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    output_file = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(sys, "argv", ["rate_guard.py", "--job", "repo-ci-invariants"])

    code = rg.main()

    assert code == 0
    assert "skip=true" in output_file.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert "::warning::" in captured.out
    assert "12/1000" in captured.out, captured.out


def test_both_readings_are_printed_side_by_side(monkeypatch, tmp_path, capsys):
    """Замер, которого требует #1437, обязан попадать в лог КАЖДОГО прогона, а
    не только упавшего: иначе следующий 403 снова придётся ловить отдельной
    кампанией. Оба остатка печатаются рядом даже когда гейт пропускает."""
    _patch_gh_two_sources(monkeypatch, rate_limit_stdout=REAL_RATE_LIMIT_RESPONSE,
                          headers_stdout=REAL_RESPONSE_WITH_HEADERS)
    monkeypatch.setenv("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github_output"))
    monkeypatch.setattr(sys, "argv", ["rate_guard.py", "--job", "orchestra"])

    rg.main()

    out = capsys.readouterr().out
    assert "5000/5000" in out, out          # rate_limit
    assert "14592/15000" in out, out        # применяемый счётчик
    assert "core" in out, out               # X-Ratelimit-Resource


def test_unreadable_enforced_counter_is_an_error_not_a_green_pass(monkeypatch, tmp_path, capsys):
    """Применяемый счётчик не прочитался ПО-НАСТОЯЩЕМУ (сеть легла) — дорогой
    путь НЕ идёт вперёд молча. Возврат к «ну, rate_limit же сказал ок» вернул
    бы ровно #1437."""
    _patch_gh_two_sources(monkeypatch, rate_limit_stdout=REAL_RATE_LIMIT_RESPONSE,
                          headers_stdout="", headers_returncode=1,
                          headers_stderr="dial tcp 140.82.121.6:443: connect: connection refused")
    monkeypatch.setenv("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    output_file = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(sys, "argv", ["rate_guard.py", "--job", "orchestra"])

    code = rg.main()

    assert code == 1
    captured = capsys.readouterr()
    assert "::error::" in captured.out
    assert "ПРИМЕНЯЕМОМУ" in captured.out
    assert "skip=false" not in (output_file.read_text(encoding="utf-8") if output_file.exists() else "")


def test_exhausted_counter_is_a_measurement_not_a_failure(monkeypatch, tmp_path, capsys):
    """САМОЕ ТЯЖЁЛОЕ состояние объекта: бак реально пуст, и зонд получает
    403 «rate limit exceeded». Это ОТВЕТ счётчика, а не отказ измерения —
    исход обязан быть обычным skip: `skip=true`, код 0, `::warning::`, job
    зелёный, дорогие шаги выключены.

    Первая редакция этого PR роняла здесь красным, то есть в худшем случае
    воспроизводила дефект #1437 детерминированно: первый шаг КАЖДОГО
    открытого PR красный, пока бак не сбросится (до часа), и пульс
    оркестратора умирает на гейте до очереди слияний. Находка ai-review
    PR #1451, блокирующая; текст отказа — дословный из лога прогона
    35715554412."""
    _patch_gh_two_sources(
        monkeypatch, rate_limit_stdout=REAL_RATE_LIMIT_RESPONSE,
        headers_stdout="", headers_returncode=1,
        headers_stderr="gh: API rate limit exceeded for installation. If you reach out to "
                       "GitHub Support for help, please include the request ID "
                       "205B:9E2A9:3347C48:A84182D:6AB257F5 … (HTTP 403)")
    monkeypatch.setenv("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    output_file = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(sys, "argv", ["rate_guard.py", "--job", "repo-ci"])

    code = rg.main()

    assert code == 0, "исчерпанный бак не имеет права ронять обязательную проверку"
    assert "skip=true" in output_file.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert "::warning::" in captured.out
    assert "::error::" not in captured.out


def test_secondary_rate_limit_is_the_same_outcome(monkeypatch, tmp_path, capsys):
    """Вторичный лимит формулируется другими словами, но значит то же:
    счётчик ответил «пусто». Разная реакция на два текста одного факта —
    та же угадайка, которую правило «алерт не гадает» запрещает."""
    _patch_gh_two_sources(
        monkeypatch, rate_limit_stdout=REAL_RATE_LIMIT_RESPONSE,
        headers_stdout="", headers_returncode=1,
        headers_stderr="gh: You have exceeded a secondary rate limit (HTTP 403)")
    monkeypatch.setenv("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    output_file = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(sys, "argv", ["rate_guard.py", "--job", "repo-ci"])

    assert rg.main() == 0
    assert "skip=true" in output_file.read_text(encoding="utf-8")
    assert "::error::" not in capsys.readouterr().out


def test_quota_exhausted_stays_catchable_as_quota_check_failed():
    """Подкласс, а не отдельная ветка иерархии: вызывающий, который ловит
    только базовый QuotaCheckFailed, продолжает работать — ошибка не
    протечёт наружу необработанной, если кто-то забудет про подкласс."""
    assert issubclass(rg.QuotaExhausted, rg.QuotaCheckFailed)


def test_missing_github_repository_is_loud(monkeypatch, tmp_path, capsys):
    """Репозиторий неизвестен — применяемый счётчик прочитать нечем. Тихий
    откат к одному только rate_limit был бы возвратом дефекта через заднюю
    дверь."""
    _patch_gh_two_sources(monkeypatch, rate_limit_stdout=REAL_RATE_LIMIT_RESPONSE,
                          headers_stdout=REAL_RESPONSE_WITH_HEADERS)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github_output"))
    monkeypatch.setattr(sys, "argv", ["rate_guard.py", "--job", "orchestra"])
    # ни --repo, ни $GITHUB_REPOSITORY

    code = rg.main()

    assert code == 1
    assert "::error::" in capsys.readouterr().out


def test_enforced_reader_calls_the_repo_endpoint_not_rate_limit(monkeypatch):
    """Читать заголовки у самого `rate_limit` бессмысленно: этот эндпоинт
    исключён из лимитирования и отдаёт свой бак — измерено 2026-09-22, у него
    даже `reset` другой. Запрос обязан идти по обычному пути."""
    calls = _patch_gh(monkeypatch, stdout=REAL_RESPONSE_WITH_HEADERS)

    rg.fetch_enforced("mytab0r/edge-harness")

    assert calls == [["gh", "api", "-i", "repos/mytab0r/edge-harness"]]


# ── #1004: обёртка main() гвардии, читающей API ──────────────────────────────


def test_exhausted_budget_in_a_guard_is_a_warning_not_a_red_check(capsys):
    """Живой случай: прогон 35727716021 (PR #1458). Гейт квоты корректно
    пропустил дорогие шаги, а шаг «Каталог гвардий» всё равно свалил
    обязательную проверку — `decision-doc-numbering-guard` упал трейсбеком.
    Текст отказа — дословный из того лога."""
    def failing() -> int:
        raise RuntimeError(
            "gh api repos/mytab0r/edge-harness/pulls?state=open&per_page=100&page=1: "
            "gh: API rate limit exceeded for installation … (HTTP 403)")

    code = rg.run_guard_main(failing, guard="decision-doc-numbering-guard")

    assert code == 0, "исчерпанный бюджет не имеет права красить обязательную проверку"
    out = capsys.readouterr().out
    assert "::warning::" in out
    assert "::error::" not in out
    assert "decision-doc-numbering-guard" in out


def test_a_real_failure_is_not_swallowed_by_the_wrapper():
    """Обёртка обязана быть узкой: всё, что не про квоту, проходит наверх
    нетронутым. Проглотить настоящую поломку было бы silent-wrong, ради
    которого весь класс и чинится."""
    def broken() -> int:
        raise RuntimeError("scripts/lib/foo.py: ожидался список, пришёл dict")

    with pytest.raises(RuntimeError, match="ожидался список"):
        rg.run_guard_main(broken, guard="x")


def test_wrapper_returns_the_guards_own_code_when_nothing_throws():
    assert rg.run_guard_main(lambda: 0, guard="x") == 0
    assert rg.run_guard_main(lambda: 1, guard="x") == 1


def test_classifier_is_one_place_of_truth_for_both_surfaces():
    """Гейт и обёртка спрашивают ОДНУ функцию: две копии регулярки разошлись
    бы на первом же новом варианте текста отказа."""
    assert rg.is_rate_limit_refusal("gh: API rate limit exceeded for installation")
    assert rg.is_rate_limit_refusal("You have exceeded a secondary rate limit")
    assert not rg.is_rate_limit_refusal("dial tcp: connection refused")
