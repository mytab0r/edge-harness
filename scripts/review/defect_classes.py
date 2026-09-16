#!/usr/bin/env python3
"""Классификация НОВЫХ классов дефектов — в источнике, тем же вызовом модели
(issue #1237), не постфактум-кластеризацией.

## Почему не постфактум (доказано числом, не мнением)

Аналитик (2026-09-14) посчитал word-overlap cosine на подлинных текстах трёх
инстансов класса #1172 (PR #1089/#1104/#1114, все — «механизм физически не
может сработать») и одной находки другого класса (#1185):

    A1(#1089)-A2(#1104)  cos=0.094   ОДИН класс
    A1(#1089)-A3(#1114)  cos=0.019   ОДИН класс
    A2(#1104)-A3(#1114)  cos=0.013   ОДИН класс   ← минимум одноклассовых
    A3(#1114)-B2(#1185)  cos=0.060   РАЗНЫЕ классы ← БОЛЬШЕ минимума одноклассовых

`min(одноклассовых)=0.0125 < max(разноклассовых)=0.060` — порога, отделяющего
«один класс» от «разных», не существует на словах находки: это оригинальная
проза, а класс по определению бьёт РАЗНЫЕ файлы и разные формулировки
(«недостижим» в #1089/#1104, но «не выстрелит никогда» в #1114).
Нормализацию обязана делать модель В МОМЕНТ находки — бесплатно, тем же
вызовом, который уже идёт (docs/decisions/0025-defect-class-source-
classification.md — полный разбор и отклонённый вариант).

## Три состояния поля КЛАСС (не два) — тот же принцип, что scripts/lib/
## check_result.py (issue #1096): «не назвала» ≠ «новый» ≠ «известный»

Молчаливое слияние «класс не назван» с «класс новый» подделало бы кандидатов
несуществующим сигналом (каждый unclassified PR выглядел бы как новый
кандидат); слияние «новый» с «известный» сломало бы саму идею
переиспользования — модель никогда не увидела бы, что её решение
зарегистрировано. `classify()` ниже возвращает `ClassSignal` с явным полем
`state` ИЗ ТРЁХ значений, не двух, по тому же обоснованию, что у
`check_result.CheckResult`.

## Обратная связь кандидатов — ядро замысла, без него список выродится в
## прозу

Известные классы (`KNOWN_CLASSES`, из `defect_classes.json`) подставляются в
промпт ($defect_classes_section, ai_prompt.md) ВМЕСТЕ с кандидатами —
slug'ами, замеченными 1+ раз и ещё не продвинутыми в отдельную задачу.
Без кандидатов второй PR того же класса не увидел бы, что первый уже назвал
slug, и придумал бы синоним — ровно то, что сделало #1172 находкой на
11-м инстансе, а не на третьем.

## Носитель — файл (известные) + комментарии-вердикты по всему репозиторию
## (кандидаты) — БЕЗ отдельного маркера, история #1255

Известные классы — `defect_classes.json`, один файл, читается БЕЗ сети
(cmd_gather уже делает много сетевых вызовов; статический файл — 0
дополнительных). «Реестр обновляется МАШИНОЙ при повышении кандидата, а не
человеком» (issue #1237) — промоушен (issue #1240, follow-up) редактирует
этот JSON автоматическим PR, не прямым коммитом из параллельных прогонов
ai-review: JSON, редактируемый параллельными CI-джобами напрямую (git
commit), гонял бы в гонку записи (ai-review идёт ~109 раз/сутки, докстринг
soft_failure_digest.py) — PR через обычный мерж-конвейер репозитория
сериализует эти правки тем же механизмом, что и любой другой код.

Кандидаты — ПЕРВАЯ РЕДАКЦИЯ (PR #1244) писала отдельный маркер-комментарий
на выделенную issue #1238 (`POST .../issues/1238/comments`, job `verdict`).
Это СЛОМАНО СТРУКТУРНО, не по недосмотру: `verdict` намеренно лишён
`issues: write` мандатом владельца 2026-09-11 (#939, эскалация в
WATCHDOG_ISSUE убрана из ai_review.py целиком — писать в Issues API этому
job'у стало незачем, право снято). Живая улика (issue #1255): прогон
34857962308, PR #1212 — модель класс назвала (`class: candidate=
маркер-позже-действия` в шапке ГЛАВНОГО комментария-вердикта, опубликован
успешно под `pull-requests: write`), а отдельный POST на #1238 упал `HTTP
403 Resource not accessible by integration`; issue #1238 осталась пустой
навсегда — писать в неё было физически нечем.

Фикс (issue #1255) убирает отдельную запись вовсе, а не чинит право: slug
УЖЕ лежит в шапке главного комментария-вердикта (`class: candidate=...`,
build_comment) — тот пишется job'ом `verdict` под `pull-requests: write`,
которое НЕ снималось и работает (иначе не было бы вердикта вообще). Читатель
(`recent_candidate_stats`) агрегирует кандидатов из УЖЕ опубликованных
комментариев — репозиторий-широкий эндпоинт `GET /repos/{repo}/issues/
comments` (не `.../issues/{N}/comments`: он один на весь репозиторий, а не
на одну issue), доверенный автор (`review_labels._is_trusted_verdict_author`,
тот же класс проверки, что #294 — публичный репозиторий, тело недоверенного
автора не парсится как факт) и поле `class` шапки (`review_labels.FACT_RE`,
общее место правды и для читателя, и для писателя — не вторая копия
регэкспа). Читает это job `review`/gather, права `issues: read` там были и
остаются — НИЧЕГО не добавлено. Запись и чтение теперь — ОДНА и та же
операция POST главного комментария: класс отказа «эмиссия работает, запись
молча ломается» закрыт по построению — второй операции, которая могла бы
разойтись с первой, больше нет.

Issue #1238 закрыта (#1255) со ссылкой на этот докстринг и issue #1255 —
осиротевший носитель, в который больше никто не пишет, не бросается молча
висеть (AGENTS.md, класс #1172 «механизм недостижим по построению»).

Не переиспользуем WATCHDOG_ISSUE (#120) — довод из первой редакции остаётся
в силе для ЛЮБОЙ отдельной issue-очереди: находка ревью PR #1089 (класс
#1172) — маркер, вытесненный из окна чтения штормом чужих комментариев
(>100/час на #120). Репозиторий-широкий поток (эта редакция) — измеримо
РЕЖЕ: живой замер issue #1255 (2026-09-14) — 636 комментариев/24ч по всему
репозиторию (~26/ч), из которых лишь ~15% несут `reviewer:`-факт (доверенный
вердикт), то есть ниже темпа, который сам #120 уже признал теснотой.

## Потолок размера и честная цена — токены прод-промпта не растут без
## границы, но окно истории УЖЕ не бесконечное

`MAX_CANDIDATES_SHOWN` ограничивает список кандидатов в промпте (не сам
носитель — история живёт в уже опубликованных PR-комментариях, промпт видит
только срез). `MAX_CANDIDATE_READ_PAGES` ограничивает СТОИМОСТЬ чтения (тот
же приём, что review_labels.list_pages/pulse_guard.issue_marker_times,
`max_pages` — стоимость тика не должна расти с историей): число ВЫЗОВОВ API
за тик — то же самое (≤5 постранично), что было бы у отдельной issue #1238 в
рабочем состоянии — `list_pages` ограничивает по числу СТРАНИЦ, не по доле
полезных записей на странице. Изменилась не цена в запросах, а окно
ИСТОРИИ: на живом темпе issue #1255 (636 комментариев/24ч) 5 страниц по 100
= 500 самых свежих комментариев ВСЕГО репозитория покрывают ~19 часов
(500/636×24ч), а не «всю историю маркеров», как хранила бы выделенная issue.
Кандидат, не повторившийся в течение ~19 часов, выпадает из среза раньше,
чем выпал бы из #1238 — цена спрямления носителя, названная явно, а не
предполагаемая тихо. Цена в токенах текущего словаря (1 известный класс, 0
кандидатов на момент PR #1244) — см. `render_prompt_section.__doc__` и число,
посчитанное в тестах (`test_defect_classes.py::
test_render_prompt_section_token_cost_is_small`).

## Сигнал инертности (issue #1240, требование координатора; issue #1255) —
## «повторов пока не было» ≠ «контракт не исполняется вовсе»

`recent_candidate_stats` возвращает `CandidateScan`, не голый список: рядом
со срезом кандидатов — `rework_verdicts_seen`/`rework_with_class_seen`,
считанные ТОЛЬКО по вердиктам `rework` (у `approve` нет находок, `class:` у
него законно отсутствует, см. build_comment). Ноль class-строк среди
НЕНУЛЕВОГО числа rework-вердиктов — это «контракт КЛАСС не исполняется» (то
же наблюдение, что уже делает cmd_verdict ПОШТУЧНО на каждом PR, issue
#1237 п.8 — здесь то же самое суммарно, по срезу), отличимое от «rework
вердиктов в окне не было вовсе» и от «кандидатов 0, потому что все
находки — известные классы». Ровно тот случай, который #1255 живьём и
воспроизвёл: issue #1238 была пуста, и ОДНОЙ пустоты было недостаточно,
чтобы понять, что именно сломалось (запись 403 vs честных «повторов пока
нет»).

## Не подтверждено

- Реальная модель (GLM/др. провайдер цепочки), получив эту подсказку,
  действительно выберет существующий slug вместо синонима — это КЛАСС
  ПОВЕДЕНИЯ модели, не механики; проверено этим PR только КОСВЕННО —
  вручную (автором PR, не живым вызовом модели: сборка промпта в CI не
  воспроизведена) на подлинных текстах трёх находок #1089/#1104/#1114 (см.
  `test_defect_classes.py::test_three_real_pr1172_findings_collapse_to_one_slug`)
  — присвоение slug там сделано человеком/агентом-разработчиком по контракту
  промпта, не самой моделью ai-review. Первый настоящий прогон в CI —
  единственное honest-подтверждение; PR явно называет это ограничение,
  не выдаёт симуляцию за факт.
- Достаточно ли окна ~19ч (при текущем темпе комментариев) для порога
  повторов, который follow-up #1240 применит при повышении кандидата в
  известный класс, — не проверено: зависит от того, как часто ai-review в
  среднем натыкается на один и тот же НОВЫЙ класс, а этого числа пока нет
  (первый честный прогон нового носителя ещё не случился на момент правки).

Запуск: python -m pytest scripts/review/test_defect_classes.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import re
from typing import NamedTuple

SCRIPT_DIR = Path(__file__).resolve().parent

# review_labels.list_pages — то же место правды, что уже использует ai_review.py
# (пагинация до короткой страницы, класс #308) — не заводим вторую копию.
_RL_SPEC = importlib.util.spec_from_file_location(
    "review_labels", SCRIPT_DIR.parent / "lib" / "review_labels.py")
review_labels = importlib.util.module_from_spec(_RL_SPEC)
_RL_SPEC.loader.exec_module(review_labels)  # type: ignore[union-attr]

REGISTRY_FILE = SCRIPT_DIR / "defect_classes.json"

# issue #1238 — БЫВШИЙ носитель-реестр кандидатов (PR #1244), закрыта issue
# #1255: писать было физически нечем (job `verdict` без `issues: write`,
# #939), носитель заменён на уже публикуемые комментарии-вердикты (см.
# докстринг модуля). Номер оставлен здесь ТОЛЬКО как исторический якорь для
# читателя докстрингов/комментариев — в коде НЕ используется (не второй
# источник правды, просто ссылка на закрытую issue).
_RETIRED_DEFECT_CLASS_TRACKER_ISSUE = 1238

# Строка контракта промпта (ai_prompt.md, п.1 «Блокирует мерж») — тот же
# стиль допуска markdown/точки, что VERDICT_RE/SCOPE_RE в ai_review.py, но
# здесь без markdown-обрамления: КЛАСС — не отдельная контрактная строка
# верхнего уровня (как ВЕРДИКТ/РАЗМЕР), а трейлер внутри найденной прозы,
# модель не имеет повода обрамлять её так же настойчиво.
CLASS_LINE_RE = re.compile(r"^КЛАСС:\s*(\S+?)\s*\.?\s*$")

# slug — 2-5 русских/латинских слов через дефис, без цифр/путей/пробелов
# внутри токена (CLASS_LINE_RE уже гарантирует \S — один токен без пробела).
SLUG_RE = re.compile(r"^[a-zа-яё]+(-[a-zа-яё]+){1,4}$", re.IGNORECASE)

MAX_CANDIDATES_SHOWN = 15
MAX_CANDIDATE_READ_PAGES = 5

STATE_NOT_NAMED = "not_named"
STATE_CANDIDATE = "candidate"
STATE_KNOWN = "known"


# ── Реестр известных классов — статический файл, читается без сети ──────────

def _load_known() -> tuple[dict, ...]:
    try:
        raw = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"defect_classes.json не прочитан/повреждён ({error}) — "
            "реестр известных классов недоступен, промпт не соберётся"
        ) from None
    entries = raw.get("known")
    if not isinstance(entries, list):
        raise RuntimeError("defect_classes.json: поле 'known' отсутствует или не список")
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("slug") or not entry.get("summary"):
            raise RuntimeError(f"defect_classes.json: запись без slug/summary: {entry!r}")
    return tuple(entries)


KNOWN_CLASSES: tuple[dict, ...] = _load_known()


def known_slugs() -> set[str]:
    return {entry["slug"] for entry in KNOWN_CLASSES}


# ── Разбор поля КЛАСС из ответа модели (чистая функция, без сети) ───────────

def parse_defect_classes(text: str) -> list[str]:
    """Все строки `КЛАСС: <slug>` в тексте, в порядке появления (не только
    валидные по SLUG_RE — валидация отдельно в classify(), чтобы вызывающий
    мог отличить «строки нет вовсе» от «строка есть, но не по формату»)."""
    return [
        match.group(1).strip()
        for line in (text or "").splitlines()
        for match in [CLASS_LINE_RE.match(line.strip())]
        if match
    ]


def is_valid_slug(slug: str) -> bool:
    return bool(SLUG_RE.match(slug))


class ClassSignal(NamedTuple):
    """Три состояния, не два — см. докстринг модуля. `known`/`candidates` —
    дедуплицированные кортежи валидных slug'ов в порядке появления;
    `invalid` — строки КЛАСС, найденные, но не по формату SLUG_RE (не
    считаются находкой класса, но видны отдельно для диагностики промпта)."""

    state: str
    known: tuple[str, ...]
    candidates: tuple[str, ...]
    invalid: tuple[str, ...]


def classify(answer: str, known: set[str] | None = None) -> ClassSignal:
    """`known` — снимок known_slugs() на момент вызова (параметр, не чтение
    модуля напрямую внутри функции) — тестируемость без монтирования
    временного defect_classes.json."""
    known = known if known is not None else known_slugs()
    raw = parse_defect_classes(answer)
    valid = [s for s in raw if is_valid_slug(s)]
    invalid = tuple(dict.fromkeys(s for s in raw if not is_valid_slug(s)))
    known_found = tuple(dict.fromkeys(s for s in valid if s in known))
    candidates_found = tuple(dict.fromkeys(s for s in valid if s not in known))
    if candidates_found:
        state = STATE_CANDIDATE
    elif known_found:
        state = STATE_KNOWN
    else:
        state = STATE_NOT_NAMED
    return ClassSignal(state, known_found, candidates_found, invalid)


# ── Промпт: известные + кандидаты, подставляются ОБРАТНО (ядро замысла) ─────

def render_prompt_section(candidates: list[dict]) -> str:
    """Текст для $defect_classes_section (ai_prompt.md). `candidates` — уже
    отсортированный, обрезанный по MAX_CANDIDATES_SHOWN список
    {"slug":..., "count":..., "prs": set(...)} (см. recent_candidate_stats).

    Цена в токенах (см. test_defect_classes.py, грубая оценка 4 символа/
    токен — тот же порядок, что использует cmd_gather для остального
    промпта): при 1 известном классе и 0 кандидатах (состояние репозитория
    на момент этого PR) секция — около 60 токенов; полный потолок
    (1 известный + MAX_CANDIDATES_SHOWN=15 кандидатов) — около 550 токенов,
    что на порядок меньше context_pack/rules_section (десятки КБ каждый) —
    не доминирующая статья бюджета промпта даже на потолке."""
    lines = [
        "Классы дефектов этого репозитория — переиспользуй slug ДОСЛОВНО, "
        "если находка по сути один из них (другая формулировка не делает её "
        "новым классом); ни один не подходит — придумай новый короткий slug "
        "(2-4 русских слова через дефис, без цифр и путей).",
        "",
        "Утверждённые:",
    ]
    if KNOWN_CLASSES:
        for entry in KNOWN_CLASSES:
            issue_note = f" (issue #{entry['issue']})" if entry.get("issue") else ""
            lines.append(f"- {entry['slug']} — {entry['summary']}{issue_note}")
    else:
        lines.append("(пока ни одного)")
    lines.append("")
    if candidates:
        lines.append(
            "Кандидаты (замечены недавно, ещё не заведены отдельной задачей — "
            "совпадение тоже считается переиспользованием, не новым классом):")
        for item in candidates:
            lines.append(f"- {item['slug']} (замечено {item['count']}x)")
    else:
        lines.append("Кандидатов сейчас нет.")
    return "\n".join(lines)


def render_prompt_section_unavailable(reason: str) -> str:
    """Кандидаты не прочитаны (сеть/права) — секция БЕЗ них, но с явной
    причиной (AGENTS.md, «алерт не гадает»): молчаливое «кандидатов нет»
    неотличимо было бы от настоящего «пока никто не назвал новый класс»."""
    lines = [
        "Классы дефектов этого репозитория — переиспользуй slug ДОСЛОВНО, "
        "если находка по сути один из них; ни один не подходит — придумай "
        "новый короткий slug (2-4 русских слова через дефис).",
        "",
        "Утверждённые:",
    ]
    for entry in KNOWN_CLASSES:
        issue_note = f" (issue #{entry['issue']})" if entry.get("issue") else ""
        lines.append(f"- {entry['slug']} — {entry['summary']}{issue_note}")
    lines.append("")
    lines.append(f"Кандидаты не прочитаны в этом прогоне ({reason}) — список неполный.")
    return "\n".join(lines)


# ── Кандидаты: чтение среза из уже опубликованных комментариев-вердиктов ────
# (issue #1255) — НЕТ отдельной записи: slug уже лежит в шапке главного
# комментария (`class:`, build_comment), который job `verdict` и так публикует
# под `pull-requests: write`. Второй операции записи (которая могла бы
# разойтись с первой) здесь больше не существует — см. докстринг модуля.

class CandidateScan(NamedTuple):
    """Срез кандидатов + диагностика инертности (issue #1240, issue #1255) —
    см. докстринг модуля «Сигнал инертности». `rework_verdicts_seen`/
    `rework_with_class_seen` считают ТОЛЬКО доверенные вердикты `rework`
    (approve законно не несёт `class:` — у него нет находок); ноль
    class-строк среди ненулевого `rework_verdicts_seen` — контракт КЛАСС не
    исполняется, а не «повторов пока не было»."""

    candidates: list[dict]
    rework_verdicts_seen: int
    rework_with_class_seen: int


def recent_candidate_stats(
    repo: str, gh_func, max_pages: int = MAX_CANDIDATE_READ_PAGES,
    known: set[str] | None = None,
) -> CandidateScan:
    """Срез кандидатов из последних `max_pages` страниц комментариев ВСЕГО
    репозитория (`GET /repos/{repo}/issues/comments`, не одной issue — issue
    #1238 закрыта, см. докстринг модуля «Носитель»). Только доверенные
    вердикты (`review_labels._is_trusted_verdict_author` — тот же класс
    проверки, что #294: публичный репозиторий, чужое тело не парсится как
    факт) с полем `class:` в шапке (`review_labels.header_facts`/`FACT_RE` —
    одно место правды с писателем, build_comment). Уже ИЗВЕСТНЫЕ (см.
    known_slugs()) slug'ы исключены: однажды продвинутый класс не должен
    маячить кандидатом (follow-up #1240 обязан читать срез честно как «уже
    известен НА МОМЕНТ чтения», старые комментарии задним числом не
    редактируются). Сортировка — по числу РАЗНЫХ PR по убыванию (не по
    общему числу упоминаний: три упоминания одного PR — один случай, тот же
    критерий, что follow-up #1240 обязан применить при повышении), обрезка
    по MAX_CANDIDATES_SHOWN.

    `known` — снимок known_slugs() (параметр, не чтение модуля напрямую
    внутри функции — тот же приём, что classify()): тестируемость
    исторического среза ДО повышения кандидата без монтирования временного
    defect_classes.json (см. test_defect_classes.py, «Живые тексты #1172» —
    #1172/«недостижимый-механизм» уже сидит в реестре ЭТОГО PR как
    известный класс; симуляция состояния «ещё кандидат» требует явного
    known=set())."""
    comments = review_labels.list_pages(
        f"repos/{repo}/issues/comments?per_page=100&sort=created&direction=desc",
        gh_func, max_pages=max_pages)
    known = known if known is not None else known_slugs()
    by_slug: dict[str, dict] = {}
    rework_seen = 0
    rework_with_class = 0
    for comment in comments:
        if not review_labels._is_trusted_verdict_author(comment):
            continue
        facts = review_labels.header_facts(comment.get("body") or "")
        if facts.get("reviewer") != "rework":
            continue
        rework_seen += 1
        class_fact = facts.get("class")
        if not class_fact:
            continue
        rework_with_class += 1
        if not class_fact.startswith(f"{STATE_CANDIDATE}="):
            continue
        pr_field = facts.get("pr")
        try:
            pr = int(pr_field)
        except (TypeError, ValueError):
            continue
        for slug in class_fact[len(STATE_CANDIDATE) + 1:].split(","):
            slug = slug.strip()
            if not slug or not is_valid_slug(slug) or slug in known:
                continue
            entry = by_slug.setdefault(slug, {"slug": slug, "count": 0, "prs": set()})
            entry["prs"].add(pr)
            entry["count"] = len(entry["prs"])
    ranked = sorted(by_slug.values(), key=lambda e: -e["count"])
    return CandidateScan(ranked[:MAX_CANDIDATES_SHOWN], rework_seen, rework_with_class)
