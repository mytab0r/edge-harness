"""Тесты механизма issue #1172 (scripts/orchestra/reachability_guard.py):
находит условие срабатывания, физически недостижимое из-за конкурирующего
условия на той же величине (подпись 1) или из-за строки, не встречающейся ни
в одном реальном логе (подпись 3).

Критерий приёмки issue #1172 — измеримый: "находит все три случая на
исторических коммитах ДО их фикса и не даёт ложных срабатываний на текущем
main". Здесь — случаи 1 и 3 (случай 2 не формализован как отдельный
детектор, см. докстринг reachability_guard.py, раздел "НЕ подтверждено"/
преамбула о случае 2).

Запуск: python -m pytest scripts/orchestra/test_reachability_guard.py -q
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("reachability_guard.py")
_spec = importlib.util.spec_from_file_location("reachability_guard", SCRIPT)
rg = importlib.util.module_from_spec(_spec)
sys.modules["reachability_guard"] = rg  # dataclass() резолвит типы через sys.modules[cls.__module__]
_spec.loader.exec_module(rg)  # type: ignore[union-attr]

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("testdata") / "reachability"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── Ядро арифметики (мутация: границу `<` легко сломать на `<=`) ───────────

def test_race_verdict_unreachable_when_gated_doubles_past_slow_threshold():
    verdict = rg.race_verdict(fast_threshold=150.0, slow_threshold=295.0, gated=True)
    assert verdict.status == "unreachable"
    assert verdict.min_reachable_minutes == 300.0


def test_race_verdict_reachable_when_ungated():
    verdict = rg.race_verdict(fast_threshold=150.0, slow_threshold=295.0, gated=False)
    assert verdict.status == "reachable"
    assert verdict.min_reachable_minutes == 150.0


def test_race_verdict_boundary_equal_is_unreachable_not_reachable():
    # Мутация "<" -> "<=" в race_verdict должна покраснить этот тест: на
    # РОВНО равных порогах быстрый нож срабатывает НЕ РАНЬШЕ медленного
    # (совпадение по времени не считается опережением).
    verdict = rg.race_verdict(fast_threshold=100.0, slow_threshold=100.0, gated=False)
    assert verdict.status == "unreachable"


def test_race_verdict_not_applicable_when_any_fact_missing():
    # "Доказывай доказыватель": отсутствие факта — третье, отдельное
    # состояние, не молчаливое "reachable".
    assert rg.race_verdict(None, 295.0, False).status == "not_applicable"
    assert rg.race_verdict(150.0, None, False).status == "not_applicable"
    assert rg.race_verdict(150.0, 295.0, None).status == "not_applicable"


# ── Извлечение фактов из текста источника ───────────────────────────────────

def test_extract_constant_reads_module_level_int():
    source = "FOO = 42\nBAR = 3.5\n"
    assert rg.extract_constant(source, "FOO") == 42.0
    assert rg.extract_constant(source, "BAR") == 3.5
    assert rg.extract_constant(source, "MISSING") is None


def test_extract_function_source_bounds_at_next_def():
    source = "def a():\n    pass\n\n\ndef b():\n    pass\n"
    body = rg.extract_function_source(source, "a")
    assert body is not None
    assert "def a" in body
    assert "def b" not in body
    assert rg.extract_function_source(source, "missing") is None


def test_is_gated_returns_none_when_resolve_call_absent():
    assert rg.is_gated_by_age_threshold("def f():\n    pass\n", "resolve_thing", "FAST") is None


def test_is_gated_true_when_call_follows_age_condition():
    source = (
        "def f():\n"
        "    if age_minutes >= FAST:\n"
        "        resolve_thing()\n"
    )
    assert rg.is_gated_by_age_threshold(source, "resolve_thing", "FAST") is True


def test_is_gated_false_when_call_is_unconditional():
    source = "def f():\n    resolve_thing()\n"
    assert rg.is_gated_by_age_threshold(source, "resolve_thing", "FAST") is False


# ── Случай 1 (issue #1172): scheduler.py, WORKER_STALL_MINUTES/WORKER_SILENCE_MINUTES ──
# Историческая фикстура — ДОСЛОВНЫЙ фрагмент scheduler.py на коммитах PR
# #1089 (не читается через `git show <sha>` в момент теста — см. докстринг
# фикстур, коммит живёт на ветке PR, не в main).

def test_case1_worker_silence_race_unreachable_before_fix():
    source = read_fixture("scheduler_before_fix_reap.py.txt")
    verdict = rg.check_gate_then_duration_pair(
        source,
        container_func="reap_stalled_worker_run",
        resolve_call="_stalled_run_task_number",
        fast_const="WORKER_SILENCE_MINUTES",
        slow_const="WORKER_STALL_MINUTES",
    )
    assert verdict.status == "unreachable", verdict.detail
    assert verdict.min_reachable_minutes == 300.0  # 2 * 150 >= 295


def test_case1_worker_silence_race_reachable_after_fix():
    source = read_fixture("scheduler_after_fix_reap.py.txt")
    verdict = rg.check_gate_then_duration_pair(
        source,
        container_func="reap_stalled_worker_run",
        resolve_call="_stalled_run_task_number",
        fast_const="WORKER_SILENCE_MINUTES",
        slow_const="WORKER_STALL_MINUTES",
    )
    assert verdict.status == "reachable", verdict.detail
    assert verdict.min_reachable_minutes == 150.0


def test_case1_mutation_reverting_fix_text_flips_verdict_back_to_unreachable():
    """ДОКАЗАТЕЛЬСТВО МУТАЦИЕЙ: беру ПОСЛЕ-фикстуру (реальный зелёный текст)
    и текстуально возвращаю ровно тот гейт, что был до фикса (вставляю `if
    age_minutes >= WORKER_SILENCE_MINUTES:` перед резолвом) — guard обязан
    покраснеть (снова увидеть unreachable), доказывая, что вердикт зависит
    от факта в коде, а не от случайности фикстуры."""
    fixed_source = read_fixture("scheduler_after_fix_reap.py.txt")
    assert "task_number = _stalled_run_task_number(repo, pool, pulls, start)" in fixed_source
    mutated_back = fixed_source.replace(
        "    task_number = _stalled_run_task_number(repo, pool, pulls, start)",
        "    task_number = None\n"
        "    if age_minutes >= WORKER_SILENCE_MINUTES:\n"
        "        task_number = _stalled_run_task_number(repo, pool, pulls, start)",
    )
    verdict = rg.check_gate_then_duration_pair(
        mutated_back,
        container_func="reap_stalled_worker_run",
        resolve_call="_stalled_run_task_number",
        fast_const="WORKER_SILENCE_MINUTES",
        slow_const="WORKER_STALL_MINUTES",
    )
    assert verdict.status == "unreachable", verdict.detail


def test_case1_no_false_positive_on_current_worktree_scheduler():
    """На текущем main/рабочем дереве этой пары констант ещё нет вовсе (PR
    #1089, задача #1085, не влит на момент этого PR) — guard обязан сказать
    "not_applicable" (проверка не выполнялась), а НЕ "reachable" (что было
    бы враньём — по факту пары constants нет)."""
    live_path = REPO_ROOT / "scripts" / "orchestra" / "scheduler.py"
    source = live_path.read_text(encoding="utf-8")
    verdict = rg.check_gate_then_duration_pair(
        source,
        container_func="reap_stalled_worker_run",
        resolve_call="_stalled_run_task_number",
        fast_const="WORKER_SILENCE_MINUTES",
        slow_const="WORKER_STALL_MINUTES",
    )
    assert verdict.status == "not_applicable", (
        f"неожиданно {verdict.status} — если #1089 уже влит, обнови фикстуру/тест "
        f"(constants теперь есть в живом scheduler.py): {verdict.detail}"
    )


# ── Случай 3 (issue #1172): dsh-ci.sh, INVALID_API_KEY как стоп-класс ───────

def test_case3_invalid_api_key_stop_class_unconfirmed_before_fix():
    source = read_fixture("dsh_ci_before_fix_stop_classes.sh.txt")
    corpus = read_fixture("real_error_log_corpus.txt")
    verdict = rg.check_stop_signature_in_corpus(source, "INVALID_API_KEY:", corpus)
    assert verdict.status == "unconfirmed", verdict.detail


def test_case3_rate_limit_signature_is_confirmed_by_real_corpus():
    # Позитивный контроль: сигнатура, реально встречающаяся в корпусе,
    # обязана классифицироваться как confirmed, не unconfirmed — иначе
    # проверка красит ВСЁ подряд и ничего не доказывает.
    source = (
        "  if grep -qE 'RATE_LIMIT:' \"$err_file\"; then\n"
        "    return 1\n"
        "  fi\n"
    )
    corpus = "some line mentioning rate_limit: retry budget exceeded\n"
    verdict = rg.check_stop_signature_in_corpus(source, "RATE_LIMIT:", corpus)
    assert verdict.status == "confirmed", verdict.detail


def test_case3_no_false_positive_on_current_dsh_ci():
    """Текущий dsh-ci.sh (после доводки #1084) не держит НИ ОДНОГО именованного
    стоп-класса на литерале INVALID_API_KEY (снят целиком, feca9e9d) —
    "not_applicable" (класс не воспроизводится этим текстом), не находка."""
    live_path = REPO_ROOT / "scripts" / "lib" / "dsh-ci.sh"
    source = live_path.read_text(encoding="utf-8")
    corpus = read_fixture("real_error_log_corpus.txt")
    verdict = rg.check_stop_signature_in_corpus(source, "INVALID_API_KEY:", corpus)
    assert verdict.status == "not_applicable", (
        f"неожиданно {verdict.status} — если стоп-класс вернулся в dsh-ci.sh, "
        f"подтверди его реальной цитатой в корпусе или сними: {verdict.detail}"
    )


def test_signature_in_corpus_is_case_insensitive_substring():
    assert rg.signature_in_corpus("Rate_Limit:", "видел RATE_LIMIT: retry\n") is True
    assert rg.signature_in_corpus("no_such_signature", "видел RATE_LIMIT: retry\n") is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
