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
когда подтягивание не изменило дифф») — см. diff_unchanged.

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

# ── Конфликт (mark_conflicts, scheduler.py) ──────────────────────────────────
# Единственное определение (было задублировано локальной константой в
# scheduler.py) — should_update_branch ниже читает её же.
CONFLICT_LABEL = "conflict"

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


def merge_label_gate(labels) -> str | None:
    """Причина, по которой метки запрещают слияние; None — метки слияние открыли.

    Единственное место, где гейт слияния формулируется словами: scheduler
    печатает причину в отчёт, тесты доказывают обе ветки. Порог «оба гейта
    зелёные» — здесь, а не в вызывающем коде.
    """
    names = _names(labels)
    if REVIEW_OK not in names:
        return f"нет вердикта {REVIEW_OK} (ждёт детерминированное ревью или доработку)"
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
    невозможным вместо того, чтобы полагаться на дисциплину вызывающих."""
    if "per_page=100" not in url:
        raise ValueError(
            f"list_pages требует per_page=100 в URL (стоп-условие "
            f"len(chunk) < 100 иначе молча теряет хвост): {url!r}")
    page = 1
    items: list[dict] = []
    while True:
        chunk = gh_func(f"{url}&page={page}")
        if not isinstance(chunk, list) or not chunk:
            break
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


def diff_fingerprint(files) -> str:
    """Отпечаток содержимого диффа PR — sha256 по отсортированному списку
    `имя_файла:статус:blob-sha` из прод-формы `gh api .../pulls/{n}/files`.

    Почему blob-sha, а не число строк/изменений: длина диффа — не признак
    содержимого, две разные правки могут случайно дать одинаковое число
    добавленных/удалённых строк (класс, который явно назвала задача #252).
    `sha` каждого файла в этом ответе — SHA блоба GitHub на голове PR
    (для removed — блоб удалённого содержимого): он меняется тогда и только
    тогда, когда меняются байты файла, поэтому две разные правки одного
    размера получают разные отпечатки, а чистое подтягивание main (файлы PR
    не тронуты) — тот же самый. Сортировка по строке снимает зависимость от
    порядка страниц API; статус в строке отличает rename/added/removed друг
    от друга даже при совпадении итогового имени файла.
    """
    parts = sorted(
        f"{f.get('filename', '')}:{f.get('status', '')}:{f.get('sha', '')}"
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
    """
    names = _names(current_labels)
    if AI_FAILED in names:
        return True
    if not (names & {AI_OK, AI_CHANGES}):
        return True
    return not diff_unchanged(stored_fingerprint, current_fingerprint)


# Шапка-факты ревью-комментария: pr/head/reviewer (ai_review.build_comment)
# плюс diff — отпечаток diff_fingerprint на момент вердикта (#252). Разбор
# останавливается на первой пустой строке, чтобы проза/фенсы ниже не
# притворялись фактами (см. header_facts). Одно место правды — раньше жило
# только в ai_review.py, check_pr.py читало бы вторую копию regex.
FACT_RE = re.compile(r"^(pr|head|reviewer|diff):\s*(.+)$")

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


def latest_ai_comment(repo: str, pr: int, gh_func) -> dict | None:
    """Последний комментарий AI-ревью PR (шапка с решающим `reviewer:`,
    опубликованный доверенной учёткой — _is_trusted_verdict_author) —
    источник сохранённого отпечатка диффа для check_pr.py. `gh_func` —
    вызывающий `gh(*args)` того же модуля (subprocess-обёртка над `gh api`,
    паттерн уже используемый в check_pr/ai_review) — сеть здесь не
    зашивается, чтобы функция оставалась инъекцией зависимости и её решение
    (diff_unchanged) проверялось без сети.

    Комментарии от кого угодно, кроме доверенной учётки, пропускаются ДО
    разбора шапки: посторонний участник публичного репозитория может
    опубликовать комментарий с валидной шапкой reviewer:/diff: (находка
    дыры безопасности, вердикт ai-review PR #294) — доверять телу
    комментария можно только после проверки автора, не вместо неё.

    Листает все страницы (per_page=100) — эндпоинт комментариев не
    поддерживает сортировку по убыванию (замер file_tasks.py на PR #138),
    поэтому «последний» ищем перебором, как уже делает file_tasks.latest_review_comment.
    Обход страниц — тот же list_pages, что у list_pr_files/list_timeline
    выше (#308): полный список собирается сначала, «последний подходящий»
    ищется одним проходом по нему — порядок выдачи API list_pages сохраняет
    (extend по страницам подряд), поэтому семантика «последний по порядку
    среди доверенных с решающей шапкой» не меняется."""
    latest = None
    for comment in list_pages(f"repos/{repo}/issues/{pr}/comments?per_page=100", gh_func):
        if not _is_trusted_verdict_author(comment):
            continue
        facts = header_facts(comment.get("body") or "")
        if facts.get("reviewer") in ("approve", "rework", "error"):
            latest = comment
    return latest


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


def review_status_state(verdict: str) -> str:
    """Состояние статуса гейта 1 по вердикт-метке (REVIEW_OK/REVIEW_CHANGES/
    REVIEW_LARGE) — success только при REVIEW_OK, ровно тот же порог, что
    merge_label_gate. REVIEW_CHANGES и REVIEW_LARGE оба блокируют слияние —
    оба дают failure, второго промежуточного состояния тут нет."""
    return "success" if verdict == REVIEW_OK else "failure"


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
