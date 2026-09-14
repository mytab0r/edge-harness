"""Тесты механизма issue #1172 (scripts/orchestra/reachability_guard.py):
находит условие срабатывания, физически недостижимое из-за конкурирующего
условия на той же величине (подпись 1), из-за величины, вычисленной и
проверенной в одном такте (подпись 2), или из-за строки, не встречающейся
ни в одном реальном логе (подпись 3).

Критерий приёмки issue #1172 — измеримый: "находит все три случая на
исторических коммитах ДО их фикса и не даёт ложных срабатываний на текущем
main". Здесь — все три случая: 1 и 3 на вендоренных фикстурах PR #1089/#1114,
2 — на вендоренных фикстурах PR #1104 (a414098 до фикса, d848f99 после);
плюс живые проверки рабочего дерева на каждый случай (not_applicable либо
reachable, никогда не «находка»).

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


def test_is_gated_recognizes_any_comparison_form_of_the_same_gate():
    """Находка ревью PR #1180 (блокирующая 1): проба узнавала ровно одну
    запись гейта (`age_minutes >= ИМЯ`); строгий `>`, переставленные операнды
    и другое имя переменной давали False — «reachable» на коде, физически
    неспособном сработать (ложное ОК). Все записи одного гейта обязаны
    узнаваться."""
    forms = [
        "    if age_minutes >= FAST:\n",   # исходная наблюдавшаяся форма
        "    if age_minutes > FAST:\n",    # строгий >
        "    if FAST <= age_minutes:\n",   # переставленные операнды
        "    if run_age >= FAST:\n",       # другое имя величины
    ]
    for gate_line in forms:
        source = "def f():\n" + gate_line + "        resolve_thing()\n"
        assert rg.is_gated_by_age_threshold(source, "resolve_thing", "FAST") is True, gate_line


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


GATE_FORM_MUTATIONS = [
    ("строгий >", "    if age_minutes > WORKER_SILENCE_MINUTES:"),
    ("переставленные операнды", "    if WORKER_SILENCE_MINUTES <= age_minutes:"),
    ("другое имя величины", "    if run_age >= WORKER_SILENCE_MINUTES:"),
]


@pytest.mark.parametrize("label,gate_line", GATE_FORM_MUTATIONS)
def test_case1_mutated_gate_form_must_not_give_reachable(label, gate_line):
    """МУТАЦИИ находки ревью PR #1180 (блокирующая 1): беру РЕАЛЬНУЮ после-
    фикстуру (зелёный текст после фикса #1089) и текстуально возвращаю гейт
    в одной из записей, отличных от той единственной, что узнавала прежняя
    проба. Каждая из трёх форм физически та же недостижимость (наблюдение
    появляется не раньше порога, тишина — не раньше 2×порога=300 >= 295),
    и НИ НА ОДНОЙ вердикт не имеет права быть «reachable» (ложное ОК)."""
    fixed_source = read_fixture("scheduler_after_fix_reap.py.txt")
    assert "task_number = _stalled_run_task_number(repo, pool, pulls, start)" in fixed_source
    mutated = fixed_source.replace(
        "    task_number = _stalled_run_task_number(repo, pool, pulls, start)",
        "    task_number = None\n"
        f"{gate_line}\n"
        "        task_number = _stalled_run_task_number(repo, pool, pulls, start)",
    )
    verdict = rg.check_gate_then_duration_pair(
        mutated,
        container_func="reap_stalled_worker_run",
        resolve_call="_stalled_run_task_number",
        fast_const="WORKER_SILENCE_MINUTES",
        slow_const="WORKER_STALL_MINUTES",
    )
    assert verdict.status == "unreachable", f"форма «{label}»: {verdict.detail}"


def test_case1_live_worktree_scheduler_stays_reachable():
    """#1085 (PR #1089) уже несёт пару WORKER_SILENCE_MINUTES/WORKER_STALL_
    MINUTES в живом scheduler.py этого дерева (заменяет прежнюю "not_
    applicable"-версию теста, писавшуюся ДО того, как #1089 довели фиксом —
    см. историю этого файла) — живая регрессионная гвардия: КАЖДЫЙ пульс
    резолвит task_number без возрастного гейта (round 1 фикса #1089), поэтому
    вердикт обязан оставаться "reachable" с тем же min=WORKER_SILENCE_MINUTES
    (150), не откатываться к "unreachable" (300, находка ai-review) молча."""
    live_path = REPO_ROOT / "scripts" / "orchestra" / "scheduler.py"
    source = live_path.read_text(encoding="utf-8")
    verdict = rg.check_gate_then_duration_pair(
        source,
        container_func="reap_stalled_worker_run",
        resolve_call="_stalled_run_task_number",
        fast_const="WORKER_SILENCE_MINUTES",
        slow_const="WORKER_STALL_MINUTES",
    )
    assert verdict.status == "reachable", (
        f"неожиданно {verdict.status} (не 'reachable') — регресс класса issue #1172 "
        f"на живом scheduler.py этого PR: {verdict.detail}"
    )
    assert verdict.min_reachable_minutes == 150.0, verdict.detail


# ── Случай 3 (issue #1172): dsh-ci.sh, INVALID_API_KEY как стоп-класс ───────

def test_case3_invalid_api_key_stop_class_unconfirmed_before_fix():
    source = read_fixture("dsh_ci_before_fix_stop_classes.sh.txt")
    corpus = read_fixture("real_error_log_corpus.txt")
    verdict = rg.check_stop_signature_in_corpus(source, "INVALID_API_KEY:", corpus)
    assert verdict.status == "unconfirmed", verdict.detail


def test_case3_signature_present_in_real_corpus_is_confirmed():
    # Позитивный контроль на РЕАЛЬНОМ корпусе, не синтетике (замечание ревью
    # PR #1180): цитата «Server Error (HTTP 502)» действительно есть в
    # real_error_log_corpus.txt (job `review`, run 34748740141, PR #1089) —
    # сигнатура, реально встречающаяся в корпусе, обязана классифицироваться
    # как confirmed, иначе проверка красит ВСЁ подряд и ничего не доказывает.
    source = (
        "  if grep -qE 'HTTP 502' \"$err_file\"; then\n"
        "    return 1\n"
        "  fi\n"
    )
    corpus = read_fixture("real_error_log_corpus.txt")
    assert "HTTP 502" in corpus, "предпосылка контроля нарушена: сигнатуры нет в реальном корпусе"
    verdict = rg.check_stop_signature_in_corpus(source, "HTTP 502", corpus)
    assert verdict.status == "confirmed", verdict.detail


def test_case3_active_stop_branch_in_later_block_is_not_missed():
    """Замечание ревью PR #1180 (чеклист): литерал может встретиться в тексте
    ДВАЖДЫ — сначала переключаемой веткой с `return 0`, потом активным
    стоп-классом с `return 1`. Поиск только по ПЕРВОМУ совпадению молчал бы
    (`not_applicable`) о реально активном стоп-классе: проверяются ВСЕ
    if/fi-блоки с литералом."""
    source = (
        "  if grep -qE 'TIMEOUT:' \"$err_file\"; then\n"
        "    return 0  # переключаемый класс, не стоп\n"
        "  fi\n"
        "cond() {\n"
        "  if grep -qE 'TIMEOUT:' \"$err_file\"; then\n"
        "    return 1  # активный стоп-класс\n"
        "  fi\n"
        "}\n"
    )
    corpus = "real log line: connection reset by peer\n"
    verdict = rg.check_stop_signature_in_corpus(source, "TIMEOUT:", corpus)
    # корпус намеренно БЕЗ литерала: «unconfirmed» доказывает, что проверка
    # увидела активный стоп-класс во ВТОРОМ блоке (не молчала not_applicable
    # по первому блоку с return 0)
    assert verdict.status == "unconfirmed", verdict.detail


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


# ── Регрессия прочёса #1184: ложное срабатывание на РЕАЛЬНО присутствующей
# на main сигнатуре (STREAM_CLOSED:, восемь return 0, ни одного return 1) ──

def test_case3_stream_closed_present_on_main_is_not_a_stop_class():
    """STREAM_CLOSED: РЕАЛЬНО встречается в текущем dsh-ci.sh (grep -qE
    'STREAM_CLOSED:' существует), но её собственный if/fi-блок кончается
    `return 0` (переключаемый класс, #1084) — единственный `return 1` в
    файле принадлежит СОВСЕМ ДРУГОЙ функции (dsh_provider_quota_gate_skip,
    #857), дальше по файлу. Старый неограниченный поиск дотягивался туда и
    давал ложный `unconfirmed`; проверка обязана сказать `not_applicable`,
    ровно как для сигнатуры, которой в файле вовсе нет."""
    live_path = REPO_ROOT / "scripts" / "lib" / "dsh-ci.sh"
    source = live_path.read_text(encoding="utf-8")
    assert "grep -qE 'STREAM_CLOSED:'" in source, "фикстура протухла — сигнатура ушла из dsh-ci.sh"
    assert source.count("return 1") >= 1, "в файле обязан быть хотя бы один return 1 (иначе тест не различает баг)"
    corpus = read_fixture("real_error_log_corpus.txt")
    verdict = rg.check_stop_signature_in_corpus(source, "STREAM_CLOSED:", corpus)
    assert verdict.status == "not_applicable", (
        f"ложное срабатывание (класс прочёса #1184): {verdict.status} — {verdict.detail}"
    )


def test_case3_mutation_unbounded_search_reintroduces_stream_closed_false_positive():
    """ДОКАЗАТЕЛЬСТВО МУТАЦИЕЙ (обратное — воспроизводит СТАРЫЙ баг): текст
    `dsh-ci.sh` не трогаю, мутирую саму функцию проверки на старую,
    неограниченную форму — она обязана вернуть `unconfirmed` на реальном
    main (ровно баг, найденный прочёсом), доказывая, что фикс (граница по
    fi) — не случайность фикстуры."""
    live_path = REPO_ROOT / "scripts" / "lib" / "dsh-ci.sh"
    source = live_path.read_text(encoding="utf-8")
    corpus = read_fixture("real_error_log_corpus.txt")

    def unbounded_check(source: str, stop_pattern: str, corpus_text: str):
        import re
        pattern = re.compile(
            r"grep\s+-qE\s+'" + re.escape(stop_pattern) + r"'.*\n(?:.*\n)*?\s*return 1",
        )
        if pattern.search(source) is None:
            return rg.SignatureVerdict("not_applicable", "старая форма: не найдено")
        return rg.SignatureVerdict("unconfirmed", "старая форма: return 1 найден где-то дальше")

    mutated_verdict = unbounded_check(source, "STREAM_CLOSED:", corpus)
    assert mutated_verdict.status == "unconfirmed", (
        "мутация обязана воспроизвести старый баг (ложный unconfirmed) — если она этого не "
        "делает, тест выше не доказывает, что фикс закрывает именно этот класс"
    )


# ── Живая проверка простого порядка на main (ответ на находку прочёса #1184,
# п.2 — «на живом коде подпись 1 не проверяет ничего») ──────────────────────

def test_worker_stall_minutes_stays_below_worker_yml_wall_on_main():
    """WORKER_STALL_MINUTES (scheduler.py) обязан оставаться НИЖЕ жёсткой
    стены job'а worker.yml (timeout-minutes) — запас уже задокументирован
    как осознанное решение в комментарии над самой константой (scheduler.py,
    #1067: 295 = 270+25, на 45 мин ниже 340-минутной стены). Простая форма
    (не «гейт+длительность» случая 1, gated=False) — единственная сегодня
    живая пара, проверяемая этим механизмом на текущем main, а не только на
    вендоренных исторических фикстурах."""
    scheduler_source = (REPO_ROOT / "scripts" / "orchestra" / "scheduler.py").read_text(encoding="utf-8")
    workflow_source = (REPO_ROOT / ".github" / "workflows" / "worker.yml").read_text(encoding="utf-8")
    stall = rg.extract_constant(scheduler_source, "WORKER_STALL_MINUTES")
    wall = rg.extract_yaml_number(workflow_source, "timeout-minutes")
    assert stall is not None and wall is not None, "константы ушли из ожидаемых мест — обнови извлечение"
    verdict = rg.race_verdict(stall, wall, gated=False)
    assert verdict.status == "reachable", verdict.detail


# ── Вырожденная пара (находка прочёса #1184, п.1): fast_const == slow_const ──

def test_check_gate_then_duration_pair_degenerate_when_same_constant_passed_twice():
    source = read_fixture("scheduler_before_fix_reap.py.txt")
    verdict = rg.check_gate_then_duration_pair(
        source,
        container_func="reap_stalled_worker_run",
        resolve_call="_stalled_run_task_number",
        fast_const="WORKER_SILENCE_MINUTES",
        slow_const="WORKER_SILENCE_MINUTES",  # по ошибке та же константа дважды
    )
    assert verdict.status == "degenerate", verdict.detail


# ── Случай 2 (issue #1172): harness.ts, алерт на записи этого же тика ────────
# Исторические фикстуры — ДОСЛОВНЫЕ фрагменты cf-worker/src/harness.ts на
# a414098 (до фикса PR #1104) и d848f99 (после) — вендорятся по той же
# причине, что и фикстуры случая 1 выше.

CASE2_CONSUMER = "#tickPulseAlert"
CASE2_TEXT_FUNC = "pulseAlertText"


def test_case2_pulse_alert_age_branch_unreachable_before_fix():
    source = read_fixture("harness_before_fix_pulse_alert.ts.txt")
    verdict = rg.check_same_tick_age_branch(source, CASE2_CONSUMER, CASE2_TEXT_FUNC)
    assert verdict.status == "unreachable", verdict.detail


def test_case2_pulse_alert_age_branch_reachable_after_fix():
    source = read_fixture("harness_after_fix_pulse_alert.ts.txt")
    verdict = rg.check_same_tick_age_branch(source, CASE2_CONSUMER, CASE2_TEXT_FUNC)
    # Фикс PR #1104: pulseAlertText(lastPulse) — сигнатура без `now`; факты
    # «тексту уходит now» и «возраст считается от now и ts» невоспроизводимы,
    # сама форма бага исчезла — reachable, не not_applicable (проверка
    # выполнена, нарушений нет).
    assert verdict.status == "reachable", verdict.detail


def test_case2_no_false_positive_on_current_worktree_harness():
    """Текущий harness.ts (фикс #1104 на main) не должен давать «unreachable»:
    запись этого же тика по-прежнему кормит #tickPulseAlert, но текстовая
    функция больше не принимает `now` — форма недостижимой ветки
    невоспроизводима. Живая проверка — обязательная часть критерия задачи
    («не даёт ложных срабатываний на текущем main»)."""
    live_path = REPO_ROOT / "cf-worker" / "src" / "harness.ts"
    source = live_path.read_text(encoding="utf-8")
    verdict = rg.check_same_tick_age_branch(source, CASE2_CONSUMER, CASE2_TEXT_FUNC)
    assert verdict.status == "reachable", (
        f"неожиданно {verdict.status} — возрастная ветка текста алерта вернулась в "
        f"harness.ts или проба перестала узнавать зафиксированную форму: {verdict.detail}"
    )


def test_case2_mutation_reintroducing_now_into_alert_text_flips_to_unreachable():
    """ДОКАЗАТЕЛЬСТВО МУТАЦИЕЙ: беру ПОСЛЕ-фикстуру (реальный зелёный текст)
    и текстуально возвращаю обе правки фикса #1104 — `now` в вызов текста и
    `now` в сигнатуру с возрастной строкой. Guard обязан снова увидеть
    unreachable, доказывая, что вердикт зависит от факта в коде, а не от
    случайности фикстуры."""
    fixed_source = read_fixture("harness_after_fix_pulse_alert.ts.txt")
    assert "text: pulseAlertText(lastPulse)," in fixed_source
    mutated = fixed_source.replace(
        "text: pulseAlertText(lastPulse),",
        "text: pulseAlertText(now, lastPulse),",
    ).replace(
        "export function pulseAlertText(lastPulse: PulseStatus): string {",
        "export function pulseAlertText(now: number, lastPulse: PulseStatus): string {\n"
        "  const minutes = Math.round((now - lastPulse.ts) / 60_000);",
    )
    verdict = rg.check_same_tick_age_branch(mutated, CASE2_CONSUMER, CASE2_TEXT_FUNC)
    assert verdict.status == "unreachable", verdict.detail


def test_case2_not_applicable_when_any_fact_missing():
    # «Доказывай доказыватель»: отсутствие любого из трёх фактов — третье,
    # отдельное состояние, не молчаливое «reachable».
    no_same_tick = rg.check_same_tick_age_branch(
        "x = 1\n", CASE2_CONSUMER, CASE2_TEXT_FUNC
    )
    assert no_same_tick.status == "not_applicable"
    same_tick_only = (
        "this.#tickPulseAlert(now, { ts: now, dispatch_ok: true });\n"
    )
    assert rg.check_same_tick_age_branch(same_tick_only, CASE2_CONSUMER, CASE2_TEXT_FUNC).status == "not_applicable"


def test_extract_braced_source_bounds_at_balanced_brace():
    source = "class A {\n  m(): void {\n    if (x) { y(); }\n  }\n  other(): void {}\n}\n"
    body = rg.extract_braced_source(source, r"^\s*m\(")
    assert body is not None
    assert "y();" in body
    assert "other" not in body
    assert rg.extract_braced_source(source, r"^\s*missing\(") is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
