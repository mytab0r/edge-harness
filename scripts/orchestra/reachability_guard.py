"""Механизм для issue #1172: находит условие срабатывания, которое физически
недостижимо, потому что раньше срабатывает конкурирующее условие, режущее ту
же величину, либо потому что распознаваемая строка не встречается ни в одном
реальном логе, либо потому что проверяемая величина всегда имеет одно
значение. Не пытается решить класс целиком (общий случай — по сути проблема
останова) — закрывает ТРИ узкие, реально наблюдавшиеся подписи:

  1. «Два ножа режут одну величину, гейт блокирует быстрый» (случай 1 issue
     #1172 — PR #1089/задача #1085): быстрое условие нуждается в наблюдении,
     которое само появляется не раньше, чем сработает МЕДЛЕННЫЙ порог (или
     тот же самый быстрый порог как гейт) — из-за этого быстрое условие
     физически не может сработать раньше медленного.
  2. «Величина вычислена и проверена в одном такте» (случай 2 issue #1172 —
     PR #1104/задача #1103): алерт-проверка вызывается на записи, созданной
     ЭТИМ ЖЕ тиком (`ts: now`), и при этом считает возраст записи от того же
     `now` — возрастная ветка `now - ts >= порог` не истинна никогда,
     «N мин назад» всегда 0.
  3. «Распознаваемая строка не встречается ни в одном реальном логе» (случай
     3 issue #1172 — PR #1114/задача #1084): именованный стоп-класс держится
     на литерале, который не наблюдался ни разу за пределами синтетической
     фикстуры смоука — приём взят из PR #1177 (INFRA_ERROR_SIGNATURES),
     формализован здесь как переиспользуемая функция.

Честная граница ВСЕХ трёх проб (общая для класса): проба распознаёт ту форму
кода, что реально наблюдалась в живых случаях, и НЕ подтверждена для других
форм. Что именно не распознаётся — названо в докстринге каждой пробы; это
граница узкости, а не заявление «баг невозможен».

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

_GATE_OPS = r"(?:>=|<=|==|>|<)"


def _comparison_with_const(const_name: str) -> re.Pattern[str]:
    """Любое сравнение (`>=`, `<=`, `==`, `>`, `<`) константы `const_name` с
    другой величиной, в любую сторону (`x OP КОНСТ` и `КОНСТ OP x`)."""
    n = re.escape(const_name)
    return re.compile(rf"\b\w+\s*{_GATE_OPS}\s*{n}\b|{n}\b\s*{_GATE_OPS}\s*\w+")


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
    """True — вызов `resolve_call(` в тексте функции стоит ПОСЛЕ сравнения
    константы `gate_const_name` с какой-то величиной (любой оператор из
    `>=`, `<=`, `==`, `>`, `<`, в любую сторону, любое имя второй величины:
    `age_minutes >= ИМЯ`, `ИМЯ <= age_minutes`, строгий `>`, `run_age > ИМЯ`
    — узнаваемые записи одного и того же гейта «сначала возраст, потом
    наблюдение», форма бага случая 1). False — вызов найден, но ни одного
    сравнения с константой перед ним нет (безусловный вызов, наблюдение
    доступно с первого пульса). `None` — сам `resolve_call` не встречается
    в тексте вовсе: «неприменимо», не «безопасно» (доказывай доказыватель,
    AGENTS.md).

    Честная граница пробы (не «ложного ОК нет», а вот где оно возможно):
    гейт, СПРЯТАННЫЙ за хелпером или промежуточной переменной (сравнение
    вынесено в отдельную функцию/предвычисленный флаг и в тексте перед
    вызовом не встречается), пробой НЕ распознаётся — такой код даст
    False («reachable»). Проба — текстовая эвристика по наблюдавшейся
    форме, а не dataflow-анализ; пару (файл, константы) для проверки
    выбирает оператор сознательно, см. `check_gate_then_duration_pair`."""
    call_index = function_source.find(f"{resolve_call}(")
    if call_index == -1:
        return None
    before_call = function_source[:call_index]
    return _comparison_with_const(gate_const_name).search(before_call) is not None


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
    `return 1`) до этого фикса давал ложный `unconfirmed`.

    Проверяются ВСЕ if/fi-блоки с этим литералом, не только первый (замечание
    ревью PR #1180): литерал может встретиться и в переключаемой ветке с
    `return 0`, и дальше как АКТИВНЫЙ стоп-класс с `return 1` — первый блок
    без `return 1` не даёт права молчать о втором."""
    block_pattern = re.compile(
        r"grep\s+-qE\s+'" + re.escape(stop_pattern) + r"'.*\n"
        r"((?:.*\n)*?)"
        r"[ \t]*fi[ \t]*\n",
    )
    active_block = next(
        (match for match in block_pattern.finditer(source) if "return 1" in match.group(1)),
        None,
    )
    if active_block is None:
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


# ── Подпись 2: запись этого же тика кормит проверку возраста ─────────────────

def extract_braced_source(source: str, decl_pattern: str) -> str | None:
    """Текст от строки, совпавшей с `decl_pattern` (regex, MULTILINE), до
    закрывающей скобки первого `{` после объявления (подсчёт баланса
    `{`/`}`). `None` — объявления с таким паттерном в тексте нет (другая
    форма кода — проверка неприменима). Честная граница: подсчёт слеп к
    скобкам внутри строковых литералов/комментариев — для вендоренных
    фикстур и зарегистрированных целей (фрагменты harness.ts) скобки
    сбалансированы, НЕ подтверждено для кода с одиночными `{`/`}` в строках."""
    match = re.search(decl_pattern, source, re.MULTILINE)
    if match is None:
        return None
    brace_start = source.find("{", match.end())
    if brace_start == -1:
        return None
    depth = 0
    for index in range(brace_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start():index + 1]
    return None  # несбалансированный текст — считаем «объявление не найдено»


@dataclass(frozen=True)
class SameTickVerdict:
    status: str  # "not_applicable" | "unreachable" | "reachable"
    detail: str


def check_same_tick_age_branch(source: str, consumer_call: str, text_func: str) -> SameTickVerdict:
    """Подпись 2 issue #1172 (случай 2 — PR #1104/задача #1103). Три текстовых
    факта наблюдавшейся формы бага:

      A. «запись этого же тика»: вызов `consumer_call(VAR, { ts: VAR, …` —
         второй аргумент строится литералом объекта, чьё поле `ts` связано с
         ТЕМ ЖЕ идентификатором `VAR`, что и первый аргумент (у пульса,
         проверяемого на возраст, `ts` равен `now` этого же тика — возраст
         заведомо 0);
      B. «потребитель кормит текстовую функцию тем же тактом»: в теле метода
         `consumer_call` есть вызов `text_func(VAR, …` — `now` уходит туда же,
         куда и только что созданная запись;
      C. «возраст считается от now и ts записи»: тело `text_func` содержит
         `now - <x>.ts` (арифметика возраста, «N мин назад»).

    A и B и C одновременно → `unreachable`: возрастная ветка текста алерта
    не истинна никогда («N мин назад» всегда 0) — механизм, который не может
    сработать (класс issue #1172). Любой факт не найден → `not_applicable`
    (проверка не выполнялась, не «прошла»). Форма не воспроизводится →
    `reachable`: фикс PR #1104 на текущем main даёт ровно это — вызов
    `pulseAlertText(lastPulse)` теряет факт B, сигнатура без `now` делает
    невоспроизводимым и C.

    Честная граница пробы: распознаётся только наблюдавшаяся запись фактов —
    `{ ts: VAR` с одним пробелом после `{`, возраст одной разностью
    `now - x.ts`. Другие записи (многострочный литерал, возраст через хелпер
    типа `pulseStale(now, x)` в ТЕКСТЕ функции, сравнение без вычитания)
    пробой НЕ распознаются — «reachable» на такой форме ложным не является
    только при условии, что форма реально отсутствует; пару (вызов,
    функция) для проверки выбирает оператор сознательно, НЕ подтверждено
    для форм вне трёх фактов выше."""
    same_tick = re.search(
        re.escape(consumer_call) + r"\(\s*(\w+)\s*,\s*\{\s*ts:\s*\1\b", source
    )
    if same_tick is None:
        return SameTickVerdict(
            "not_applicable",
            f"вызов `{consumer_call}(VAR, {{ ts: VAR, …` (запись этого же тика) не найден "
            "в этом тексте — проверка не выполнялась",
        )
    tick_var = same_tick.group(1)
    # `async` перед именем метода допускается ровно так же, как у text_func
    # ниже. Без этого пометка метода асинхронным ТИХО превращала проверку в
    # not_applicable — «не проверяли» вместо «прошло», то есть гвардия
    # переставала работать от правки, к её предмету отношения не имеющей
    # (поймано живым прогоном на #1495, где #tickPulseAlert стал async).
    consumer_body = extract_braced_source(
        source, rf"^\s*(?:async\s+)?{re.escape(consumer_call)}\("
    )
    if consumer_body is None:
        return SameTickVerdict(
            "not_applicable",
            f"объявление `{consumer_call}(` не найдено в этом тексте — проверка не выполнялась",
        )
    feeds_now = (
        re.search(re.escape(text_func) + r"\(\s*" + re.escape(tick_var) + r"\b", consumer_body)
        is not None
    )
    text_body = extract_braced_source(
        source, rf"^\s*(?:export\s+)?(?:async\s+)?function\s+{re.escape(text_func)}\("
    )
    if text_body is None:
        return SameTickVerdict(
            "not_applicable",
            f"объявление функции `{text_func}(` не найдено в этом тексте — проверка не выполнялась",
        )
    computes_age = re.search(r"\bnow\s*-\s*\w+\.ts\b", text_body) is not None
    if feeds_now and computes_age:
        return SameTickVerdict(
            "unreachable",
            f"`{text_func}` вызывается на записи этого же тика (`{{ ts: {tick_var}, …`) и считает "
            f"`{tick_var} - <x>.ts` — возраст всегда 0, возрастная ветка текста недостижима "
            "(класс issue #1172, случай 2)",
        )
    return SameTickVerdict(
        "reachable",
        f"форма недостижимой ветки не воспроизводится: "
        f"факт «тексту уходит now этого же тика» = {feeds_now}, "
        f"факт «возраст считается от now и ts» = {computes_age}",
    )
