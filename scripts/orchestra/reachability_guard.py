"""Механизм для issue #1172: находит условие срабатывания, которое физически
недостижимо, потому что раньше срабатывает конкурирующее условие, режущее ту
же величину, либо потому что распознаваемая строка не встречается ни в одном
реальном логе. Не пытается решить класс целиком (общий случай — по сути
проблема останова, см. AGENTS.md/issue #1172, п.3 задачи) — закрывает ДВЕ
узкие, реально наблюдавшиеся подписи:

  1. «Два ножа режут одну величину, гейт блокирует быстрый» (случай 1 issue
     #1172 — PR #1089/задача #1085): быстрое условие нуждается в наблюдении,
     которое само появляется не раньше, чем сработает МЕДЛЕННЫЙ порог (или
     тот же самый быстрый порог как гейт) — из-за этого быстрое условие
     физически не может сработать раньше медленного.
  2. «Распознаваемая строка не встречается ни в одном реальном логе» (случай
     3 issue #1172 — PR #1114/задача #1084): именованный стоп-класс держится
     на литерале, который не наблюдался ни разу за пределами синтетической
     фикстуры смоука — приём взят из PR #1177 (INFRA_ERROR_SIGNATURES),
     формализован здесь как переиспользуемая функция.

Случай 2 issue #1172 (алерт, вычисленный и проверенный в одном такте,
PR #1104/задача #1103) НЕ формализован здесь как отдельный детектор: класс
«значение вычислено и проверено в одном вызове» требует dataflow-анализа
произвольного кода (кто передал `now` туда же, откуда его прочитали) — это
тот же неразрешимый в общем виде случай, что и весь issue #1172 целиком.
Единственная закрытая здесь польза от случая 2 — фикс уже на main
(`pulseAlertText(lastPulse)` не принимает `now` вовсе, недостижимая ветка
физически не может быть написана без изменения сигнатуры — сама сигнатура
это и есть страховка, отдельного детектора не требует).

НЕ подтверждено: формула «гейт+длительность» (`_min_reachable_minutes`)
верна для КОНКРЕТНОЙ формы гейта, разобранной здесь (`age_minutes >= ИМЯ`
перед вызовом, резолвящим наблюдение). Для другой формы гейта тот же баг
дал бы `gated=None` (probe не нашёл сигнатуру) — «неприменимо», не «ложное
ОК»: см. `RaceVerdict.status`.

Живая проверка на main (не только исторические фикстуры): подпись 1
(«гейт+длительность») сегодня НЕ зарегистрирована ни на одной живой паре —
единственная известная пара этой формы (WORKER_STALL_MINUTES/
WORKER_SILENCE_MINUTES) существует только в неслитом PR #1089 (задача
#1085), в main её нет вовсе (см. `test_case1_no_false_positive_on_current_
worktree_scheduler`, статус `not_applicable`). Другие пары порогов на одну
величину, встреченные попутно (WORKER_STALL_MINUTES/стена `worker.yml`,
интервал `alarm()`/`pulseNeedsRecoveryDispatch`, троттлинг quota-watch/
`MEASUREMENT_STALE_MINUTES`) — РАЗНОЙ формы (простой потолок «А должно
остаться ниже Б», не «гейт блокирует резолв той же величины») и не
регистрируются здесь автоматически: `race_verdict(gated=False)` подходит
для них арифметически, но заявлять их «проверенными» без разбора КАЖДОЙ
формы гейта означало бы повторить ровно ту ошибку, которую чинит этот файл
(находка прочёса #1184: ложное срабатывание из-за неограниченного поиска —
см. `check_stop_signature_in_corpus` ниже). Зарегистрирована ОДНА живая
пара такой простой формы — `test_worker_stall_minutes_stays_below_worker_
yml_wall_on_main` (WORKER_STALL_MINUTES < `worker.yml` timeout-minutes),
уже задокументированная как осознанный запас в комментарии над самой
константой (scheduler.py:2492 и выше).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# ── Подпись 1: гейт блокирует быстрый нож ────────────────────────────────────

def extract_constant(source: str, name: str) -> float | None:
    """Значение `NAME = <число>` на уровне модуля — читает из ЖИВОГО текста
    источника (или его вендоренной фикстуры), не дублирует число руками."""
    match = re.search(rf"^{re.escape(name)}\s*=\s*([0-9]+(?:\.[0-9]+)?)", source, re.MULTILINE)
    return float(match.group(1)) if match is not None else None


def extract_yaml_number(source: str, key: str) -> float | None:
    """Значение `key: <число>` YAML-строкой (например `timeout-minutes: 340`
    в workflow-файле) — тот же принцип, что `extract_constant`, для формы,
    где число не Python-присваивание."""
    match = re.search(rf"^\s*{re.escape(key)}\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*$", source, re.MULTILINE)
    return float(match.group(1)) if match is not None else None


def extract_function_source(source: str, func_name: str) -> str | None:
    """Тело функции `func_name` от `def func_name(` до следующего `def ` на
    уровне модуля (или до конца текста) — `None`, если такой функции в этом
    тексте нет вовсе (другая форма кода, проверка неприменима)."""
    match = re.search(rf"^def {re.escape(func_name)}\(", source, re.MULTILINE)
    if match is None:
        return None
    tail = source[match.end():]
    next_def = re.search(r"^def ", tail, re.MULTILINE)
    end = match.end() + next_def.start() if next_def is not None else len(source)
    return source[match.start():end]


def is_gated_by_age_threshold(function_source: str, resolve_call: str, gate_const_name: str) -> bool | None:
    """True — вызов `resolve_call(` в тексте функции стоит ПОСЛЕ условия,
    сравнивающего `age_minutes` с `gate_const_name` (гейт «сначала возраст,
    потом наблюдение» — ровно форма бага случая 1). False — вызов найден, но
    гейта перед ним нет (безусловный вызов, наблюдение доступно с первого
    пульса). `None` — сам `resolve_call` не встречается в тексте вовсе:
    «неприменимо», не «безопасно» (доказывай доказыватель, AGENTS.md)."""
    call_index = function_source.find(f"{resolve_call}(")
    if call_index == -1:
        return None
    before_call = function_source[:call_index]
    gate_pattern = re.compile(r"age_minutes\s*>=\s*" + re.escape(gate_const_name))
    return gate_pattern.search(before_call) is not None


@dataclass(frozen=True)
class RaceVerdict:
    status: str  # "not_applicable" | "degenerate" | "unreachable" | "reachable"
    min_reachable_minutes: float | None
    detail: str


def race_verdict(fast_threshold: float | None, slow_threshold: float | None, gated: bool | None) -> RaceVerdict:
    """Мехническое ядро подписи 1 — чистая арифметика поверх ТРЁХ уже
    извлечённых фактов, ни одного захардкоженного числа:
      - `gated=True`  → быстрое условие недостижимо раньше `2 * fast_threshold`
        (сначала нужно ДОЖДАТЬСЯ гейта `fast_threshold`, потом ЕЩЁ
        `fast_threshold` тишины/бездействия для срабатывания самого условия);
      - `gated=False` → достижимо с первого пульса, порог — сам `fast_threshold`;
      - любой факт не найден (`None`) → `not_applicable`, не `reachable` —
        проверка не выполнялась, а не «прошла»."""
    if fast_threshold is None or slow_threshold is None or gated is None:
        return RaceVerdict(
            "not_applicable", None,
            "константы или форма кода не найдены в этом тексте — проверка не выполнялась "
            "(не путать с «нарушений не найдено»)",
        )
    min_reachable = fast_threshold * 2 if gated else fast_threshold
    if min_reachable < slow_threshold:
        return RaceVerdict(
            "reachable", min_reachable,
            f"быстрый нож достижим на {min_reachable:g} мин < медленного порога {slow_threshold:g} мин "
            f"(gated={gated})",
        )
    return RaceVerdict(
        "unreachable", min_reachable,
        f"быстрый нож физически НЕ может сработать раньше медленного: "
        f"минимально достижимо {min_reachable:g} мин >= медленного порога {slow_threshold:g} мин "
        f"(gated={gated}) — недостижимое условие (класс issue #1172)",
    )


def check_gate_then_duration_pair(
    source: str, container_func: str, resolve_call: str, fast_const: str, slow_const: str,
) -> RaceVerdict:
    """Собирает все три факта из ОДНОГО текста источника (файл целиком или
    вендоренная фикстура функции + констант) и считает вердикт. Один вызов —
    один зарегистрированный пары порогов (см. `test_reachability_guard.py`,
    там же — историческая проверка на реальных коммитах).

    `fast_const == slow_const` — вырожденный вызов (по ошибке передана ОДНА
    константа как обе стороны пары, находка прочёса #1184): арифметика ниже
    молча дала бы уверенный "unreachable" на `2*X >= X`, хотя это не находка
    про реальный код, а ошибка КОНФИГУРАЦИИ вызова. Отдельный статус
    `degenerate`, не `unreachable` — доказывай доказыватель (AGENTS.md)."""
    if fast_const == slow_const:
        return RaceVerdict(
            "degenerate", None,
            f"fast_const и slow_const совпадают ('{fast_const}') — вырожденный вызов "
            "(константа передана дважды по ошибке), не находка о реальном коде",
        )
    fast = extract_constant(source, fast_const)
    slow = extract_constant(source, slow_const)
    func_source = extract_function_source(source, container_func)
    gated = (
        is_gated_by_age_threshold(func_source, resolve_call, fast_const)
        if func_source is not None else None
    )
    return race_verdict(fast, slow, gated)


# ── Подпись 3: сигнатура не встречается ни в одном реальном логе ────────────

def signature_in_corpus(signature: str, corpus_text: str) -> bool:
    """Регистронезависимая проверка подстроки — тот же критерий, что
    `classify_failure_cause` в pulse_guard.py применяет при сравнении
    (`.lower()` на обеих сторонах)."""
    return signature.strip().lower() in corpus_text.lower()


@dataclass(frozen=True)
class SignatureVerdict:
    status: str  # "not_applicable" | "unconfirmed" | "confirmed"
    detail: str


def check_stop_signature_in_corpus(source: str, stop_pattern: str, corpus_text: str) -> SignatureVerdict:
    """`stop_pattern` — литерал, который классификатор ищет через
    `grep -qE '<pattern>'`/аналог перед тем, как остановить цепочку/эскалацию
    (`return 1`). Если этот ЛИТЕРАЛ не встречается нигде в `source` как
    активная стоп-ветка — `not_applicable` (проверять нечего, класс уже не
    воспроизводится этим текстом, как на текущем main). Если встречается как
    активная ветка, но не найден в `corpus_text` (реальные цитаты логов) —
    `unconfirmed` (ровно случай 3: недостижимый в проде стоп-класс). Если
    встречается и в source, и в корпусе — `confirmed`.

    Поиск `return 1` ОГРАНИЧЕН телом ЭТОГО `if ...; then ... fi`-блока (до
    следующей строки, состоящей только из `fi`), а не всем текстом после
    grep-строки: находка прочёса #1184 — старый неограниченный `(?:.*\\n)*?`
    дотягивался через закрывающую скобку функции ДО `return 1` СОВСЕМ
    ДРУГОЙ функции (`dsh_provider_quota_gate_skip`), где `return 1` значит
    противоположное. Проверено дословно: `STREAM_CLOSED:` на main (реальная
    сигнатура, восемь `return 0` в `dsh_chain_should_advance`, ни одного
    `return 1`) до этого фикса давал ложный `unconfirmed`."""
    block_pattern = re.compile(
        r"grep\s+-qE\s+'" + re.escape(stop_pattern) + r"'.*\n"
        r"((?:.*\n)*?)"
        r"[ \t]*fi[ \t]*\n",
    )
    match = block_pattern.search(source)
    if match is None or "return 1" not in match.group(1):
        return SignatureVerdict(
            "not_applicable",
            f"'{stop_pattern}' не встречается как активная стоп-ветка (return 1 в СВОЁМ "
            "if/fi-блоке) в этом тексте",
        )
    if signature_in_corpus(stop_pattern, corpus_text):
        return SignatureVerdict("confirmed", f"'{stop_pattern}' подтверждён реальной цитатой в корпусе")
    return SignatureVerdict(
        "unconfirmed",
        f"'{stop_pattern}' используется как СТОП-класс (return 1), но не встречается ни в одной "
        "реальной цитате корпуса — недостижимый стоп-класс (класс issue #1172, случай 3)",
    )
