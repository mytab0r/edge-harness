#!/usr/bin/env python3
"""Метки-вердикты конвейера ревью — одно место правды для всех потребителей.

Два гейта, две независимые метки (задача #18):

  Гейт 1 — детерминированное ревью (scripts/review/check_pr.py, workflow
  pr-review): review:ok / review:changes-requested, размерный гейт
  review:large с осознанным обходом review:large-ok.

  Гейт 2 — AI-ревью диффа (scripts/review/ai_review.py, workflow ai-review):
  ai:ok / ai:changes-requested / ai:failed.

Слияние (scheduler.merge_queue) требует review:ok И ai:ok. Вердикт AI
привязан к head, который ревьюили: детерминированное ревью при каждом своём
запуске (новый пуш) снимает все ai:*-метки — протухший вердикт не может
открыть слияние (см. ai_verdicts_to_drop) — ЕСЛИ дифф PR относительно base
действительно изменился. Подтягивание main без конфликтов меняет только
head (новый merge-коммит), но не патчи PR: check_pr.py сверяет отпечаток
диффа (diff_fingerprint) с тем, что сохранён в шапке последнего
AI-ревью-комментария (latest_ai_comment), и сохраняет ai:*-метку, если
отпечаток не изменился (#252, диагноз «вердикт AI не должен сбрасываться,
когда подтягивание не изменило дифф») — см. diff_unchanged. Носитель
отпечатка (#740, с 2026-09-08) — `patch` каждого файла (трёхточечный дифф
относительно ЕГО merge-base), не SHA блоба на голове: голова меняется даже
тогда, когда main правит файл, который PR не трогал по существу — см.
diff_fingerprint/_file_content_key ниже.

Импорт из соседних каталогов — importlib по файлу (паттерн claim_task):
скрипты запускаются как файлы, не как пакет.

Commit Status API (#345, кандидат из docs/research/23-platform-native-vs-custom.md
п.2): оба вердикта публикуются ВТОРЫМ каналом, POST /repos/{repo}/statuses/{sha},
параллельно меткам — переходный период, метки не убираются. Второго источника
истины не заводится: STATUS_* ниже вычисляются из ТОЙ ЖЕ переменной вердикта,
которую вызывающий код (check_pr.py/ai_review.py) уже использует для метки, не
отдельным запросом к GitHub. Цель — нативный `allow_auto_merge` (уже включён на
репозитории): метки он не видит, required status checks — видит.
"""

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone

# ── Гейт 1: детерминированное ревью ──────────────────────────────────────────
REVIEW_OK = "review:ok"
REVIEW_CHANGES = "review:changes-requested"
REVIEW_LARGE = "review:large"
LARGE_OK = "review:large-ok"

# ── Гейт 2: AI-ревью ─────────────────────────────────────────────────────────
AI_OK = "ai:ok"
AI_CHANGES = "ai:changes-requested"
AI_FAILED = "ai:failed"
AI_VERDICTS = (AI_OK, AI_CHANGES, AI_FAILED)

# ── Признак изменяющего вызова `gh api` ──────────────────────────────────────
# Одно место правды (находка ревью PR #950, третий проход): claim_task.gh и
# pulse_guard.gh несли ДВЕ побайтово идентичные копии этого множества и
# предиката (`claim_task._is_write`/`pulse_guard._gh_call_is_write`) — второй
# метод, добавленный в одну копию, молча не попал бы во вторую. `-X GET`
# (используется в паре мест репозитория, например search/issues) остаётся
# чтением — не в GH_WRITE_METHODS.
GH_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def is_write_call(args: tuple[str, ...]) -> bool:
    """`-X МЕТОД`, где МЕТОД — не GET, где-либо в аргументах `gh api ...`
    (порядок гейта — `-X МЕТОД путь ...`, но ищем по всей команде, не только
    по первым двум аргументам — устойчивее к перестановке)."""
    for i, arg in enumerate(args):
        if arg == "-X" and i + 1 < len(args):
            return args[i + 1].upper() in GH_WRITE_METHODS
    return False

# ── Вердикт-метки гейта 1 одним кортежем (#203) ──────────────────────────────
# Любой момент времени на PR должен существовать не более чем один из этих
# трёх; своп решает verdict_label_changes ниже.
REVIEW_VERDICTS = (REVIEW_OK, REVIEW_CHANGES, REVIEW_LARGE)

# ── Классификация причины verdict=error (#431) ───────────────────────────────
# Четыре состояния transport_failed/error_reason различают уже с #419 —
# reason_tag ниже даёт им короткие теги, чтобы их читал не только человек
# (findings — проза), но и scheduler.trigger_ai_review (факт `reason:` в
# шапке комментария, см. FACT_RE/header_facts): «квота исчерпана на неделю»
# и «модель ответила криво» — разные вещи и заслуживают разного числа
# автоповторов, а парсить прозу findings для этого решения нельзя — находка
# #431 на PR #329: findings там оказался ПРОЗОЙ МОДЕЛИ ("Установка прошла
# неполно..."), а не текстом error_reason, потому что reason подставляется в
# findings, только если findings пуст (см. ai_review.cmd_verdict) — прод-
# форма подтверждает: решение обязано читать структурный факт, не пересказ.
FAILURE_REASON_QUOTA_EXHAUSTED = "quota_exhausted"
FAILURE_REASON_RATE_LIMIT_BUDGET = "rate_limit_retry_budget_exceeded"
FAILURE_REASON_TRANSPORT = "transport_error"
FAILURE_REASON_CONTRACT = "contract_violation"

# ── Конфликт (mark_conflicts, scheduler.py) ──────────────────────────────────
# Единственное определение (было задублировано локальной константой в
# scheduler.py) — should_update_branch ниже читает её же.
CONFLICT_LABEL = "conflict"

# ── Провал контракта PR↔задача (contract_check.py) ───────────────────────────
# Единственное определение (было задублировано литералом "contract:failed"
# четырежды: дважды в contract_check.py, дважды в scheduler.py — REWORK_LABELS
# и test_scheduler.py читают её же).
CONTRACT_FAILED_LABEL = "contract:failed"

# ── Гвардия молчаливого отката main (#217) ───────────────────────────────────
# Газ гвардии check_pr.revert_guard: PR, чей дифф удаляет запись прод-манифеста
# или файл патч-серии, красит детерминированное ревью; эта метка, поставленная
# исполнителем ОСОЗНАННО вместе с объяснением в теле PR, объявляет удаление
# входящим в замысел. Значение здесь, рядом с остальными метками, которые
# читает конвейер; кто ставит/что блокирует/порог — docs/agents/LABELS.md.
REVERT_OK = "revert-ok"

# ── Commit Status API — вердикты вторым каналом, параллельно меткам (#345) ───
# Контексты кандидата в required_status_checks (branch protection ставит
# владелец вручную после подтверждения живым прогоном — не эта задача).
STATUS_REVIEW = "harness/review"
STATUS_AI_REVIEW = "harness/ai-review"

# Состояния mergeable_state, при которых GitHub ЯВНО подтвердил «не dirty» —
# только по ним mark_conflicts вправе снять CONFLICT_LABEL (#270). None/
# "unknown" сюда не входят: «вычисление не завершилось» — не то же самое,
# что «конфликта нет» (снятие по ним спрятало бы реальный конфликт).
CONFLICT_CLEAR_STATES = ("clean", "unstable", "has_hooks", "behind", "blocked")


def _names(labels) -> set[str]:
    """Имена меток из любой прод-формы: список dict'ов API или множество имён."""
    if isinstance(labels, str):
        return {labels}
    try:
        return {label["name"] for label in labels}
    except TypeError:
        return set(labels)


# Обе метки, любой из которых означает «гейт 1 вынёс вердикт» (#432) — общий
# кортеж для gate1_decided ниже и для потребителей, которым нужен сам набор
# имён меток (например, фильтр события `labeled` в таймлайне PR), не только
# булев ответ.
GATE1_LABELS = (REVIEW_OK, REVIEW_LARGE)


def gate1_decided(labels) -> bool:
    """True — гейт 1 (детерминированное ревью) уже вынес ЛЮБОЙ вердикт:
    `review:ok` ИЛИ `review:large` (метки взаимоисключающие — verdict_for
    ставит ровно одну, не обе сразу).

    Не путать с merge_label_gate ниже: тот отвечает на другой вопрос —
    «гейт 1 разрешил СЛИЯНИЕ» (там `review:large` не считается, оно и
    блокирует слияние до `review:large-ok`). Этот предикат — про «гейт 1
    уже закончил работу и решение можно продолжать конвейером» (двигать
    таймер ожидания вердикта AI, запускать автоповтор, считать инвариант
    состояния): для крупного PR это решение — `review:large`, и оно ничем
    не хуже `review:ok` с точки зрения «есть на что реагировать дальше».

    Смешение этих двух вопросов в одной проверке `REVIEW_OK in labels`
    once уже сделало AI-ревью невидимым для крупных PR на полгода (#396,
    PR #393 — `ai-review.yml::facts` открывался только по `review:ok`),
    а второй раз то же смешение сделало недостижимым для review:large PR
    и автоповтор ai-review, и наблюдательный инвариант
    `repo_invariants.check_stuck_review_gate` (#432, живой случай — PR #412:
    `review:large` + `ai:failed`, автоповторов ноль спустя полтора часа при
    пороге 30 минут). Единственное место правды — здесь, оба потребителя
    читают этот предикат, не переопределяют его локально."""
    names = _names(labels)
    return bool(names & set(GATE1_LABELS))


def merge_label_gate(labels) -> str | None:
    """Причина, по которой метки запрещают слияние; None — метки слияние открыли.

    Единственное место, где гейт слияния формулируется словами: scheduler
    печатает причину в отчёт, тесты доказывают обе ветки. Порог «оба гейта
    зелёные» — здесь, а не в вызывающем коде.

    Гейт 1 засчитан ЛИБО `review:ok`, ЛИБО связкой `review:large` +
    `review:large-ok` (находка #702, живой случай PR #333) — тот же
    порог, что `check_pr.py::size_gate` уже применяет НА МОМЕНТ простановки
    метки (`is_large = size_overflow and LARGE_OK not in labels`, значит при
    наличии `review:large-ok` очередной ЗАПУСК check_pr.py поставил бы
    `review:ok`, не `review:large`). Разрыв был здесь: `apply_large_ok`
    (`ai_review.py`) ставит `review:large-ok` МЕТКОЙ, без нового пуша — а
    `pr-review.yml` (где живёт check_pr.py) реагирует только на
    `opened/synchronize/reopened`, не на простановку своей же метки. Без
    этой строки PR с одобренным размером и AI навсегда стоял бы с
    `review:large`+`review:large-ok`+`ai:ok`, требуя ЕЩЁ одного, ничем не
    мотивированного пуша, чтобы гейт заметил уже принятое решение — тормоз
    без газа (AGENTS.md), хотя `test_gate1_decided_true_does_not_imply_merge_label_gate_open`
    и докстринг `gate1_decided` уже называли `review:large-ok` условием
    открытия («слияние ждёт review:large-ok» — ждёт, не игнорирует
    навсегда)."""
    names = _names(labels)
    gate1_ok = REVIEW_OK in names or (REVIEW_LARGE in names and LARGE_OK in names)
    if not gate1_ok:
        return (
            f"нет вердикта {REVIEW_OK} (ждёт детерминированное ревью, "
            f"{LARGE_OK} для крупного диффа, или доработку)"
        )
    if AI_OK not in names:
        return f"нет вердикта {AI_OK} (ждёт AI-ревью, доработку или повтор после сбоя)"
    return None


def should_update_branch(labels) -> bool:
    """Газ выборочного подтягивания веток (#252): true — обновлять ветку из
    main стоит, false — нет.

    Раньше scheduler.update_remaining_pulls дёргал gh pr update-branch для
    ВСЕХ открытых недрафт PR после каждого слияния (до 96 запусков оркестратора
    в сутки, cron */15) — тот же вызов и для PR, отставшего в merge_queue.
    Каждый update-branch — это push в чужую ветку → GitHub шлёт
    pull_request:synchronize → pr-review.yml перезапускается → снимает все
    ai:*-метки (ai_verdicts_to_drop выше) → при review:ok стартует дорогое
    ai-review.yml. PR, которому рано сливаться (нет вердиктов, в доработке,
    ai:changes-requested), от этого не выигрывает ничего — только теряет
    валидный вердикт и жжёт AI-квоту вхолостую.

    Число прогонов (расхождение с прозой issue #252 разобрано и закрыто, не
    догадкой): тело issue #252 называет «сто прогонов за четырнадцать часов»
    для окна 2026-09-02T20:00 → 2026-09-03T10:30 (14.5 ч) — это округление
    диагностики. Точный запрос за ТО ЖЕ окно —
    `gh api "repos/mytab0r/edge-harness/actions/workflows/ai-review.yml/runs
    ?created=2026-09-02T20:00:00Z..2026-09-03T10:30:00Z" --jq .total_count`
    (проверено повторно 2026-09-03) — отдаёт 142, не 100: «сто» в прозе issue
    было прикидкой на момент диагностики, точный подсчёт по её же окну даёт
    142. Дальше в тексте используется точное число 142 как подтверждённое
    запросом, не как второе, конкурирующее с issue значение.

    Обновлять стоит только два случая:
      1. оба вердикта уже зелёные (merge_label_gate(labels) is None) —
         PR реально близок к слиянию, следующий обход merge_queue его сольёт,
         и свежий head ему нужен;
      2. PR уже помечен CONFLICT_LABEL — подтягивание из main может расшить
         конфликт (только оно и способно).
    Во всех остальных случаях (нет вердиктов, review:changes-requested,
    ai:changes-requested, ai:failed без обоих ok) — подтягивание пропускается.

    ГАЗ (обязателен, автоматический, см. AGENTS.md «тормоз без газа не
    принимается»): предикат не хранит собственного состояния — он на лету
    читает текущие labels PR. Как только детерминированное и AI-ревью
    проставят оба вердикта (или mark_conflicts повесит CONFLICT_LABEL),
    САМЫЙ СЛЕДУЮЩИЙ прогон оркестратора (update_remaining_pulls после
    следующего слияния или behind-ветка merge_queue) увидит новые labels и
    снова начнёт подтягивать этот PR — без ручного вмешательства. Тормоз и
    газ — одно и то же чтение labels, разнесённое по времени.
    """
    names = _names(labels)
    if CONFLICT_LABEL in names:
        return True
    return merge_label_gate(names) is None


def ai_verdicts_to_drop(labels) -> list[str]:
    """ai:*-метки, которые детерминированное ревью снимает перед своим новым
    вердиктом. Вердикт AI действителен только для диффа, на котором сделан:
    вызывающая сторона (`check_pr.py`) снимает их, ТОЛЬКО ЕСЛИ дифф PR
    относительно base действительно изменился (`diff_unchanged` ниже) — не
    любой новый пуш, поскольку чистое подтягивание main меняет head, но не
    патчи PR (#252/#294). Эта функция называет метки к снятию; решение
    «снимать ли вообще» — за вызывающей стороной, читающей diff_unchanged.
    """
    names = _names(labels)
    return [label for label in AI_VERDICTS if label in names]


# ── Вердикт AI переживает подтягивание main без изменения диффа (#252) ──────
#
# Корень, который направления «выборочное подтягивание» (should_update_branch
# выше) и «одна задача — один PR» (mark_conflicts) лечили только как
# следствие: сам сброс ai:*-метки на каждом пуше не различает «дифф PR
# изменился» и «в ветку влили main, а дифф относительно base — тот же набор
# патчей». Проверено на реальном PR #292 (2026-09-04): у него один
# собственный коммит и один `Merge branch 'main'`; `git diff --stat` между
# merge-base и головой ДО и ПОСЛЕ слияния даёт побайтово идентичный список
# файлов — GitHub App API `pulls/{n}/files` вычисляет дифф той же логикой
# (base...head по merge-base), поэтому отпечаток по нему устойчив к чистому
# подтягиванию main и меняется только при реальной правке файлов PR.

def list_pr_files(repo: str, pr: int, gh_func) -> list[dict]:
    """Все файлы PR постранично, не только первая страница `per_page=100`.

    Класс (#294, вердикт ai-review PR #294): `gh api pulls/{n}/files` режет
    ответ на страницы по 100; и `check_pr.py`, и `ai_review.py` раньше читали
    только первую (`?per_page=100` без `page=`), поэтому у PR за сотню файлов
    правка файла ЗА первой сотней не меняла `diff_fingerprint` — `ai:ok`
    переживал настоящую правку автора молча (гейт открыт по протухшему
    вердикту), а сумма `additions` занижалась в обоих гейтах. Обход страниц —
    одно место правды в list_pages ниже (#308: та же форма для любого
    списочного эндпоинта, четвёртая копия того же цикла здесь была бы
    рецидивом того же класса, что и сам #308) — эта функция лишь несёт URL.
    """
    return list_pages(f"repos/{repo}/pulls/{pr}/files?per_page=100", gh_func)


def list_pages(url: str, gh_func) -> list[dict]:
    """Обход постранично любого списочного эндпоинта GitHub API до короткой
    страницы — та же форма, что list_pr_files/list_timeline выше, обобщённая
    на URL целиком (класс #308: место общее для любого списка, а не только
    files/timeline). `url` уже несёт свои query-параметры, включая
    `per_page=100`; листание добавляет `&page=N`.

    Найдено на живом репозитории (2026-09-05): `open_task_issues` и
    `open_pulls` в scheduler.py читали сырую первую страницу
    `...?state=open&...&per_page=100` без обхода — при 106 открытых задачах
    с меткой `task` (107 сырых записей issues на этой выборке; одна из них,
    #248, сама PR под меткой task и отфильтровывается по ключу
    pull_request — см. fixtures_open_task_issues_310.json) и растущем числе
    открытых PR воркер и планировщик молча не видели хвост за первой сотней:
    не ошибка, не предупреждение, задачи просто не существовали для пула.
    `reap_stale` читал таймлайн той же сырой формой
    (`.../timeline?per_page=100`) — тот же класс, что уже чинили в
    `last_review_ok_labeled_at`/`last_ready_labeled_at` (#303), сюда не
    мигрировали; там теперь используется list_timeline ниже.

    Fail loud (находка ревью PR #311): стоп-условие ниже — `len(chunk) < 100`,
    жёстко зашитое число, а не размер страницы из URL. Вызов с чужим
    `per_page` (например 50 на списке из 120 записей) молча вернул бы только
    первую страницу — тот же класс silent-wrong, который эта функция и
    закрывает для остальных вызовов. Проверка ниже делает такой вызов
    невозможным вместо того, чтобы полагаться на дисциплину вызывающих.

    Fail loud на неожиданной ФОРМЕ ответа (issue watchdog #120, дефект A):
    до этой правки `if not isinstance(chunk, list) or not chunk: break`
    трактовал ЛЮБОЙ не-список (dict с телом ошибки от вторичного
    рейт-лимита/абьюз-детектора GitHub, None от пустого тела) ТАК ЖЕ, как
    честную короткую последнюю страницу — обрыв обхода, возврат уже
    накопленного (возможно пустого) списка МОЛЧА. Живой случай: `open_pulls`
    (scheduler.py, идёт через эту функцию) отдавал `[]` при 27 реально
    открытых PR, ждущих доработки, — `wip_gate` считал это как «доработки
    нет» и открывал диспатч новых задач на ложном нуле (watchdog-issue #120,
    2026-09-11: `⏸️ ... 25 ≥ лимита 12` в 10:02:44Z, `✅ ... 0 < 12` в
    10:25:27Z — реальный живой пересчёт тем же критерием в тот же день дал
    27, не 0). Пустой снимок физически неотличим от «доработки нет» без
    этой проверки — то самое состояние, которое AGENTS.md требует красить,
    а не пропускать.

    Различие теперь: `chunk == []` (валидный список, просто пустой — короткая
    страница длиной 0, то же самое стоп-условие `len(chunk) < 100`, что и для
    непустой короткой страницы) останавливает обход как раньше. Любая
    НЕ-list форма (`dict`, `None`, `str`) — не «страниц больше нет», а «ответ
    непонятен» — RuntimeError наружу, вызывающий обязан упасть тем же путём,
    что и на сетевой ошибке `gh()`, а не тихо продолжить с частичным (или
    нулевым) списком."""
    if "per_page=100" not in url:
        raise ValueError(
            f"list_pages требует per_page=100 в URL (стоп-условие "
            f"len(chunk) < 100 иначе молча теряет хвост): {url!r}")
    page = 1
    items: list[dict] = []
    while True:
        chunk = gh_func(f"{url}&page={page}")
        if chunk == []:
            break  # честная короткая страница — items уже несёт всё найденное
        if not isinstance(chunk, list):
            raise RuntimeError(
                f"list_pages: неожиданный ответ на странице {page} для "
                f"{url!r} — ожидался list, получено {type(chunk).__name__} "
                f"({chunk!r:.200}); пустой/некорректный снимок не равен "
                "«элементов больше нет» (AGENTS.md fail loud, дефект #120A)")
        items.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return items


def list_timeline(repo: str, number: int, gh_func) -> list[dict]:
    """Весь таймлайн issue/PR постранично, не только первая страница
    `per_page=100` (#303, тот же класс пагинации, что list_pr_files выше и
    вердикт ai-review PR #294): `last_review_ok_labeled_at` и
    `last_ready_labeled_at` в scheduler.py читали сырой первый ответ
    `timeline?per_page=100` без обхода — на PR с длинным таймлайном (много
    комментариев/пушей/перелейбловок) событие `labeled` за первой сотней
    молча не находилось, `ready_since`/anchor обнулялись именно на самых
    долгоживущих PR — тех, ради которых порог и написан. Обход страниц —
    одно место правды в list_pages ниже (та же причина, что у list_pr_files
    выше): эта функция лишь несёт URL."""
    return list_pages(f"repos/{repo}/issues/{number}/timeline?per_page=100", gh_func)


def _file_content_key(f: dict) -> str:
    """Ключ содержимого одного файла для diff_fingerprint (#740).

    Раньше — `sha` блоба GitHub НА ГОЛОВЕ PR. Найдено фактом (issue #740,
    PR #333, 2026-09-08): `sha` — это SHA получившихся БАЙТОВ файла, а не
    признак «что изменил автор». Как только main меняет тот же файл, что и
    PR (пересечение файлов непусто — не «файлы PR не тронуты», условие,
    которое докстринг раньше молча предполагал), merge-коммит переписывает
    байты на голове → `sha` меняется → отпечаток объявляет изменение там,
    где патч автора не сдвинулся ни на байт. Улика: `.github/workflows/
    repo-ci.yml` в PR #333 — `sha` разошёлся (main правил файл в PR #730),
    `patch` (три-точечный дифф PR-ветки относительно ЕЁ merge-base) остался
    побайтово идентичным (fixtures_pr333_pull_no_overlap.json).

    Носитель — `patch` того же прод-ответа `pulls/{n}/files` (уже
    трёхточечный: relative to merge-base, тот же принцип, который API уже
    применяет для самого списка файлов — см. header-комментарий модуля).
    `patch` инвариантен к тому, что база поменяла НЕ пересекающиеся строки
    (три-точечный дифф отбрасывает чужой вклад), но меняется, когда база
    правит те же строки/контекст, что и автор (сдвигает номера строк или
    содержимое хайка) — то самое «содержательное пересечение», которое
    обязано провоцировать новое ревью (см. fixtures_pull_overlap_index.json:
    два файла из тринадцати меняют `patch`, остальные одиннадцать — нет).

    Фолбэк на `sha`: `patch` отсутствует в ответе GitHub для бинарных файлов
    и файлов, обрезанных по размеру (документированное поведение API) — для
    них у нас нет содержимого для хеширования вовсе, а `sha` меняется чаще,
    чем реальный дифф (в т.ч. на чистом подтягивании) — то есть ошибается в
    сторону «изменился», как требует AGENTS.md «при сомнении считаем
    изменившимся», а не в сторону «пропустить непроверенный бинарник».
    Префикс different для двух веток (patch:/sha:) — чтобы полный хеш файла
    с patch=None и другой с sha, случайно совпавшим текстом, не схлопнулись
    в одинаковый ключ.
    """
    patch = f.get("patch")
    if patch:
        return "patch:" + hashlib.sha256(patch.encode("utf-8")).hexdigest()
    return "sha:" + f.get("sha", "")


def diff_fingerprint(files) -> str:
    """Отпечаток содержимого диффа PR — sha256 по отсортированному списку
    `имя_файла:статус:ключ_содержимого` из прод-формы `gh api
    .../pulls/{n}/files` (см. _file_content_key выше — одно место правды на
    признак «содержимое файла не изменилось», оба гейта читают её же).

    Почему не число строк/изменений: длина диффа — не признак содержимого,
    две разные правки могут случайно дать одинаковое число добавленных/
    удалённых строк (класс, который явно назвала задача #252). Сортировка
    по строке снимает зависимость от порядка страниц API; статус в строке
    отличает rename/added/removed друг от друга даже при совпадении
    итогового имени файла.
    """
    parts = sorted(
        f"{f.get('filename', '')}:{f.get('status', '')}:{_file_content_key(f)}"
        for f in files
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def diff_unchanged(stored_fingerprint: str | None, current_fingerprint: str) -> bool:
    """True — дифф PR не изменился с момента последнего вердикта AI (совпали
    отпечатки). Нет сохранённого отпечатка (None/пусто — старый комментарий
    без поля `diff:`, сеть не отдала комментарий) трактуется как «изменился»:
    ложный сброс стоит лишнего круга ревью, ложное сохранение пропускает
    непроверенный код в main — при сомнении выбираем сброс (AGENTS.md)."""
    return bool(stored_fingerprint) and stored_fingerprint == current_fingerprint


# ── Дорогой прогон второго гейта переживает подтягивание main (#294) ────────
#
# Диагноз вердикта AI-ревью на PR #294: `check_pr.py` сохраняет `ai:*`-метку
# при неизменном диффе (diff_unchanged выше), НО сам workflow `ai-review.yml`
# всё равно триггерится — он слушает не метку, а `workflow_run` от
# `pr-review` (события от GITHUB_TOKEN не создают триггеров по меткам,
# см. шапку ai-review.yml). Предохранители workflow (conclusion == success,
# review:ok, совпадение head) при чистом подтягивании все зелёные — дорогой
# вызов модели стартует вхолостую. Критерий приёмки задачи («слияние одного
# PR не порождает прогонов второго гейта у остальных») этим не выполнялся.
#
# should_run_ai_review — общее решение «нужен ли этот прогон вообще»,
# читаемое ai-review.yml (подкоманда ai_review.py::cmd_should_run) ДО того,
# как job перейдёт к доверенному чекауту/gather/DSH: то же место правды, что
# и diff_unchanged/ai_verdicts_to_drop, чтобы workflow не завёл вторую копию
# условия рядом с check_pr.py.

def should_run_ai_review(current_labels, stored_fingerprint: str | None,
                          current_fingerprint: str) -> bool:
    """True — второй гейт обязан выполнить дорогой прогон; False — прогон
    можно пропустить целиком (ai-review.yml отдаёт go=false до чекаута/DSH).

    Пропуск возможен ТОЛЬКО когда на PR уже стоит ОКОНЧАТЕЛЬНЫЙ вердикт
    (`ai:ok`/`ai:changes-requested`) и его отпечаток диффа совпал с текущим
    (`diff_unchanged`) — ревьюер уже видел ровно этот код. `ai:failed`
    НИКОГДА не пропускает прогон, даже если дифф не менялся: у него
    отдельный, ранее выданный газ на автоповтор по таймеру
    (`scheduler.py::trigger_ai_review`, #196) — совпавший отпечаток не
    должен отнимать этот газ, иначе повторная попытка после сбоя
    провайдера/транспорта молча перестанет случаться. Вердикта нет вовсе
    (первое ревью PR) — прогон тоже нужен, пропускать нечего.

    Намеренно НЕ читает «летит ли прогон прямо сейчас» (#779, разрыв 4):
    второй копии other_active_ai_review_runs здесь не заводим — эта функция
    отвечает только на вопрос «этот код уже ревьюили», а «его ревьюят прямо
    сейчас» решает other_active_ai_review_runs у ВСЕХ вызывающих (cmd_should_run
    для обоих путей триггера, trigger_ai_review) ДО обращения сюда. С этой
    проверкой выше по стеку у ai:failed эффективно получается «повтор нужен,
    если прогона сейчас не летит» — один повтор на неизменившийся отпечаток,
    а не столько, сколько раз сработает таймер, — без переписывания самого
    предиката отпечатка."""
    names = _names(current_labels)
    if AI_FAILED in names:
        return True
    if not (names & {AI_OK, AI_CHANGES}):
        return True
    return not diff_unchanged(stored_fingerprint, current_fingerprint)


# Шапка-факты ревью-комментария: pr/head/reviewer (ai_review.build_comment)
# плюс diff — отпечаток diff_fingerprint на момент вердикта (#252), плюс
# provider/reset-at — факты цепочки провайдеров (#727): имя провайдера,
# фактически ответившего, и даты сброса опробованных — читает
# scheduler.py::trigger_ai_review, чтобы не жечь авто-повтор (#196) вслепую
# в ту же квоту. reason — тег причины verdict=error (#431, см.
# FAILURE_REASON_* выше). Разбор останавливается на первой пустой строке,
# чтобы проза/фенсы ниже не притворялись фактами (см. header_facts). Одно
# место правды — раньше жило только в ai_review.py, check_pr.py читало бы
# вторую копию regex.
FACT_RE = re.compile(r"^(pr|head|reviewer|diff|provider|reset-at|reason):\s*(.+)$")


def transport_failed(dsh_rc: str) -> bool:
    """True — DSH не смог вызвать модель вовсе (rc≠0: сеть, 404, таймаут).

    Единственный источник истины — код возврата dsh (ai_dsh.sh пишет его в
    dsh_rc.txt, независимо от содержимого ответа). Пусто/не-число — код
    неизвестен (экзотический обрыв шага раннера) и по умолчанию НЕ считается
    транспортным сбоем: ложное «инфраструктура сломана» хуже, чем чуть менее
    точный «модель ответила не по контракту» в редком крайнем случае.

    Одно место правды (#431): раньше жила только в ai_review.py — scheduler
    (reason_tag ниже) теперь тоже классифицирует по ней, второй копии не
    заводим (ai_review.transport_failed — реэкспорт отсюда, как и header_facts)."""
    try:
        return int(dsh_rc) != 0
    except (TypeError, ValueError):
        return False


def reason_tag(dsh_rc: str, failure_reason: str = "") -> str:
    """Короткий машиночитаемый тег причины verdict=error — одно из
    FAILURE_REASON_* выше. Пара к ai_review.error_reason (тот же порядок
    проверки и те же входы), но возвращает тег для шапки комментария
    (`reason:`, см. FACT_RE), не текст для человека — scheduler.trigger_ai_review
    (#431) решает по тегу, не по прозе findings (findings может оказаться
    и текстом самой модели, см. FAILURE_REASON_* докстринг выше)."""
    if failure_reason == FAILURE_REASON_QUOTA_EXHAUSTED:
        return FAILURE_REASON_QUOTA_EXHAUSTED
    if failure_reason == FAILURE_REASON_RATE_LIMIT_BUDGET:
        return FAILURE_REASON_RATE_LIMIT_BUDGET
    if transport_failed(dsh_rc):
        return FAILURE_REASON_TRANSPORT
    return FAILURE_REASON_CONTRACT

# ── Автор вердикта — не любой комментатор (дыра, найдена вердиктом ai-review
# PR #294, у неё выше приоритет, чем у самого #294) ──────────────────────────
#
# Шапка `reviewer:`/`diff:` — это ТЕКСТ ТЕЛА комментария, его пишет автор
# комментария, а не GitHub. Репозиторий публичный: до этой правки
# latest_ai_comment брала последний комментарий с такой шапкой от ЛЮБОГО
# user.login. diff_fingerprint считается из публичного `pulls/{n}/files`
# (см. diff_fingerprint выше) — его может вычислить и опубликовать в
# поддельном комментарии кто угодно, получив `ai_verdict_keep == True` на
# реально изменённом диффе и `should_run_ai_review == False`: дорогое
# AI-ревью пропускается молча, merge_label_gate смотрит только метки — и
# непроверенный код едет в main. Наш же фикс #252/#294 открыл этот канал:
# до него check_pr.py снимал ai:*-метки безусловно, комментарии в решение
# гейта не входили вовсе.
#
# Проверено по факту на PR #294 (2026-09-05), а не по предположению:
#   gh api "repos/mytab0r/edge-harness/issues/294/comments" \
#     --jq '.[]|select(.body|test("reviewer:"))|"\(.user.login) \(.user.type)"'
# все 4 настоящих ai-ревью-комментария — "github-actions[bot] Bot": вердикт
# публикует шаг verdict workflow ai-review.yml через `gh -f body=...` от
# имени GITHUB_TOKEN. user.login/user.type в ответе GitHub API — это факт
# об АВТОРЕ комментария в базе GitHub, не текст, который пишет автор, и
# подделать его публикацией нового комментария нельзя.
#
# ── Следствие для «гейт медленный, поставлю ai:ok вручную» (#828) ───────────
#
# Живой случай: PR #818/#819/#826 (2026-09-09) — владелец вручную ставил
# ai:ok вместе с самодельным комментарием, имитирующим формат вердикта
# ("pr: N | reviewer: approve\n\n## Вердикт второго гейта — bootstrap..."),
# в обход настоящего прогона ai-review.yml. Следующий же пуш (даже чистое
# подтягивание main, дифф не менялся) снимал ai:ok — не потому, что
# diff_fingerprint ошибся (он трёхточечный и устойчив к подтягиванию, #740),
# а потому что _is_trusted_verdict_author отвергает автора-человека:
# latest_ai_comment не находит НИ ОДНОГО доверенного вердикта на PR вообще,
# ai_verdict_keep получает stored_fp=None и обязан снять метку (по контракту
# diff_unchanged: «нет отпечатка — считаем изменившимся», как и должно быть
# при сомнении, AGENTS.md). Разбор — issue #828, регресс-тест на буквальном
# тексте комментария PR #818 — scripts/review/test_check_pr.py
# (test_ai_ok_from_bootstrap_comment_never_survives_next_push_even_unchanged_diff).
#
# Это НЕ баг и чинить его смягчением проверки автора нельзя — тогда снова
# открылась бы дыра #294 (любой участник публичного репозитория подделывает
# approve). Метка, поставленная в обход ai-review.yml, физически не может
# получить устойчивый к пушам отпечаток, потому что отпечаток живёт только в
# комментарии ДОВЕРЕННОЙ учётки. Если ai-review.yml реально не отвечает
# (квота/сбой провайдера) — газ уже есть и назван в LABELS.md (`ai:failed`):
# `gh workflow run ai-review.yml -f pr=N -f force=true` заводит настоящий
# прогон, который публикует комментарий от github-actions[bot] с реальным
# отпечатком — тогда keep-путь #252 сработает как задумано на следующем
# чистом подтягивании. Хендрафченный комментарий этого не даёт никогда.
TRUSTED_VERDICT_LOGIN = "github-actions[bot]"


def _is_trusted_verdict_author(comment: dict) -> bool:
    """True — комментарий опубликован сервисной учёткой GITHUB_TOKEN самого
    workflow, не посторонним читателем публичного репозитория. Единственное
    место правды на признак автора — latest_ai_comment (этот модуль) и
    file_tasks.latest_review_comment опираются на неё же, не на свою копию."""
    author = comment.get("user") or {}
    return author.get("login") == TRUSTED_VERDICT_LOGIN and author.get("type") == "Bot"


def header_facts(comment_body: str) -> dict[str, str]:
    lines = (comment_body or "").splitlines()
    facts: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            break  # шапка кончилась: дальше проза и фенсы, не факты
        match = FACT_RE.match(line.strip())
        if match:
            facts[match.group(1)] = match.group(2).strip()
    return facts


def latest_trusted_comment(repo: str, pr: int, gh_func, body_matches,
                           since: datetime | None = None) -> dict | None:
    """Последний комментарий ДОВЕРЕННОЙ сервисной учётки
    (_is_trusted_verdict_author), тело которого удовлетворяет
    `body_matches(body)` — общий обход «найти свой прошлый комментарий на PR».

    Единственное место цикла «постранично (list_pages) + фильтр доверия»:
    раньше он жил в latest_ai_comment, и идемпотентность комментариев
    contract:failed/находок ревью (#203) требовала бы точных копий —
    очередной экземпляр класса дублированного обхода, который уже собирали
    в list_pages (#308). Комментарии от кого угодно, кроме доверенной
    учётки, пропускаются ДО разбора тела: репозиторий публичный, посторонний
    участник может опубликовать любой текст (находка вердикта ai-review
    PR #294) — доверять телу можно только после проверки автора, не вместо
    неё. Порядок выдачи API сохраняется (extend по страницам подряд),
    «последний по порядку среди доверенных» возвращается как есть.

    `since` — необязательный якорь эпохи (находка ревью PR #439): без него
    функция читает всю историю PR, что для check_pr.py/ai_review.py верно
    (им нужен последний вердикт вообще, для сравнения отпечатка диффа), но
    неверно там, где решение обязано различать «вердикт ЭТОЙ эпохи» от
    «вердикт эпохи прошлой» (scheduler.latest_ai_failure_reason) — комментарии
    с created_at <= since пропускаются целиком, как будто их не было."""
    latest = None
    for comment in list_pages(f"repos/{repo}/issues/{pr}/comments?per_page=100", gh_func):
        if not _is_trusted_verdict_author(comment):
            continue
        if since is not None:
            created_at = comment.get("created_at")
            if not created_at or datetime.fromisoformat(created_at.replace("Z", "+00:00")) <= since:
                continue
        if body_matches(comment.get("body") or ""):
            latest = comment
    return latest


def _is_ai_verdict_body(body: str) -> bool:
    return header_facts(body).get("reviewer") in ("approve", "rework", "error")


def latest_ai_comment(repo: str, pr: int, gh_func, since: datetime | None = None) -> dict | None:
    """Последний комментарий AI-ревью PR (шапка с решающим `reviewer:`,
    опубликованный доверенной учёткой — _is_trusted_verdict_author) —
    источник сохранённого отпечатка диффа для check_pr.py. `gh_func` —
    вызывающий `gh(*args)` того же модуля (subprocess-обёртка над `gh api`,
    паттерн уже используемый в check_pr/ai_review) — сеть здесь не
    зашивается, чтобы функция оставалась инъекцией зависимости и её решение
    (diff_unchanged) проверялось без сети.

    Обход и фильтр доверия — общий latest_trusted_comment выше (#294, #308),
    включая необязательный якорь эпохи `since` (#431/#439): матч по значению
    шапки `reviewer:` отделён в _is_ai_verdict_body."""
    return latest_trusted_comment(repo, pr, gh_func, _is_ai_verdict_body, since=since)


# ── Идемпотентная публикация вердиктов и комментариев провала (#203) ─────────
#
# Оба гейта на каждом прогоне безусловно перевешивали то, что уже висит:
# check_pr.py снимал и ставил вердикт-метку заново (unlabeled+labeled одного
# и того же значения в таймлайне PR), contract_check.py POST-ил заново
# одинаковый комментарий провала. Замер по живому репозиторию
# (scripts/measure/label_churn_203.py, окно 2026-09-05T03:00..2026-09-06T03:00Z):
# 213 событий labeled за сутки, из них 88 — review:ok, при этом unlabeled
# review:ok — 74, а реальных смен вердикта (в review:changes-requested — 1,
# в review:large — 10) на порядок меньше: подавляющая часть — чистая
# перестановка без изменения решения. Плюс цепочки дублей комментария
# контракта (#162 — 9 штук, #173/#191 — по 3). orchestra.yml слушает
# `labeled` — лишний своп метки означает лишний прогон.

def verdict_label_changes(current, verdict: str,
                          verdicts=REVIEW_VERDICTS) -> tuple[list[str], bool]:
    """`(метки к снятию, ставить ли вердикт)` — единственное место решения
    идемпотентной перестановки метки-вердикта (гейт 1: review:*; тот же код
    принимает `verdicts=AI_VERDICTS` у гейта 2).

    Вердикт не изменился → `([], False)`: вызывающий не выполняет НИ ОДНОГО
    изменяющего вызова — ни лишнего unlabeled/labeled в таймлайне, ни
    триггера `on: pull_request: [labeled]`. Вердикт сменился → чужие
    вердикты из `verdicts` снимаются, актуальный ставится (обратная
    проверка: перестановка при смене решения обязана остаться). Метки вне
    `verdicts` (review:large-ok, ai:*, conflict) решение не трогает — у них
    свои газы (LABELS.md)."""
    names = _names(current)
    stale = sorted(label for label in verdicts if label != verdict and label in names)
    return stale, verdict not in names


# Первые строки «своих» комментариев: константа здесь, рядом с матчером, а
# вторая копия строки не заводится — сборщики тела импортируют оттуда же.
CONTRACT_FAIL_HEADER = "Контракт PR ↔ задача нарушен:"
REVIEW_FINDINGS_HEADER = "Ревью нашло замечания:"


def comment_update_action(existing: dict | None, new_body: str) -> str | None:
    """`'post'` | `'patch'` | `None` — что делать с комментарием провала при
    повторном прогоне с тем же результатом (#203, критерий приёмки: второго
    одинакового комментария появляться не должно).

    `None` — комментарий уже висит с ТОЧНО этим текстом (найден
    latest_comment_by_header, то есть от доверенной учётки): молчание —
    здесь не silent-wrong, а доказанное «ничего не изменилось».
    `'patch'` — свой комментарий есть, но текст нарушений другой:
    обновляется существующий (одно живое сообщение на PR), а не цепочка
    дубликатов. `'post'` — своего комментария ещё нет."""
    if existing is None:
        return "post"
    if (existing.get("body") or "").strip() == new_body.strip():
        return None
    return "patch"


def latest_comment_by_header(repo: str, pr: int, gh_func, header: str) -> dict | None:
    """Последний ДОВЕРЕННЫЙ комментарий, начинающийся с `header`, — поиск
    «своего прошлого комментария» для comment_update_action. Совпадение по
    первой строке: `header` — маркер издателя, живёт константой рядом
    (CONTRACT_FAIL_HEADER/REVIEW_FINDINGS_HEADER) и в сборщике тела, и здесь,
    в одном экземпляре строки. Посторонний комментарий с тем же текстом
    доверия не получает (latest_trusted_comment) и заглушить гейт не может —
    дубликат публикуется, а не пропадает."""
    return latest_trusted_comment(repo, pr, gh_func,
                                  lambda body: body.startswith(header))


# ── Commit Status API: вторая проводка вердикта, не второй источник (#345) ───
#
# Мотив — docs/research/23-platform-native-vs-custom.md п.2: `allow_auto_merge`
# (включён на репозитории) читает required status checks, не метки. Метка
# остаётся единственным местом ПРИНЯТИЯ решения (merge_label_gate/scheduler
# её не трогаем этой задачей) — статус только ЗЕРКАЛИТ то же решение вторым
# каналом, вычисляясь из той же переменной вердикта в check_pr.py/ai_review.py.

def run_target_url(repo: str) -> str | None:
    """target_url текущего прогона Actions (GITHUB_SERVER_URL/{repo}/actions/runs/{id}).

    None вне Actions (локальный запуск, тест, ручной вызов без окружения
    раннера) — статус тогда публикуется без ссылки, не падает: отсутствие
    диагностической ссылки не то же самое, что отсутствие самого вердикта."""
    server = os.environ.get("GITHUB_SERVER_URL")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not server or not run_id:
        return None
    return f"{server}/{repo}/actions/runs/{run_id}"


def post_commit_status(repo: str, sha: str, context: str, state: str,
                        description: str, run_gh_func,
                        target_url: str | None = None) -> None:
    """POST /repos/{repo}/statuses/{sha} — вердикт вторым каналом, тем же
    состоянием, что метка (см. review_status_state/ai_status_state).
    `run_gh_func` — вызывающий `gh(*args)`/`run_gh` того же модуля (паттерн
    остальных функций этого файла: сеть не зашивается сюда, инъекция
    зависимости для тестов без сети). description обрезается до 140 символов —
    жёсткий лимит самого API именно в символах (срез `[:140]` режет по
    символам Python-строки, не по байтам UTF-8 — кириллица не обрезается
    сильнее нужного), обрезка здесь, а не молчаливый отказ GitHub."""
    args = ["api", "-X", "POST", f"repos/{repo}/statuses/{sha}",
            "-f", f"state={state}", "-f", f"context={context}",
            "-f", f"description={description[:140]}"]
    if target_url:
        args += ["-f", f"target_url={target_url}"]
    run_gh_func(*args)


def status_posted_at(repo: str, sha: str, context: str, gh_func) -> str | None:
    """Момент (сырая ISO-строка, парсинг — дело вызывающего кода: не заводим
    здесь зависимость от pulse_guard.parse_time) последней публикации commit
    status `context` на точном `sha` — якорь для таймеров #196/#269, который
    НЕ зависит от идемпотентности меток (находка ревью #424): `check_pr.py`/
    `ai_review.py` публикуют `harness/review`/`harness/ai-review` КАЖДЫМ
    прогоном безусловно (#345, второй канал вердикта для required status
    checks), даже когда вердикт не изменился и `labeled`-событие с #203 не
    выбрасывается вовсе. Раньше scheduler.last_gate1_labeled_at и
    repo_invariants.last_gate1_labeled_at (две независимые копии одной
    логики, уже расходившиеся дважды — #303, #432) читали таймлайн на
    `labeled`; тот сигнал заморожен на первой простановке метки после #203 —
    один и тот же сигнал для обеих копий закрывает класс дупликации заодно
    с классом протухшего якоря.

    `GET /repos/{repo}/commits/{sha}/statuses` — короткий список (обычно
    считаные context'ы на коммит), пагинация #308 избыточна.
    None — этот context на этом sha не публиковался вовсе."""
    statuses = gh_func(f"repos/{repo}/commits/{sha}/statuses?per_page=100")
    posted = [s["created_at"] for s in statuses if s.get("context") == context]
    return max(posted) if posted else None


def review_status_state(verdict: str) -> str:
    """Состояние статуса гейта 1 по вердикт-метке (REVIEW_OK/REVIEW_CHANGES/
    REVIEW_LARGE) на МОМЕНТ ПОСЛЕДНЕГО ПУША — success только при REVIEW_OK.
    REVIEW_CHANGES и REVIEW_LARGE оба блокируют слияние — оба дают failure,
    второго промежуточного состояния тут нет.

    ВНИМАНИЕ (#702, разошлось с merge_label_gate): этот порог — НЕ то же
    самое, что открывает merge_label_gate. С #702 `merge_label_gate`
    засчитывает гейт 1 также по связке REVIEW_LARGE+LARGE_OK, но
    `review_status_state` вызывается только из `check_pr.py` на каждом
    пуше, а `LARGE_OK` ставится отдельной меткой (`apply_large_ok`) БЕЗ
    нового пуша — на PR с REVIEW_LARGE+LARGE_OK+ai:ok этот статус может
    остаться `failure`, пока merge_label_gate уже открыт. Не чинится здесь
    (нужна перепубликация статуса из apply_large_ok — заведено отдельной
    задачей из ревью PR #704), только называется, чтобы вызывающий код не
    полагался на равенство порогов, которое здесь описывалось раньше."""
    return "success" if verdict == REVIEW_OK else "failure"



# ── Ручной workflow_dispatch не должен дублировать прогон, который уже
# идёт или уже вынес окончательный вердикт (#399) ───────────────────────────
#
# Аудит 197 платных прогонов ai-review за 2026-09-05/06 нашёл утечку: 44 из
# них (22%) — ручные workflow_dispatch под личным токеном владельца,
# дублирующие уже идущий или уже завершённый прогон на том же PR, БЕЗ
# ai:failed. Причина — ai_review.py::cmd_should_run с --force не заходил в
# сеть вовсе, а сам workflow выставлял --force БЕЗУСЛОВНО для любого
# workflow_dispatch (см. Дельта 2026-09-05 в
# docs/decisions/0007-ai-review-gate.md), минуя should_run_ai_review выше
# целиком. --force остаётся, но только по явному input force:true — по
# умолчанию ручной запуск проходит ТУ ЖЕ сверку, что и автоматический.
#
# Отдельная гонка, которую сверка отпечатка не ловит: PR ещё БЕЗ вердикта
# (первое ревью), но прогон УЖЕ идёт прямо сейчас — should_run_ai_review
# честно вернёт True (вердикта нет — прогон нужен), хотя второй одновременный
# прогон того же PR бессмыслен. Единственный надёжный признак «этот прогон
# про PR N» — run-name (см. `run-name:` в ai-review.yml): у самого
# ai-review-рана head_branch/head_sha ВСЕГДА "main" (job чекаутит main первым
# шагом) независимо от триггера — проверено на живых прогонах 2026-09-06
# (`gh api .../actions/workflows/ai-review.yml/runs` отдаёт
# head_branch=head_sha=main и для workflow_run, и для workflow_dispatch),
# поэтому матч по head_sha/head_branch не отличает прогоны разных PR вовсе.

AI_REVIEW_WORKFLOW_FILE = "ai-review.yml"
# Префикс совпадает с run-name: в ai-review.yml дословно — тест
# test_ai_review_workflow_run_name_uses_review_labels_prefix в
# scripts/review/test_ai_review.py сверяет буквально, иначе стороны могут
# разойтись молча (yml не читает эту константу — два языка).
AI_REVIEW_RUN_NAME_PREFIX = "ai-review PR #"

# Потолок возраста для «прогон ai-review.yml летит прямо сейчас» (#779,
# блокирующая 2 второго гейта): то же число, что `timeout-minutes:` самого
# job'а review в ai-review.yml — тест
# test_ai_review_timeout_minutes_matches_review_labels_constant в
# scripts/review/test_ai_review.py сверяет буквально (yml не читает эту
# константу — два языка, как и AI_REVIEW_RUN_NAME_PREFIX выше). Без потолка
# `queued`/`in_progress` читались бы как «летит» сколько угодно долго, хотя
# GitHub сам оборвёт job по timeout-minutes — окно конечно, а предикат об
# этом не знал: 40-часовой мнимый «летит» глушил бы разом занятость
# (other_active_ai_review_runs) и автоповтор (trigger_ai_review), не тратя
# бюджет попыток, и инвариант 3 (repo_invariants.retry_budget_fact) молчал
# бы «бюджет ещё есть», хотя двигаться он не может.
AI_REVIEW_TIMEOUT_MINUTES = 130


def parse_github_timestamp(raw: str | None) -> datetime | None:
    """Единственное место разбора метки времени формата Actions/Issues API
    (`created_at`) в aware datetime UTC — не бросает исключений наружу
    (#780, доводка #779). Класс: `datetime.fromisoformat(raw.replace("Z",
    "+00:00"))` ПАРСИТ метку без offset ("Z"/"+HH:MM") как наивный datetime
    без ValueError — падение приходит НИЖЕ по коду, на `aware - naive`
    вычитании/сравнении. До этой правки класс был закрыт по одной копии
    `except (ValueError, TypeError)` в каждом вызывающем месте
    (`other_active_ai_review_runs` и `ai_review._verdict_label_and_age`) —
    второе появилось в этом же PR #779 и сперва ловило только ValueError
    (находка доводки #780). Копия try/except на каждое новое место —
    отложенный рецидив (AGENTS.md «одно место правды»): эта функция вместо
    этого нормализует наивный результат в aware (UTC — Actions/Issues API
    прод-формой всегда её и подразумевает, #779) и возвращает None на любую
    строку, которая не разбирается вовсе. Вызывающий трактует None как
    «метки нет» — ровно то же решение, что раньше принимал `except`."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def ai_review_run_name(pr: int) -> str:
    """run-name прогона ai-review.yml на PR #pr — зашивается в сам прогон
    ДО старта job'а (workflow-level `run-name:`, вычисляется из события: у
    workflow_dispatch — input `pr`, у workflow_run — из
    `github.event.workflow_run.pull_requests[0].number`, доступного в
    событии pr-review БЕЗ похода в API). Форк-PR — известное ограничение:
    `pull_requests` пуст для PR не из этого репозитория (документированное
    поведение GitHub) — тогда автоматический прогон получает run-name без
    номера PR и этой функцией не матчится: деградация признака для форков,
    не молчаливая ошибка гейта (остальные PR защищены)."""
    return f"{AI_REVIEW_RUN_NAME_PREFIX}{pr}"


def other_active_ai_review_runs(repo: str, pr: int, exclude_run_id, gh_func,
                                 now: datetime | None = None) -> list[dict]:
    """Прогоны `ai-review.yml` (`queued`/`in_progress`) для PR #pr, кроме
    прогона `exclude_run_id` (себя) — единственное место правды на «летит ли
    прогон этого PR прямо сейчас» (#779, критерий 4: третьей копии этой
    проверки не заводим). Шесть точек вызова читают её же: ручной
    workflow_dispatch И событийный workflow_run (`ai_review.py::cmd_should_run`,
    #399 и #779 разрыв 1), `scheduler.py::trigger_ai_review` ПЕРЕД диспатчем
    (#779 разрыв 2), `scheduler.py::update_branch` (#488),
    `mechanical_rebase.py` (issue #764, тот же тормоз, что update_branch —
    не двигаем head механическим рёбейзом, пока по PR летит ai-review.yml) и
    `repo_invariants.py::retry_budget_fact` (#779, блокирующая 3 — держит ли
    летящий прогон автоповтор, для инварианта 3) — второй одновременный
    прогон денег ждать не должен, а подтягивание/рёбейз ветки не должны
    двигать head из-под летящего ревью.

    Не листает глубже одной страницы на статус (`per_page=100`): одновременно
    активных прогонов одного workflow на масштабе этого репозитория ожидается
    единицы, не сотни — при реальном превышении это отдельная, более крупная
    проблема, которую этот гейт не обязан решать.

    Потолок возраста (#779, блокирующая 2): прогон старше
    AI_REVIEW_TIMEOUT_MINUTES по `created_at` летящим не считается — GitHub
    сам оборвёт такой job по `timeout-minutes` job'а `review` в
    .github/workflows/ai-review.yml (число сверяется тестом
    test_ai_review_timeout_minutes_matches_review_labels_constant, не
    номером строки — тот протухает при вставке строк выше), а без
    потолка он маскировал бы занятость бесконечно. Отсутствие/битый
    `created_at` — не повод молча исключить прогон из списка активных (это
    была бы деградация в обратную сторону, тише про реальную занятость);
    такой прогон остаётся в списке как раньше.

    `now` — по умолчанию реальное время; параметр существует только для
    детерминированных тестов потолка (никто из шести вызывающих его не
    передаёт).

    Неточность потолка для `queued` (не блокирует, названо явно #780,
    доводка #779): `timeout-minutes` GitHub применяет ко времени ИСПОЛНЕНИЯ
    job'а (`in_progress`), а не ко времени ожидания в очереди — для
    `queued`-прогона фраза «GitHub сам оборвёт по timeout-minutes» выше
    буквально неверна: часы обрыва не идут, пока прогон не стартовал.
    Практическое окно узкое (concurrency-группа по номеру PR, эта же правка
    #779, не пускает второй `queued`-прогон дальше одного ждущего, а тот
    стартует не позже, чем завершится/оборвётся летящий, ограниченный теми
    же AI_REVIEW_TIMEOUT_MINUTES) — но это довод о практике, не о буквальном
    смысле `timeout-minutes`, и подменять его текстом «GitHub оборвёт»
    буквально для обоих статусов сразу — то же самое приближение, которое
    эта функция обязана называть честно, а не молчать.

    Граница форк-PR (не блокирует, названо явно #779): у форк-PR
    `pull_requests[0]` пуст, `ai_review_run_name`-фолбэк даёт голый
    "ai-review" вместо "ai-review PR #N" (см. её докстринг) — тогда ЭТА
    функция не находит своих же прогонов вовсе (`display_title != target`
    для любого форк-прогона), то есть на форк-PR выключены ОБА тормоза
    одновременно: и «прогон уже летит», и очередь concurrency-группы
    ai-review.yml (та же деградация в fallback, тот же корень). Сегодня
    форк-PR в репозитории нет (прочёс 1425 прогонов, #779: голые
    display_title — все ДО внесения run-name, в свежих 600 аномалий ноль,
    head_repository у всех свой) — граница не устранена, только названа.

    Слепота к статусу `pending` (не блокирует, названо явно #779): Actions
    API знает статусы `queued`/`in_progress`/`completed`/`waiting`/
    `requested`/`pending` — эта функция опрашивает только первые два.
    Concurrency-группа по номеру PR (эта же правка #779) ВПЕРВЫЕ в истории
    репозитория создаёт `pending`-прогоны (второй триггер того же PR ждёт
    своей очереди в группе). Для `cmd_should_run` слепота к `pending` делает
    схему верной: летящий прогон не видит ждущего и спокойно публикует
    вердикт, ждущий стартует уже в одиночку и отказывает дёшево по
    отпечатку — на этом слепота и держится, а не вопреки ей. Для трёх
    других вызывающих (`update_branch`, `mechanical_rebase.py`,
    `trigger_ai_review`) это то же самое узкое окно, где тормоз не
    срабатывает: они могут сдвинуть head/задиспатчить повтор, пока
    `pending`-прогон ждёт своей очереди, невидимый им."""
    now = now or datetime.now(timezone.utc)
    target = ai_review_run_name(pr)
    matches: list[dict] = []
    for status in ("in_progress", "queued"):
        chunk = gh_func(
            f"repos/{repo}/actions/workflows/{AI_REVIEW_WORKFLOW_FILE}/runs"
            f"?status={status}&per_page=100")
        runs = chunk.get("workflow_runs", []) if isinstance(chunk, dict) else []
        for run in runs:
            if run.get("display_title") != target:
                continue
            if str(run.get("id")) == str(exclude_run_id):
                continue
            created_at = run.get("created_at")
            created = parse_github_timestamp(created_at)
            # Битая/наивная строка (см. parse_github_timestamp) даёт None —
            # прогон остаётся в списке активных как раньше, без ValueError/
            # TypeError наружу (#780, доводка #779).
            if created is not None and now - created > timedelta(minutes=AI_REVIEW_TIMEOUT_MINUTES):
                continue  # старше потолка — GitHub оборвёт сам, не блокируем
            matches.append(run)
    return matches


def ai_status_state(verdict: str) -> str:
    """Состояние статуса гейта 2 по вердикту ai_review.parse_verdict
    (approve/rework/error, НЕ по имени метки): approve → success,
    rework → failure, error → pending.

    error — это НЕ вердикт о коде: ai_review.error_reason различает три
    состояния, и error чаще всего означает сбой провайдера/транспорта DSH
    (transport_failed), у которого уже есть свой газ — автоповтор по таймеру
    (scheduler.py::trigger_ai_review, #196), не зависящий от того, что стоит
    на PR сейчас. `failure` держал бы required status check красным
    НАВСЕГДА до следующего пуша человеком (в отличие от метки, которую
    сбрасывает следующий прогон конвейера, обязательная проверка сама себя
    не пересчитывает) — то есть код мог быть безупречен, а слияние
    заблокировано так, будто ревью его отвергло. `pending` — точное описание
    факта: решение ещё не вынесено, придёт с автоповтором; ложноположительным
    `success` это не грозит, потому что pending не открывает auto-merge.
    """
    if verdict == "approve":
        return "success"
    if verdict == "rework":
        return "failure"
    return "pending"
