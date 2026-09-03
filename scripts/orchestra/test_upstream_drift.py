#!/usr/bin/env python3
"""Тесты сигнала дрейфа пина апстрима (scripts/orchestra/upstream_drift.py, #134).

Кормятся прод-формой: теги — настоящий ответ `gh api repos/pawaca/dsh-edge/tags`
(имена и sha сняты живым запросом 2026-09-03; sha тега dsh-edge-v0.8.0 = текущий
пин dsh-edge/upstream.json, sha dsh-edge-v0.7.1 = предыдущий пин). Проводка
upstream_drift_check — на моке pulse_guard.gh (единственный пункт патча, сеть
не нужна); сигнал Telegram внутри escalate в тестовой среде не доставлен
(секретов нет) — это честный исход, место правды комментария проверяется
фактом POST-вызова.

Запуск: python -m pytest scripts/orchestra/test_upstream_drift.py -q
"""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

SCRIPT = _DIR / "upstream_drift.py"
spec = importlib.util.spec_from_file_location("upstream_drift", SCRIPT)
ud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ud)  # type: ignore[union-attr]

pg = sys.modules["pulse_guard"]


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def tag(name, sha):
    """Прод-форма элемента repos/{repo}/tags (поля, которые читает модуль)."""
    return {"name": name, "commit": {"sha": sha, "url": f"https://x/{sha}"}}


# Настоящий ответ API (сокращён до значимых тегов; порядок — как отдаёт GitHub).
PIN_080 = "b9a8ddd6cd11bc0db94d3f67bbc7de4d674e69a1"   # dsh-edge-v0.8.0, пин с бампа #134
PIN_071 = "113a96913c51881993122afbf42e776882c4beb7"   # dsh-edge-v0.7.1, пин до бампа
TAGS = [
    tag("dsh-edge-v0.8.0", PIN_080),
    tag("dsh-edge-v0.7.1", PIN_071),
    tag("dsh-edge-v0.7.0", "872170e9fc8aa938867e6f0b8c10bbc49f01d6aa"),
    tag("dsh-edge-v0.7.0-alpha.2", "c99cc8d3a900ba6bc7cbccee69f1add30331c908"),
    tag("dsh-edge-v0.7.0-alpha.1", "d167d53541fc1a0cedea2e896b024c76505dfdc2"),
    tag("dsh-edge-v0.6.0", "d761215650fc9fdefd0f3d093c127318d40ced34"),
    tag("dsh-edge-v0.5.3", "b369935c2eb1955bbc34f931d5f4a93095ced900"),
    tag("dsh-edge-v0.5.0", "8277216848bb2afd379ad474bf1a1683338ea57a"),
    tag("dsh-edge-v0.5.0-alpha.1", "8277216848bb2afd379ad474bf1a1683338ea57a"),
    tag("dsh-edge-v0.3.0", "5ff7ea0b46788cf0b4f7ee966159a8210ecba2ec"),
]


def marker(target, when="2026-09-03T00:00:00Z"):
    """Прод-форма комментария-маркера, как его оставляет upstream_drift_check."""
    return (datetime.fromisoformat(when.replace("Z", "+00:00")),
            f"🚨 edge-harness: {ud.DRIFT_MARKER} {target}]\nтекст сигнала")


def pin(sha=PIN_080, repo="pawaca/dsh-edge"):
    return {"repo": repo, "sha": sha}


# ── Семвер тегов: парсинг и сравнение ────────────────────────────────────────────


def test_parse_release_tag_stable_and_prerelease():
    assert ud.parse_release_tag("dsh-edge-v0.8.0") == ((0, 8, 0), 1, ())
    key = ud.parse_release_tag("dsh-edge-v0.7.0-alpha.2")
    assert key[0] == (0, 7, 0) and key[1] == 0  # ядро + флаг пререлиза


@pytest.mark.parametrize("tag_name", [
    "main", "v0.8.0", "dsh-edge-v0.8", "dsh-edge-0.8.0", "", "dsh-edge-v008.0",
])
def test_parse_release_tag_rejects_non_release_tags(tag_name):
    assert ud.parse_release_tag(tag_name) is None


def test_semver_ordering_two_digit_minor_beats_nine():
    # Класс «0.10 < 0.9»: сравнение покомпонентное ЧИСЛОВОЕ, не строковое.
    assert ud.parse_release_tag("dsh-edge-v0.10.0") > ud.parse_release_tag("dsh-edge-v0.9.9")


def test_semver_stable_beats_prerelease_of_same_core():
    assert ud.parse_release_tag("dsh-edge-v0.8.0") > ud.parse_release_tag("dsh-edge-v0.8.0-rc.1")


def test_semver_numeric_prerelease_id_below_alpha():
    # semver §11: 1.2.3-a.2 < 1.2.3-a.10 < 1.2.3-b
    a2 = ud.parse_release_tag("dsh-edge-v1.2.3-a.2")
    a10 = ud.parse_release_tag("dsh-edge-v1.2.3-a.10")
    b = ud.parse_release_tag("dsh-edge-v1.2.3-b")
    assert a2 < a10 < b


# ── Новейший стабильный тег и поиск тега по sha ──────────────────────────────────


def test_latest_stable_tag_on_prod_form():
    assert ud.latest_stable_tag(TAGS)["name"] == "dsh-edge-v0.8.0"


def test_latest_stable_tag_ignores_prerelease_even_with_higher_core():
    tags = TAGS + [tag("dsh-edge-v0.9.0-alpha.1", "a" * 40)]
    # Пререлиз новее по дате и по ядру, но целью бампа быть не может: политика
    # пина — «тег релиза» (dsh-edge/upstream.json).
    assert ud.latest_stable_tag(tags)["name"] == "dsh-edge-v0.8.0"


def test_latest_stable_tag_empty_is_none():
    assert ud.latest_stable_tag([]) is None
    assert ud.latest_stable_tag([tag("dsh-edge-v0.9.0-alpha.1", "a" * 40)]) is None


def test_tag_by_sha_finds_old_pin():
    assert ud.tag_by_sha(TAGS, PIN_071)["name"] == "dsh-edge-v0.7.1"


def test_tag_by_sha_unknown_is_none():
    assert ud.tag_by_sha(TAGS, "f" * 40) is None


def test_tag_by_sha_same_commit_two_tags_returns_a_tag():
    # Реальный случай апстрима: v0.5.0 и v0.5.0-alpha.1 — один коммит. Достаточно
    # любого из них, важна не уникальность, а факт «sha стоит на теге».
    assert ud.tag_by_sha(TAGS, "8277216848bb2afd379ad474bf1a1683338ea57a") is not None


# ── Решение: ok / drift / pin-not-tag ────────────────────────────────────────────


def test_decide_ok_when_pin_is_latest_stable():
    decision = ud.decide_drift(pin(PIN_080), TAGS)
    assert decision["state"] == "ok"
    assert decision["pinned_tag"] == "dsh-edge-v0.8.0"
    assert decision["latest_tag"] == "dsh-edge-v0.8.0"


def test_decide_drift_when_upstream_released_newer():
    # Состояние до бампа #134: пин 0.7.1, апстрим выпустил 0.8.0 — ровно тот
    # дрейф, который годами не кричал (npm-сверка #73 не смотрела на пин).
    decision = ud.decide_drift(pin(PIN_071), TAGS)
    assert decision["state"] == "drift"
    assert decision["pinned_tag"] == "dsh-edge-v0.7.1"
    assert decision["latest_tag"] == "dsh-edge-v0.8.0"


def test_decide_pin_not_tag_is_loud_state():
    decision = ud.decide_drift(pin("c" * 40), TAGS)
    assert decision["state"] == "pin-not-tag"
    assert decision["pinned_tag"] is None


def test_decide_pin_on_prerelease_with_higher_core_is_ok():
    tags = TAGS + [tag("dsh-edge-v0.9.0-alpha.1", "b" * 40)]
    assert ud.decide_drift(pin("b" * 40), tags)["state"] == "ok"


def test_decide_pin_on_rc_of_released_core_is_drift():
    tags = TAGS + [tag("dsh-edge-v0.8.0-rc.1", "c" * 40)]
    assert ud.decide_drift(pin("c" * 40), tags)["state"] == "drift"


def test_decide_without_stable_tags_is_runtime_error():
    with pytest.raises(RuntimeError):
        ud.decide_drift(pin(PIN_080), [tag("dsh-edge-v0.9.0-alpha.1", "a" * 40)])


# ── Маркер «один сигнал на эпизод» ───────────────────────────────────────────────


def test_drift_target_is_latest_tag_for_drift():
    assert ud.drift_target(ud.decide_drift(pin(PIN_071), TAGS)) == "dsh-edge-v0.8.0"


def test_drift_target_binds_pin_sha_for_pin_not_tag():
    target = ud.drift_target(ud.decide_drift(pin("c" * 40), TAGS))
    assert target == "sha-cccccccccccc"


def test_signal_pending_without_markers():
    assert ud.signal_pending(ud.decide_drift(pin(PIN_071), TAGS), []) is True


def test_signal_silent_while_same_episode_marker_stands():
    decision = ud.decide_drift(pin(PIN_071), TAGS)
    markers = [marker("dsh-edge-v0.8.0")]
    assert ud.signal_pending(decision, markers) is False


def test_signal_pending_again_on_new_upstream_release():
    decision = ud.decide_drift(pin(PIN_071), TAGS)
    markers = [marker("dsh-edge-v0.7.2")]  # прошлый эпизод — другой релиз
    assert ud.signal_pending(decision, markers) is True


def test_last_drift_target_ignores_prose_and_unfinished_markers():
    markers = [
        (utc(2026, 9, 1), "обычное обсуждение, кто-то написал «дрейф пина» в прозе"),
        (utc(2026, 9, 2), marker("dsh-edge-v0.7.9")[1]),
        (utc(2026, 9, 3), "скобка не закрыта: [дрейф пина: без цели"),
    ]
    assert ud.last_drift_target(markers) == "dsh-edge-v0.7.9"


def test_last_drift_target_empty():
    assert ud.last_drift_target([]) is None


# ── Тексты сигнала: детерминированы, несут маркер и процедуру ────────────────────


def test_drift_text_carries_marker_versions_and_procedure():
    text = ud.drift_alert_text(ud.decide_drift(pin(PIN_071), TAGS))
    assert text.startswith("🚨 edge-harness: [дрейф пина: dsh-edge-v0.8.0]")
    assert "dsh-edge-v0.7.1" in text and "dsh-edge-v0.8.0" in text
    assert "https://github.com/pawaca/dsh-edge/releases" in text
    assert "dsh-edge/upstream.json" in text and "dsh-edge/patches" in text
    assert "#161" in text  # разбор чейнджлога — там, здесь только факт дрейфа


def test_pin_not_tag_text_names_the_sha():
    text = ud.drift_alert_text(ud.decide_drift(pin("c" * 40), TAGS))
    assert "[дрейф пина: sha-cccccccccccc]" in text
    assert "c" * 40 in text


# ── Проводка upstream_drift_check на моке gh ─────────────────────────────────────


class FakeGh:
    """Маршрутизатор по подстроке пути, форма test_scheduler.FakeGh: каждый
    вызов пишется — гвардии ниже требуют отсутствия мутаций там, где их быть
    не должно."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        for fragment, result in self.routes.items():
            if fragment in joined:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"нет маршрута для: {joined}")

    def mutating_calls(self):
        return [c for c in self.calls if c.startswith(("-X POST", "-X PUT", "-X DELETE"))]


def wired_fake(*, labels=(), comments=(), tags=TAGS):
    """comments — кортежи marker(): в gh идут прод-формой ответа API
    (issues/{n}/comments), как её реально читает issue_markers_any."""
    return FakeGh({
        # ВАЖНО: маршрут комментариев раньше, чем «issues/134» — FakeGh матчит
        # первую подстроку, и менее специфичный фрагмент перехватил бы комментарии.
        "repos/pawaca/dsh-edge/tags?per_page=100": tags,
        "issues/134/comments?per_page=100": [
            {"body": body, "created_at": when.isoformat().replace("+00:00", "Z")}
            for when, body in comments
        ],
        "issues/134": {"number": 134, "labels": [{"name": name} for name in labels]},
    })


@pytest.fixture()
def offline_telegram(monkeypatch):
    """Без секретов Telegram честно «не доставлен» — сетью тест не ходит."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


def patch_module_gh(monkeypatch, fake):
    """Единственный пункт патча: upstream_drift и pulse_guard (внутри которого
    работают escalate/issue_markers_any) резолвят один и тот же pulse_guard.gh."""
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(ud.pulse_guard, "gh", fake)


def test_check_ok_on_fresh_pin_zero_mutations(monkeypatch, offline_telegram, tmp_path):
    monkeypatch.chdir(tmp_path)  # пин читается по абсолютному PIN_PATH — не мешает
    fake = wired_fake(labels=[], comments=[])
    patch_module_gh(monkeypatch, fake)
    lines = ud.upstream_drift_check("mytab0r/edge-harness")
    assert lines == ["💗 пин апстрима свеж: dsh-edge-v0.8.0 — новейший стабильный релиз pawaca/dsh-edge"]
    assert fake.mutating_calls() == [], f"свежий пин не имеет права менять что-либо: {fake.mutating_calls()}"


def test_check_ok_drops_stale_label_automatic_gas(monkeypatch, offline_telegram):
    fake = wired_fake(labels=["update-available"], comments=[])
    patch_module_gh(monkeypatch, fake)
    lines = ud.upstream_drift_check("mytab0r/edge-harness")
    assert lines and lines[0].startswith("💗")
    deletes = [c for c in fake.calls if "-X DELETE" in c]
    assert any("issues/134/labels/update-available" in c for c in deletes), \
        "газ метки автоматичен: догнавший пин снимает метку сам (docs/agents/LABELS.md)"


def test_check_drift_signals_once_comment_label_telegram(monkeypatch, offline_telegram, tmp_path):
    pin_file = tmp_path / "upstream.json"
    pin_file.write_text(
        '{"repo": "pawaca/dsh-edge", "sha": "%s"}' % PIN_071, encoding="utf-8")
    fake = wired_fake(labels=[], comments=[])
    patch_module_gh(monkeypatch, fake)
    lines = ud.upstream_drift_check("mytab0r/edge-harness", pin_path=pin_file)
    assert lines and lines[0].startswith("🚨 дрейф пина")
    posted = [c for c in fake.calls if "-X POST" in c]
    assert any("repos/mytab0r/edge-harness/issues/134/comments" in c for c in posted), \
        "место правды сигнала — комментарий в #134"
    assert any("issues/134/labels" in c and "update-available" in c for c in posted), \
        "метка update-available ставится вместе с сигналом"
    comment_body = next(c for c in posted if "/comments" in c)
    assert "[дрейф пина: dsh-edge-v0.8.0]" in comment_body, "маркер эпизода — в теле комментария"
    assert "Telegram: НЕ доставлен" in lines[0]  # секретов нет — честный исход


def test_check_drift_silent_on_second_run_same_episode(monkeypatch, offline_telegram, tmp_path):
    pin_file = tmp_path / "upstream.json"
    pin_file.write_text(
        '{"repo": "pawaca/dsh-edge", "sha": "%s"}' % PIN_071, encoding="utf-8")
    fake = wired_fake(labels=["update-available"],
                      comments=[marker("dsh-edge-v0.8.0")])
    patch_module_gh(monkeypatch, fake)
    lines = ud.upstream_drift_check("mytab0r/edge-harness", pin_path=pin_file)
    assert lines == ["🔇 дрейф пина dsh-edge-v0.8.0 уже сигналился в #134 — повтор не шлём"]
    assert fake.mutating_calls() == [], \
        f"второй сигнал на тот же релиз — штурм: {fake.mutating_calls()}"


def test_check_drift_signals_again_for_new_release(monkeypatch, offline_telegram, tmp_path):
    pin_file = tmp_path / "upstream.json"
    pin_file.write_text(
        '{"repo": "pawaca/dsh-edge", "sha": "%s"}' % PIN_071, encoding="utf-8")
    fake = wired_fake(labels=["update-available"],
                      comments=[marker("dsh-edge-v0.7.2")])
    patch_module_gh(monkeypatch, fake)
    lines = ud.upstream_drift_check("mytab0r/edge-harness", pin_path=pin_file)
    assert lines and lines[0].startswith("🚨"), "новый релиз апстрима — новый эпизод"


def test_check_failure_is_loud_runtime_error(monkeypatch, offline_telegram):
    fake = wired_fake()
    fake.routes["repos/pawaca/dsh-edge/tags?per_page=100"] = RuntimeError("API 502")
    patch_module_gh(monkeypatch, fake)
    with pytest.raises(RuntimeError):
        ud.upstream_drift_check("mytab0r/edge-harness")


def test_load_pin_broken_form_is_runtime_error_not_crash(tmp_path):
    bad = tmp_path / "upstream.json"
    bad.write_text('{"repo": "pawaca/dsh-edge"}', encoding="utf-8")
    with pytest.raises(RuntimeError):
        ud.load_pin(bad)
    bad.write_text("не json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        ud.load_pin(bad)


# ── Обёртка планировщика: сбой сверки не роняет пульс и не молчит ────────────────


scheduler_spec = importlib.util.spec_from_file_location("scheduler_for_drift", _DIR / "scheduler.py")
sch = importlib.util.module_from_spec(scheduler_spec)
scheduler_spec.loader.exec_module(sch)  # type: ignore[union-attr]


def test_scheduler_wraps_drift_failure_into_warning_line(monkeypatch):
    def boom(repo):
        raise RuntimeError("API 502")
    monkeypatch.setattr(sch, "upstream_drift_check", boom)
    lines = sch.upstream_drift_lines("mytab0r/edge-harness")
    assert len(lines) == 1 and lines[0].startswith("⚠️ сверка пина")
    assert "дрейф сейчас невидим" in lines[0]


def test_scheduler_passes_drift_lines_through(monkeypatch):
    monkeypatch.setattr(sch, "upstream_drift_check", lambda repo: ["🚨 дрейф пина: …"])
    assert sch.upstream_drift_lines("mytab0r/edge-harness") == ["🚨 дрейф пина: …"]
