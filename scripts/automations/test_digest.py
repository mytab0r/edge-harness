"""Тесты сборщика дайджеста (#116): чистая логика + E2E main() на прод-форме.

Тест кормится прод-формой, а не пересказом: ответы GitHub search API, Slack Web
API и Telegram Bot API — той формы, что документируют их API и отдают живые
системы. Сеть не трогается: gh_api/post_json подменяются, формы фикстур — нет.

Запуск: python -m pytest scripts/automations/test_digest.py -q
"""

import datetime as dt
import importlib.util
import json
import sys
import urllib.parse
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "digest", Path(__file__).resolve().parent / "digest.py")
digest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(digest)

# ── Прод-форма ответов ──────────────────────────────────────────────────────────────

# GET search/issues — реальная форма ответа (поля, которые читает сборщик;
# у issue поле pull_request отсутствует, у PR — есть).
SEARCH_ISSUES = {
    "total_count": 2, "incomplete_results": False,
    "items": [
        {"number": 115, "title": "интеграции внешних систем",
         "html_url": "https://github.com/o/r/issues/115",
         "closed_at": "2026-08-31T10:00:00Z", "state": "closed", "id": 1},
        {"number": 102, "title": "UI плагинов в морде",
         "html_url": "https://github.com/o/r/issues/102",
         "closed_at": "2026-08-30T10:00:00Z", "state": "closed", "id": 2},
    ],
}
SEARCH_PRS = {
    "total_count": 1, "incomplete_results": False,
    "items": [
        {"number": 239, "title": "#115: инструменты агента",
         "html_url": "https://github.com/o/r/pull/239",
         "closed_at": "2026-09-01T10:00:00Z", "state": "closed", "id": 3,
         "pull_request": {"merged_at": "2026-09-01T10:00:00Z"}},
    ],
}

# GET /api/tasks журнала — прод-форма (cf-worker/src/harness.ts #recentTasks).
JOURNAL = {"tasks": [
    {"id": "issue-115", "created_ts": 1_800_000_000_000, "dispatch_ts": 1_800_000_050_000,
     "latency_ms": 8300, "status": "done"},
    {"id": "issue-116", "created_ts": 1_800_000_100_000, "dispatch_ts": 1_800_000_150_000,
     "latency_ms": 9100, "status": "running"},
    {"id": "automation:weekly-digest:xyz", "created_ts": 1_800_000_200_000,
     "dispatch_ts": 1_800_000_210_000, "latency_ms": 8800, "status": "done"},
    # вне периода — не считается
    {"id": "issue-4", "created_ts": 1_700_000_000_000, "dispatch_ts": None,
     "latency_ms": None, "status": "queued"},
]}

SINCE_TS, UNTIL_TS = 1_799_000_000_000, 1_800_500_000_000
DATE_FROM = dt.datetime.fromtimestamp(SINCE_TS / 1000, dt.timezone.utc).date()
DATE_TO = dt.datetime.fromtimestamp(UNTIL_TS / 1000, dt.timezone.utc).date()


def test_collect_separates_issues_and_prs(monkeypatch):
    """Гвардия формы запроса (ревью #116, major 1): диапазон дат — ОТДЕЛЬНОЕ
    слово запроса. Склейка «is:closed:d1..d2» синтаксически валидна, но GitHub
    молча отдаёт пустую выборку — точная форма запроса и есть тест."""
    seen = []

    def fake_gh(query):
        decoded = urllib.parse.unquote(query)
        seen.append(decoded)
        if "is:issue" in decoded:
            return SEARCH_ISSUES
        return SEARCH_PRS

    monkeypatch.setattr(digest, "gh_api", fake_gh)
    issues, pulls = digest.collect_issues_and_prs("o/r", DATE_FROM, DATE_TO)
    assert [i["number"] for i in issues] == [115, 102]
    assert [p["number"] for p in pulls] == [239]
    assert seen == [
        f"search/issues?q=repo:o/r is:issue is:closed closed:{DATE_FROM.isoformat()}..{DATE_TO.isoformat()}&per_page=100&page=1",
        f"search/issues?q=repo:o/r is:pr is:merged merged:{DATE_FROM.isoformat()}..{DATE_TO.isoformat()}&per_page=100&page=1",
    ]


def test_search_guard_rejects_glued_date_qualifier():
    """Мутация гвардии: клей даты к is:closed обязан ронять сбор, а не давать
    зелёный дайджест с нулями."""
    import pytest
    with pytest.raises(RuntimeError, match="склеился"):
        digest.search_github("o/r", "is:issue is:closed:", "closed", DATE_FROM, DATE_TO)


def test_collect_journal_counts_by_status_and_marks_automation_runs():
    # collect_journal ходит urllib'ом — подменяем запрос, ответ остаётся прод-формой.
    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(JOURNAL).encode()
    def fake_urlopen(request, timeout):
        assert "/api/tasks" in request.full_url
        assert request.get_header("Authorization") == "Bearer t"
        return FakeResponse()
    orig = digest.urllib.request.urlopen
    digest.urllib.request.urlopen = fake_urlopen
    try:
        result = digest.collect_journal("https://hands.example", "t", SINCE_TS, UNTIL_TS)
    finally:
        digest.urllib.request.urlopen = orig
    assert result["counts"] == {"done": 2, "running": 1}
    assert result["automation_runs"] == 1


def test_slack_delivery_on_production_forms():
    # Web API Slack на успех отвечает HTTP 200 {"ok": true, ...}
    digest.post_json = lambda *a, **k: (200, {"ok": True, "ts": "1756...", "channel": "C123"})
    ok, detail = digest.deliver_slack("x-token", "#harness", "текст")
    assert ok and "доставлено" in detail
    # ...и на ошибку канала тоже HTTP 200, но {"ok": false, "error": ...}
    digest.post_json = lambda *a, **k: (200, {"ok": False, "error": "channel_not_found"})
    ok, detail = digest.deliver_slack("x-token", "#ghost", "текст")
    assert not ok and "channel_not_found" in detail
    # нет секрета — честный отказ, а не попытка уйти без авторизации
    ok, detail = digest.deliver_slack("", "#harness", "текст")
    assert not ok and "SLACK_BOT_TOKEN" in detail


def test_telegram_delivery_on_production_forms():
    digest.post_json = lambda *a, **k: (200, {"ok": True, "result": {"message_id": 7}})
    ok, detail = digest.deliver_telegram("t", "42", "текст")
    assert ok
    # отказ Telegram — HTTP 400 и description
    digest.post_json = lambda *a, **k: (400, {"ok": False, "error_code": 400, "description": "chat not found"})
    ok, detail = digest.deliver_telegram("t", "42", "текст")
    assert not ok and "chat not found" in detail
    # длинный текст обрезается под лимит Telegram, а не тонет
    captured = {}
    def capture(url, headers, payload):
        captured["text"] = payload["text"]
        return (200, {"ok": True, "result": {}})
    digest.post_json = capture
    ok, _ = digest.deliver_telegram("t", "42", "б" * 5000)
    assert ok and len(captured["text"]) <= digest.TELEGRAM_TEXT_LIMIT + 64


def test_channel_isolation_slack_down_telegram_delivered(monkeypatch):
    # Сеть Slack упала (RuntimeError из post_json), Telegram — жив: отказ одного
    # канала не мешает остальному (критерий #116), итог честно неуспешный.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    def slack_down(url, headers, payload):
        raise RuntimeError("сеть недоступна: boom")
    responses = {"slack.com": slack_down, "api.telegram.org": lambda *a, **k: (200, {"ok": True, "result": {}})}
    def router(url, headers, payload):
        for host, handler in responses.items():
            if host in url:
                return handler(url, headers, payload)
        raise AssertionError(f"неожиданный url {url}")
    digest.post_json = router
    delivered = digest.deliver_all(
        [{"type": "slack", "target": "#harness"}, {"type": "telegram"}], "дайджест")
    assert delivered[0]["ok"] is False and "boom" in delivered[0]["detail"]
    assert delivered[1]["ok"] is True
    result = digest.build_result(issues=[{"number": 1}], pulls=[], journal={"counts": {}, "automation_runs": 0}, delivered=delivered)
    assert result["ok"] is False and result["issues"] == 1


def test_format_digest_mentions_both_sources_and_period():
    text = digest.format_digest(
        "o/r", DATE_FROM, DATE_TO,
        [{"number": 115, "title": "интеграции", "url": "u"}],
        [{"number": 239, "title": "#115: инструменты", "url": "v"}],
        {"counts": {"done": 3, "failed": 1}, "automation_runs": 2},
    )
    assert f"{DATE_FROM.isoformat()} — {DATE_TO.isoformat()}" in text
    assert "Закрытые задачи: 1" in text and "слитые PR: 1" in text
    assert "#115 интеграции" in text and "#239" in text
    assert "failed: 1" in text


def test_main_end_to_end_channel_failure_is_visible(tmp_path, monkeypatch):
    """E2E: Slack недоступен, Telegram доставил — exit 1, в result-файле виден
    отказ канала и доставка остальных («остальное доставляется, отказ виден»)."""
    config = {
        "enabled": True,
        "trigger": {"type": "schedule", "intervalHours": 168},
        "task": {"kind": "digest"},
        "report": {"channels": [{"type": "slack", "target": "#harness"}, {"type": "telegram"}]},
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    result_file = tmp_path / "result.json"

    def fake_gh(query):
        return SEARCH_PRS if "is:pr" in urllib.parse.unquote(query) else SEARCH_ISSUES

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(JOURNAL).encode()
    def fake_urlopen(request, timeout):
        if "/api/tasks" in request.full_url:
            return FakeResponse()
        raise AssertionError(f"неожиданный запрос {request.full_url}")

    def fake_post(url, headers, payload):
        if "slack.com" in url:
            return (200, {"ok": False, "error": "not_in_channel"})
        assert "api.telegram.org" in url
        return (200, {"ok": True, "result": {"message_id": 1}})

    monkeypatch.setattr(digest, "gh_api", fake_gh)
    monkeypatch.setattr(digest, "post_json", fake_post)
    monkeypatch.setattr(digest.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("HANDS_URL", "https://hands.example")
    monkeypatch.setenv("HANDS_TOKEN", "t")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    rc = digest.main(["--config-file", str(config_file), "--since-ts", str(SINCE_TS),
                      "--until-ts", str(UNTIL_TS), "--result-file", str(result_file)])
    assert rc == 1  # канал отказал — job красный, даже если Telegram доставил
    result = json.loads(result_file.read_text(encoding="utf-8"))
    assert result["ok"] is False
    by_type = {c["type"]: c for c in result["channels"]}
    assert by_type["slack"]["ok"] is False and "not_in_channel" in by_type["slack"]["detail"]
    assert by_type["telegram"]["ok"] is True
    assert result["issues"] == 2 and result["prs"] == 1


def test_main_refuses_digest_without_channels(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"report": {"channels": []}}), encoding="utf-8")
    result_file = tmp_path / "result.json"
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    assert digest.main(["--config-file", str(config_file), "--result-file", str(result_file)]) == 1
    assert not result_file.exists()  # до каналов дело не дошло — result нет


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
