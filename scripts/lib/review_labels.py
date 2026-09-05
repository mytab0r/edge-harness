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
"""

import hashlib
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
    вердиктом. Вердикт AI действителен только для head, на котором сделан:
    новый пуш запускает новое AI-ревью, а старая метка не должна доживать
    до окна слияния (класс «протухшая метка открывает гейт»).
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


# Шапка-факты ревью-комментария: pr/head/reviewer (ai_review.build_comment)
# плюс diff — отпечаток diff_fingerprint на момент вердикта (#252). Разбор
# останавливается на первой пустой строке, чтобы проза/фенсы ниже не
# притворялись фактами (см. header_facts). Одно место правды — раньше жило
# только в ai_review.py, check_pr.py читало бы вторую копию regex.
FACT_RE = re.compile(r"^(pr|head|reviewer|diff):\s*(.+)$")


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
    """Последний комментарий AI-ревью PR (шапка с решающим `reviewer:`) —
    источник сохранённого отпечатка диффа для check_pr.py. `gh_func` —
    вызывающий `gh(*args)` того же модуля (subprocess-обёртка над `gh api`,
    паттерн уже используемый в check_pr/ai_review) — сеть здесь не
    зашивается, чтобы функция оставалась инъекцией зависимости и её решение
    (diff_unchanged) проверялось без сети.

    Листает все страницы (per_page=100) — эндпоинт комментариев не
    поддерживает сортировку по убыванию (замер file_tasks.py на PR #138),
    поэтому «последний» ищем перебором, как уже делает file_tasks.latest_review_comment."""
    page = 1
    latest = None
    while True:
        chunk = gh_func(f"repos/{repo}/issues/{pr}/comments?per_page=100&page={page}")
        if not isinstance(chunk, list) or not chunk:
            break
        for comment in chunk:
            facts = header_facts(comment.get("body") or "")
            if facts.get("reviewer") in ("approve", "rework", "error"):
                latest = comment
        if len(chunk) < 100:
            break
        page += 1
    return latest
