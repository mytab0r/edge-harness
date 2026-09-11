#!/usr/bin/env python3
"""Тесты anthropic_oauth_import — на ФЕЙКОВЫХ токенах (не живые значения).

Кормим прод-форму файла Claude Code (~/.claude/.credentials.json): те же ключи, что
отдаёт настоящий логин, но выдуманные строки токенов. Проверяем валидацию, отказы,
формат секрета и главный инвариант — токены не утекают в stdout.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import anthropic_oauth_import as mod  # noqa: E402

# Прод-форма: ключи как у живого .credentials.json, значения — заведомо фейк.
FAKE_ACCESS = "fake-access-TESTTOKEN-do-not-use-0000"
FAKE_REFRESH = "fake-refresh-TESTTOKEN-do-not-use-1111"
FAKE_CREDENTIALS = {
    "mcpOAuth": {"some": "unrelated"},
    "claudeAiOauth": {
        "accessToken": FAKE_ACCESS,
        "refreshToken": FAKE_REFRESH,
        "expiresAt": 1_700_000_000_000,
        "refreshTokenExpiresAt": 1_800_000_000_000,
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max",
        "rateLimitTier": "default",
    },
}


def _write(tmp: Path, data) -> Path:
    p = tmp / "credentials.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


class LoadOAuth(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_valid_file_returns_oauth(self):
        src = _write(self.tmp, FAKE_CREDENTIALS)
        oauth = mod.load_oauth(src)
        self.assertEqual(oauth["accessToken"], FAKE_ACCESS)
        self.assertEqual(oauth["subscriptionType"], "max")

    def test_accepts_bare_oauth_key(self):
        # Плагин читает claudeAiOauth ЛИБО oauth — поддерживаем оба.
        src = _write(self.tmp, {"oauth": FAKE_CREDENTIALS["claudeAiOauth"]})
        self.assertEqual(mod.load_oauth(src)["refreshToken"], FAKE_REFRESH)

    def test_missing_access_token_fails_loud(self):
        bad = json.loads(json.dumps(FAKE_CREDENTIALS))
        del bad["claudeAiOauth"]["accessToken"]
        src = _write(self.tmp, bad)
        with self.assertRaises(mod.LoudError) as cm:
            mod.load_oauth(src)
        self.assertIn("accessToken", str(cm.exception))

    def test_missing_refresh_token_fails_loud(self):
        bad = json.loads(json.dumps(FAKE_CREDENTIALS))
        del bad["claudeAiOauth"]["refreshToken"]
        src = _write(self.tmp, bad)
        with self.assertRaises(mod.LoudError):
            mod.load_oauth(src)

    def test_no_claude_oauth_fails_loud(self):
        src = _write(self.tmp, {"mcpOAuth": {"x": 1}})
        with self.assertRaises(mod.LoudError) as cm:
            mod.load_oauth(src)
        self.assertIn("claudeAiOauth", str(cm.exception))

    def test_missing_file_fails_loud(self):
        with self.assertRaises(mod.LoudError) as cm:
            mod.load_oauth(self.tmp / "нет-такого.json")
        self.assertIn("не найден", str(cm.exception))


class SecretPayload(unittest.TestCase):
    def test_payload_is_wrapped_claude_oauth(self):
        payload = mod.build_secret_payload(FAKE_CREDENTIALS["claudeAiOauth"])
        parsed = json.loads(payload)
        # Ровно то, что читает `dsh-anthropic-pool add`.
        self.assertEqual(list(parsed.keys()), ["claudeAiOauth"])
        self.assertEqual(parsed["claudeAiOauth"]["accessToken"], FAKE_ACCESS)

    def test_payload_drops_unrelated_top_level(self):
        # mcpOAuth в секрет не тянем.
        payload = mod.build_secret_payload(FAKE_CREDENTIALS["claudeAiOauth"])
        self.assertNotIn("mcpOAuth", json.loads(payload))

    def test_secret_name(self):
        self.assertEqual(mod.secret_name(1), "ANTHROPIC_OAUTH_1")
        self.assertEqual(mod.secret_name(2), "ANTHROPIC_OAUTH_2")


def _krouter_conn(name, priority, provider="claude", authType="oauth",
                  active=True, banned=False, access=FAKE_ACCESS, refresh=FAKE_REFRESH):
    c = {"provider": provider, "authType": authType, "isActive": active,
         "isPermanentlyBanned": banned, "name": name, "priority": priority,
         "scope": "user:inference user:profile", "expiresAt": 1_700_000_000_000}
    if access is not None:
        c["accessToken"] = access
    if refresh is not None:
        c["refreshToken"] = refresh
    return c


FAKE_KROUTER = {
    "providerConnections": [
        _krouter_conn("second@x.com", priority=2),
        _krouter_conn("first@x.com", priority=1),
        _krouter_conn("apikey@x.com", priority=0, authType="apiKey"),   # не oauth — пропуск
        _krouter_conn("banned@x.com", priority=0, banned=True),          # бан — пропуск
        _krouter_conn("inactive@x.com", priority=0, active=False),       # неактив — пропуск
        _krouter_conn("norefresh@x.com", priority=0, refresh=""),        # нет refresh — пропуск
        _krouter_conn("gpt@x.com", priority=0, provider="codex"),        # не claude — пропуск
    ]
}


class KrouterSource(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.backup = _write(self.tmp, FAKE_KROUTER)

    def tearDown(self):
        self._td.cleanup()

    def test_filters_and_sorts_by_priority(self):
        accts = mod.krouter_claude_accounts(self.backup)
        self.assertEqual([a["name"] for a in accts], ["first@x.com", "second@x.com"])

    def test_conn_maps_scope_to_scopes_list(self):
        oauth = mod.krouter_conn_to_oauth(_krouter_conn("x", 1))
        self.assertEqual(oauth["scopes"], ["user:inference", "user:profile"])
        self.assertEqual(oauth["accessToken"], FAKE_ACCESS)

    def test_no_claude_oauth_fails_loud(self):
        empty = _write(self.tmp, {"providerConnections": [_krouter_conn("g", 1, provider="codex")]})
        with self.assertRaises(mod.LoudError):
            mod.krouter_claude_accounts(empty)

    def test_not_a_krouter_backup_fails_loud(self):
        bad = _write(self.tmp, {"claudeAiOauth": {}})
        with self.assertRaises(mod.LoudError) as cm:
            mod.krouter_claude_accounts(bad)
        self.assertIn("providerConnections", str(cm.exception))

    def test_from_krouter_apply_sets_both_slots_no_leak(self):
        buf = io.StringIO()
        with mock.patch.object(mod, "set_secret") as set_secret, redirect_stdout(buf):
            rc = mod.main(["--from-krouter", str(self.backup), "--apply"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        names = [c.args[1] for c in set_secret.call_args_list]
        self.assertEqual(names, ["ANTHROPIC_OAUTH_1", "ANTHROPIC_OAUTH_2"])
        self.assertNotIn(FAKE_ACCESS, out)
        self.assertNotIn(FAKE_REFRESH, out)
        self.assertIn("first@x.com", out)

    def test_from_krouter_respects_max_slots(self):
        with mock.patch.object(mod, "set_secret") as set_secret, redirect_stdout(io.StringIO()):
            mod.main(["--from-krouter", str(self.backup), "--max-slots", "1", "--apply"])
        self.assertEqual(len(set_secret.call_args_list), 1)

    def test_from_krouter_dry_run_sets_nothing(self):
        with mock.patch.object(mod, "set_secret") as set_secret, redirect_stdout(io.StringIO()):
            rc = mod.main(["--from-krouter", str(self.backup)])
        self.assertEqual(rc, 0)
        set_secret.assert_not_called()


class NoTokenLeak(unittest.TestCase):
    """Главный инвариант: значения токенов НЕ попадают в stdout ни при каком прогоне."""

    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.src = _write(self.tmp, FAKE_CREDENTIALS)

    def tearDown(self):
        self._td.cleanup()

    def test_dry_run_prints_no_token_values(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mod.main(["--slot", "1", "--source", str(self.src)])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertNotIn(FAKE_ACCESS, out)
        self.assertNotIn(FAKE_REFRESH, out)
        self.assertIn("сухой прогон", out)

    def test_apply_calls_set_secret_and_leaks_nothing(self):
        buf = io.StringIO()
        with mock.patch.object(mod, "set_secret") as set_secret, redirect_stdout(buf):
            rc = mod.main(["--slot", "2", "--source", str(self.src), "--apply"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        set_secret.assert_called_once()
        repo, name, value = set_secret.call_args.args
        self.assertEqual(name, "ANTHROPIC_OAUTH_2")
        # Секрет-значение несёт токены, но в stdout их нет.
        self.assertIn(FAKE_ACCESS, value)
        self.assertNotIn(FAKE_ACCESS, out)
        self.assertNotIn(FAKE_REFRESH, out)
        self.assertIn("ЗАПИСАН", out)

    def test_bad_slot_rejected(self):
        rc = mod.main(["--slot", "0", "--source", str(self.src)])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
