"""Регрессия на конкретную находку issue #649 при разработке носителя: сдвиг
часов обязан стартовать хуком `pytest_configure` (ДО коллекции тестов), не
autouse-фикстурой сессии (ПОСЛЕ коллекции) — иначе он не патчит модули,
загруженные идиомой `importlib.util.spec_from_file_location(...) →
module_from_spec → exec_module` без регистрации в `sys.modules` (так грузят
скрипты как модули 33 файла теста этого репозитория, включая
scripts/review/test_ai_review.py). Живой случай: `ai_review.py`, загруженный
ДО старта freeze (порядок autouse-фикстуры), сохранял настоящий
`datetime.datetime` — тест на возраст комментария в `manual_dispatch_skip_reason`
ложно показывал 0 минут вместо ожидаемых ~12.

Если это регрессирует обратно на autouse-фикстуру, тест ниже краснеет сразу,
без необходимости гонять весь scripts/ на горизонте, чтобы заметить дыру.

Запуск: python -m pytest scripts/test_conftest_clock_shift.py -q
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFTEST = Path(__file__).with_name("conftest.py")
spec = importlib.util.spec_from_file_location("conftest_under_test", CONFTEST)
conftest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conftest)  # type: ignore[union-attr]


def test_shift_is_started_by_pytest_configure_hook_not_a_fixture():
    """Класс-гвардия по исходнику: возврат к autouse-фикстуре сессии не
    ловит модули без sys.modules — хук обязан остаться pytest_configure."""
    assert hasattr(conftest, "pytest_configure")
    assert hasattr(conftest, "pytest_unconfigure")
    assert not hasattr(conftest, "_clock_shift_session"), (
        "вернулась autouse-фикстура сессии — тот же класс дыры, что и до фикса "
        "(freeze стартует ПОСЛЕ коллекции, поздно для importlib-модулей)"
    )


def test_configure_before_load_patches_module_loaded_without_sys_modules_registration(
    monkeypatch,
):
    """Прямое воспроизведение находки: модуль, загруженный БЕЗ регистрации в
    sys.modules (ровно идиома scripts/review/test_ai_review.py и ещё 32
    файлов), обязан получить FakeDatetime, если pytest_configure уже
    отработал к моменту его exec_module."""
    monkeypatch.setenv(conftest.CLOCK_SHIFT_DAYS_ENV, "8")
    try:
        conftest.pytest_configure(config=None)

        script = Path(__file__).with_name("measure") / "dispatch_tail.py"
        late_spec = importlib.util.spec_from_file_location(
            "regression_late_module_no_sys_modules_registration", script)
        late_module = importlib.util.module_from_spec(late_spec)
        assert "regression_late_module_no_sys_modules_registration" not in sys.modules
        late_spec.loader.exec_module(late_module)  # НЕ регистрируем в sys.modules

        assert late_module.datetime.__name__ == "FakeDatetime", (
            "модуль, загруженный после pytest_configure, не получил "
            "подменённый datetime — сдвиг снова стартует слишком поздно"
        )
        now_in_module = late_module.datetime.now(timezone.utc)
        real_now = datetime.now(timezone.utc)
        # real_now здесь тоже читает FakeDatetime (freeze патчит и текущий
        # процесс теста) — сравниваем оба сдвинутых значения между собой.
        assert abs((now_in_module - real_now).total_seconds()) < 5
        assert (now_in_module - timedelta(days=8)).date() <= datetime.now(timezone.utc).date()
    finally:
        conftest.pytest_unconfigure(config=None)


def test_unconfigure_stops_the_freezer_and_restores_real_clock(monkeypatch):
    monkeypatch.setenv(conftest.CLOCK_SHIFT_DAYS_ENV, "8")
    before = datetime.now(timezone.utc)
    conftest.pytest_configure(config=None)
    shifted = datetime.now(timezone.utc)
    conftest.pytest_unconfigure(config=None)
    after = datetime.now(timezone.utc)

    assert (shifted - before).days >= 7  # сдвиг реально применился
    assert (after - before).total_seconds() < 5  # а после stop() часы настоящие снова
    assert conftest._freezer is None


def test_missing_freezegun_fails_loud_not_silently_on_real_time(monkeypatch):
    """Пункт 3 задания #649: без freezegun при заданном CLOCK_SHIFT_DAYS —
    громкий RuntimeError, не тихий прогон по настоящему времени."""
    monkeypatch.setenv(conftest.CLOCK_SHIFT_DAYS_ENV, "8")
    monkeypatch.setitem(sys.modules, "freezegun", None)  # имитирует ImportError
    try:
        import pytest as _pytest
        with _pytest.raises(RuntimeError, match="freezegun не установлен"):
            conftest.pytest_configure(config=None)
    finally:
        monkeypatch.undo()


def test_zero_or_unset_shift_is_a_noop_and_does_not_import_freezegun(monkeypatch):
    """Имя обещало проверку отсутствия импорта freezegun — раньше здесь был
    мёртвый assert (`"freezegun" not in sys.modules or True`, всегда True,
    находка второго гейта ревью PR #667). freezegun мог быть уже импортирован
    другим тестом сессии, поэтому честная проверка не «нет в sys.modules», а
    «no-op не трогает _freezer» — это и есть предмет теста."""
    monkeypatch.delenv(conftest.CLOCK_SHIFT_DAYS_ENV, raising=False)
    conftest.pytest_configure(config=None)
    assert conftest._freezer is None
    conftest.pytest_unconfigure(config=None)  # не падает на пустом _freezer
