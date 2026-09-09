#!/usr/bin/env python3
"""AI-ревью диффа: второй гейт конвейера после детерминированного ревью (#18).

Trust-зона разрезана по шагам workflow ai-review:

  should-run (доверенный, GH_TOKEN) — trusted-facts шаг ai-review.yml: PR уже
                                      прошёл review:ok, но дорогой прогон нужен,
                                      только если дифф действительно изменился
                                      с последнего вердикта (#294 — см. ниже)
  gather  (доверенный, GH_TOKEN)   — факты PR, дифф-пак, задача из пула,
                                      промпт по шаблону scripts/review/ai_prompt.md
  DSH     (НЕдоверенный, без токена) — агент читает репозиторий и дифф-пак,
                                      отвечает текстом; постить в GitHub не может
                                      физически: у шага нет ни GH_TOKEN, ни git-креденшелов
  verdict (доверенный, GH_TOKEN)   — разбор ответа по контракту, комментарий
                                      в PR, метка-вердикт ai:ok /
                                      ai:changes-requested / ai:failed

Контракт ответа — как у живого решения владельца в Harness (pr_loop.py):
единственный сигнал вердикта — машиночитаемая ПОСЛЕДНЯЯ строка
«ВЕРДИКТ: approve|rework»; маркера нет, их два или он не последний —
error, неоднозначность никогда не одобряет.

Вердикт привязан к head: если PR успел получить новый пуш, вердикт не
применяется (метка/комментарий не ставятся) — новый пуш сам заведёт свежее
ревью, а детерминированное ревью к тому же снимает старые ai:*-метки.

Состояние для «завести задачи в беклог одной командой» живёт в комментарии:
шапка-факты (pr/head/reviewer/diff) до первого пустой строки + канонические
блоки-заборы ````задача — парсит scripts/review/file_tasks.py. Поле diff —
отпечаток диффа PR на момент вердикта (review_labels.diff_fingerprint,
#252): check_pr.py сверяет его с текущим и сохраняет ai:*-метку, если
подтягивание main не изменило дифф PR — см. review_labels.diff_unchanged.

Триггер ai-review.yml — workflow_run от pr-review, не метка (GITHUB_TOKEN не
создаёт событий по меткам): сохранённая check_pr.py метка сама по себе не
мешает workflow_run запуститься заново на чистом подтягивании main. Находка
вердикта AI-ревью PR #294: критерий приёмки «слияние одного PR не порождает
дорогих прогонов у остальных» не выполнялся, пока сверка отпечатка жила
только в check_pr.py. Чинит cmd_should_run/review_labels.should_run_ai_review
— то же место правды, что диффов diff_fingerprint/diff_unchanged, читаемое
шагом facts ai-review.yml ДО чекаута/gather/DSH: go=false — трудный прогон
не идёт вовсе, не просто «метка не переставляется». ai:failed из этого
пропуска исключён — его автоповтор по таймеру (#196) не должен зависеть от
неизменности диффа.

Тормоз/газ размерного гейта (#204): approve на том же head, что и review:large,
автоматически ставит review:large-ok (см. apply_large_ok/large_ok_decision) —
взгляд человека делегирован состоявшемуся вердикту AI, а не факту запуска.
Диффы длиннее check_pr.LARGE_DIFF_HUGE_LINES автоматика не подтверждает —
эскалирует владельцу через pulse_guard.escalate.

Среда: runner с gh, GH_TOKEN с правами pull-requests: write (gather/verdict).
"""

import argparse
import importlib.util
import json
import os
import re
import string
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Метки-вердикты — одно место правды в lib (общее для check_pr/scheduler).
_LIB = SCRIPT_DIR.parent / "lib" / "review_labels.py"
_spec = importlib.util.spec_from_file_location("review_labels", _LIB)
review_labels = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_labels)

# Третья категория находок — чеклист некритичных замечаний в теле PR (#462):
# парсинг блока ЗАМЕЧАНИЕ и слияние с телом PR — общее место правды с
# after_merge в scheduler.py (тот же файл читает unresolved_items при
# слиянии), поэтому живёт в lib, не дублируется здесь второй копией регэкспа.
_rc_spec = importlib.util.spec_from_file_location(
    "review_checklist", SCRIPT_DIR.parent / "lib" / "review_checklist.py")
review_checklist = importlib.util.module_from_spec(_rc_spec)
_rc_spec.loader.exec_module(review_checklist)

AI_OK = review_labels.AI_OK
AI_CHANGES = review_labels.AI_CHANGES
AI_FAILED = review_labels.AI_FAILED
AI_VERDICTS = review_labels.AI_VERDICTS

# Видимый след гонки «head PR сменился во время ai-review» (cmd_verdict ниже):
# без него job зелёный, меток нет, единственный след — ::warning:: в логе шага,
# который никто не читает без явного повода (живой инцидент 2026-09-06, PR
# #488 — тормоз самой гонки см. scheduler.py::AiReviewRunning, это не он, а
# то, что уже прогнало DSH ВПУСТУЮ и вердикт всё равно некуда применить).
# Marker несёт оба sha перехода — второй прогон на ТОТ ЖЕ переход A→B (retry
# job'а на неизменной гонке) находит уже опубликованный комментарий и не
# плодит вторую копию (см. notify_head_moved).
HEAD_MOVED_MARKER_PREFIX = "<!-- ai-review:head-moved:"

# Номер задачи из текста PR/issue — одно место правды (#187): границы числа
# с обеих сторон, не подстрока (класс «#18 совпал с #180» на contract_check,
# 33570081734).
_tr_spec = importlib.util.spec_from_file_location(
    "task_ref", SCRIPT_DIR.parent / "lib" / "task_ref.py")
task_ref = importlib.util.module_from_spec(_tr_spec)
_tr_spec.loader.exec_module(task_ref)

# Пороги размерного гейта (LARGE_DIFF_LINES/LARGE_DIFF_HUGE_LINES) — одно
# место правды в check_pr.py, рядом друг с другом (#204). Импорт по файлу
# (не как пакет) — тот же паттерн, что у review_labels/task_ref выше.
_cp_spec = importlib.util.spec_from_file_location(
    "check_pr", SCRIPT_DIR / "check_pr.py")
check_pr = importlib.util.module_from_spec(_cp_spec)
_cp_spec.loader.exec_module(check_pr)

# Канал эскалации владельцу (диффы сверх LARGE_DIFF_HUGE_LINES, #204) — тот же,
# что у предохранителя конвейера: комментарий в задачу-статус + Telegram
# (pulse_guard.escalate). Второго канала для класса «нужно решение владельца»
# не заводим (см. docstring escalate).
_pg_spec = importlib.util.spec_from_file_location(
    "pulse_guard", SCRIPT_DIR.parent / "orchestra" / "pulse_guard.py")
pulse_guard = importlib.util.module_from_spec(_pg_spec)
_pg_spec.loader.exec_module(pulse_guard)

# Контракт ответа модели. Строка ВЕРДИКТ обязана быть последней непустой и
# единственной — двусмысленность это error, а не одобрение. Модель периодически
# оборачивает машиночитаемую строку в markdown-выделение (**…**/__…__) вопреки
# промпту — это тот же сигнал, что и голая строка, парсер обязан его снять.
# Группа 1 — необязательный маркер, пустая альтернатива в её же группе
# (а не «?» снаружи) нужна, чтобы backreference \1 совпал с пустой строкой,
# когда обрамления нет вовсе. Открывающий и закрывающий маркер должны
# совпадать — «*ВЕРДИКТ: approve__» не становится валидной формой. Упоминание
# approve/rework ВНУТРИ строки прозы сюда не попадает — якоря ^…$ и жёсткая
# форма это исключают.
VERDICT_RE = re.compile(r"^(\*\*|__|)ВЕРДИКТ:\s*(approve|rework)\s*\.?\1$")
# Блок задачи в беклог: ЗАДАЧА: <заголовок> … КОНЕЦ ЗАДАЧИ. Незакрытый блок
# не принимается — тихо взять половину хуже, чем не взять совсем.
TASK_OPEN_RE = re.compile(r"^ЗАДАЧА:\s*(\S.*)$")
TASK_CLOSE = "КОНЕЦ ЗАДАЧИ"
# Масштаб находки — обязательное второе поле блока задачи, сразу после
# заголовка, критерий различения в ai_prompt.md. Замер по маркерам
# `filed: #N` за реальные сутки (2026-09-05 03:37 → 2026-09-06 03:37 UTC,
# PR #313/#395/#162/#173): 22 задачи заведено автоматом за сутки (не оценка —
# прямой подсчёт по факту file_tasks.py). Из них 5/22 (23%) классифицированы
# объективным прокси-критерием («все пути к файлам из тела задачи входят в
# diff PR») как «хвост» — не были бы заведены новой логикой (см. PR #433,
# раздел «Замер на реальных данных» — это НИЖНЯЯ оценка: семантический разбор
# нескольких находок без явной ссылки на файл относит их к «хвосту» тоже).
# Отсутствие поля НЕ трактуется молча как "отдельно" (fail loud, #426): смотри
# partition_tasks ниже.
SCOPE_RE = re.compile(r"^МАСШТАБ:\s*(хвост|отдельно)\.?\s*$")
SCOPE_TAIL = "хвост"
SCOPE_SEPARATE = "отдельно"
# Строка-зависимость, ОБЯЗАТЕЛЬНАЯ и ПОСЛЕДНЯЯ перед КОНЕЦ ЗАДАЧИ (задача
# #371, продолжение #361/task_deps.py): заведение задачи в беклог из AI-ревью
# требует явного ответа «чем блокируется» — «ничем» тоже явный ответ, просто
# отсутствие строки не принимается (тот же принцип, что «незакрытый блок» —
# полуответ хуже отсутствия). Формат — структурная последняя строка, не любое
# упоминание #N в прозе тела (тот же класс, что уже закрыт task_ref.
# declared_tasks, #251/#259): парсер не гадает, он проверяет позицию.
# Значение остаётся частью тела созданной issue (человекочитаемый след);
# перенос в нативный blockedBy делает file_tasks.py::wire_declared_dependency
# при создании — там же, где номер новой issue уже известен, не отдельным
# обходом (design.md task-priority-blocking-graph, развилка б: связь ставит
# тот, кто знает факт, в момент декларации).
BLOCKED_BY_RE = re.compile(r"^БЛОКИРУЕТСЯ:\s*(ничем|#\d+(?:\s+#\d+)*)\s*$")
# Блок-забор задачи в комментарии: строится только транспортом, парсится
# file_tasks. ЧЕТЫРЕ бэктика: внутренний ```-фенс в теле задачи (пример
# кода) не закрывает блок — иначе roundtrip молча обрезал бы тело.
TASK_FENCE = "````задача"
FENCE_CLOSE_RE = re.compile(r"^`{4,}\s*$")
# Шапка-факты комментария (pr/head/reviewer/diff) — одно место правды в
# review_labels.py (#252): check_pr.py читает ту же функцию, не вторую копию
# регэкспа, чтобы разбор поля diff не разошёлся между читателем и писателем.
FACT_RE = review_labels.FACT_RE
header_facts = review_labels.header_facts
# transport_failed/reason_tag — одно место правды в review_labels.py (#431):
# scheduler.trigger_ai_review читает тот же тег из шапки комментария
# (facts["reason"]), второй копии классификации не заводим.
transport_failed = review_labels.transport_failed
reason_tag = review_labels.reason_tag


def gh(*args: str) -> dict | list:
    result = subprocess.run(
        ["gh", "api", *args],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args[:2])}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def run_gh(*args: str) -> None:
    result = subprocess.run(
        ["gh", *args],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])}: {result.stderr.strip()}")


def pr_diff(pr: int) -> subprocess.CompletedProcess:
    """`gh pr diff <pr>` как есть, без интерпретации rc/stdout — общий вызов
    для ОБЕИХ веток cmd_gather (#687): список файлов непуст — единственный
    источник диффа (обычный путь, как раньше); список пуст — второе
    независимое подтверждение того, что дифф действительно пуст (список
    файлов САМ ПО СЕБЕ недостаточен, см. cmd_gather)."""
    return subprocess.run(
        ["gh", "pr", "diff", str(pr)],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )


def redact(text: str) -> str:
    """Маскирование секретов — ТО ЖЕ место правды, что у bash-транспортов:
    scripts/lib/dsh-ci.sh::redact. Вызывается subprocess'ом (sed-паттерны не
    дублируются на второй язык), отказ громкий."""
    result = subprocess.run(
        ["bash", "-c", f'source "{_LIB.parent / "dsh-ci.sh"}"; redact'],
        input=text, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"redact (dsh-ci.sh): {result.stderr.strip()}")
    return result.stdout


# ── Чистая логика: разбор ответа агента ──────────────────────────────────────

def parse_verdict(answer: str) -> str:
    """approve | rework | error. Единственный сигнал — машиночитаемая
    ПОСЛЕДНЯЯ строка; маркера нет, два или не последний — error."""
    lines = [line.strip() for line in (answer or "").splitlines() if line.strip()]
    marks = [m.group(2) for line in lines for m in [VERDICT_RE.match(line)] if m]
    if len(marks) == 1 and lines and VERDICT_RE.match(lines[-1]):
        return marks[0]
    return "error"


def error_reason(answer: str, dsh_rc: str, failure_reason: str = "",
                  reset_hint: str = "") -> str:
    """Причина verdict=error — теперь ЧЕТЫРЕ состояния, не смешиваемые в одно
    (класс silent-wrong прогона 33572445063: ошибка провайдера читалась как
    «модель нарушила контракт»; #419 добавил различение внутри самого
    транспортного отказа — «лимита нет вовсе» от «лимит есть, но сломано
    что-то другое», правило AGENTS.md).

    Классификация (порядок проверки, приоритет между причинами) — ЕДИНСТВЕННО
    в review_labels.reason_tag (находка ревью #439: раньше порядок был
    продублирован здесь второй копией if/elif, связанной с оригиналом только
    комментарием «тот же порядок» — разъехались бы молча, добавь кто-то
    причину в одном месте и забудь про другое). Эта функция только рендерит
    прозу для человека по уже вынесенному тегу; текст для FAILURE_REASON_
    CONTRACT дополнительно различает две подпричины через verdict_line_present
    (тег их не различает — обеим достаточно значения «формат ответа
    нарушен», разница есть только в тексте для человека).

    failure_reason — тег из $AI_WORK/failure_reason.txt (пишет ретрай-цикл
    ai_dsh.sh, #419, либо cmd_gather при пустом диффе, #658/#687), пробрасывается
    step-output'ами ai-review.yml в verdict --failure-reason:
      empty_diff                       — PR не несёт ни одного изменённого
                                          файла (cmd_gather решил это ДО
                                          вызова DSH вовсе, см. cmd_gather),
                                          подтверждено ДВУМЯ независимыми
                                          источниками (список файлов И
                                          `gh pr diff`, #687) — сливать
                                          нечего, повтор на том же head
                                          ничего не изменит.
      diff_source_mismatch             — список файлов PR пуст, а `gh pr
                                          diff` вернул непустой дифф (#687,
                                          находка вердикта ai-review PR
                                          #686): источники разошлись, это НЕ
                                          empty_diff — ревью не пропущено
                                          молча, но и не состоялось;
                                          расхождение может быть
                                          транзиентным (сеть, ограничение
                                          выдачи API) — повтор на этом же
                                          head может дать другой результат.
      quota_exhausted                  — RATE_LIMIT: Weekly/Monthly Limit
                                          Exhausted, сброс через дни — ждать
                                          внутри прогона бессмысленно, ai_dsh.sh
                                          не пытался.
      rate_limit_retry_budget_exceeded — временный RATE_LIMIT не снялся за
                                          отведённый бюджет ожидания.
      all_providers_exhausted          — #727: цепочка провайдеров
                                          (vars.DSH_PROVIDER_CHAIN) перебрана
                                          целиком, ни один не ответил — не
                                          автопереход спас (класс отказа у
                                          КАЖДОГО был переключаемый), а вся
                                          цепочка исчерпана/недоступна разом.
      "" (пусто)                       — старое поведение: либо обычный
                                          транспортный сбой (rc≠0 без
                                          RATE_LIMIT вовсе), либо контракт
                                          ответа (rc=0, формат нарушен).

    reset_hint — «имя: дата; …» опробованных провайдеров с известной датой
    сброса (dsh_extract_reset_hint, lib/dsh-ci.sh) — пусто, если ни один не
    назвал дату. Используется только при all_providers_exhausted — без него
    сообщение честно называет «дата неизвестна», не гадает (AGENTS.md,
    «алерт не гадает»).
    """
    # empty_diff/diff_source_mismatch/all_providers_exhausted (#658/#687/#727)
    # — причины БЕЗ обращения к модели вовсе (cmd_gather решил их заранее) или
    # вне оси quota/rate-limit/transport/contract, которую делит review_labels.
    # reason_tag (#431) — тег их не различает и не обязан: остаются
    # литералами failure_reason здесь, до делегирования тегу ниже.
    if failure_reason == "empty_diff":
        return ("ревью не состоялось — дифф PR пуст (0 изменённых файлов), "
                "сливать нечего: либо содержимое уже попало в main другим "
                "путём (например update-branch подтянул main с идентичным "
                "фиксом, живой случай #658), либо ветка отстала/совпала с "
                "базой — повтор на этом же head ничего не изменит, нужно "
                "либо закрыть PR, либо запушить реальные изменения")
    if failure_reason == "diff_source_mismatch":
        return ("ревью не состоялось — источники диффа PR разошлись: gh "
                "API вернул пустой список изменённых файлов, но `gh pr "
                "diff` (второе независимое подтверждение) вернул непустой "
                "дифф — это НЕ empty_diff, пропускать ревью по одному "
                "пустому списку файлов нельзя (#687). Расхождение может "
                "быть транзиентным (сетевой край, ограничение выдачи "
                "API) — повтор на этом же head (новый пуш или "
                "workflow_dispatch force: true) может дать другой "
                "результат")
    if failure_reason == "all_providers_exhausted":
        when = reset_hint.strip() if reset_hint else "дата неизвестна — ни один провайдер её не назвал"
        return (f"ревью не состоялось — все провайдеры цепочки исчерпаны/недоступны "
                f"(код возврата {dsh_rc}), ближайший сброс: {when} — действие: "
                "ждать сброса вне CI, либо добавить нового провайдера в "
                "vars.DSH_PROVIDER_CHAIN (docs/runbooks/switch-llm-provider.md)")
    # Оставшаяся ось (quota/rate-limit/transport/contract) — ЕДИНСТВЕННО
    # через review_labels.reason_tag (находка ревью #439, см. докстринг выше).
    tag = review_labels.reason_tag(dsh_rc, failure_reason)
    if tag == review_labels.FAILURE_REASON_QUOTA_EXHAUSTED:
        return (f"ревью не состоялось — квота провайдера исчерпана надолго "
                f"(RATE_LIMIT: Weekly/Monthly Limit Exhausted, код возврата "
                f"{dsh_rc}) — повтор внутри этого прогона не поможет, нужно "
                "ждать вне CI или сменить провайдера "
                "(docs/runbooks/switch-llm-provider.md)")
    if tag == review_labels.FAILURE_REASON_RATE_LIMIT_BUDGET:
        return (f"ревью не состоялось — временный RATE_LIMIT провайдера не "
                f"снялся за отведённый бюджет ожидания внутри прогона (код "
                f"возврата {dsh_rc})")
    if tag == review_labels.FAILURE_REASON_TRANSPORT:
        return f"ревью не состоялось — ошибка провайдера/транспорта DSH (код возврата {dsh_rc})"
    # tag == FAILURE_REASON_CONTRACT — единственная причина, которую тег не
    # делит на подпричины; текст для человека делит дальше.
    if verdict_line_present(answer):
        return "модель ответила, но строка «ВЕРДИКТ: …» есть, а не единственная и/или не последняя"
    return "модель ответила, но строки «ВЕРДИКТ: …» нет вообще"


def verdict_line_present(answer: str) -> bool:
    """Есть ли в ответе хоть одна строка, похожая на строку вердикта (в любом
    количестве и с любым обрамлением) — используется только для диагностики:
    различить «модель не написала вердикт вовсе» от «написала, но неоднозначно»
    в сообщении об ошибке. На сам вердикт не влияет — граница контракта не
    меняется, это чисто текст для человека."""
    lines = [line.strip() for line in (answer or "").splitlines() if line.strip()]
    return any(VERDICT_RE.match(line) for line in lines)


# ── Размерный гейт: газ к тормозу review:large (#204) ─────────────────────────

def large_ok_decision(added: int, current_labels, verdict: str) -> str:
    """«ok» — можно автоматически поставить review:large-ok; «escalate» —
    дифф крупнее LARGE_DIFF_HUGE_LINES, решение за владельцем; «skip» —
    ничего не менять (дифф не review:large или AI не одобрил).

    Требует одобренного AI-вердикта на том же head (verdict == "approve"):
    подтверждение размера обязано опираться на состоявшийся разбор диффа,
    а не на факт запуска ревью (условие из #204, п.1) — иначе rework/error
    молча открывал бы газ тормозу, для которого он не предназначен.
    """
    names = review_labels._names(current_labels)
    if check_pr.REVIEW_LARGE not in names:
        return "skip"
    if verdict != "approve":
        return "skip"
    if added > check_pr.LARGE_DIFF_HUGE_LINES:
        return "escalate"
    return "ok"


def huge_diff_escalation_text(pr: int, added: int) -> str:
    """Текст эскалации гигантского диффа. Обязан заканчиваться разделом
    «что дальше» (требование владельца от 2026-09-02, #170) — констатация
    без плана не принимается."""
    return (
        f"🚨 edge-harness: PR #{pr} — дифф +{added} строк превышает второй "
        f"порог review:large-ok ({check_pr.LARGE_DIFF_HUGE_LINES}) — жду решения "
        "владельца по объёму.\n\n"
        "Автоматика AI-ревью одобрила дифф (ai:ok), но не подтверждает размер "
        f"сама: {check_pr.LARGE_DIFF_HUGE_LINES}+ строк — за пределами диапазона, "
        "который проверен на реальных PR этого репозитория (#204).\n\n"
        "Что дальше:\n"
        f"- Исполнитель: владелец репозитория.\n"
        "- Само по себе ничего не произойдёт — PR останется с review:large "
        "без review:large-ok, авто-слияние заблокировано.\n"
        f"- Нужно явное решение: поставить review:large-ok вручную, если объём "
        "оправдан, либо запросить разбивку PR на части."
    )


def _split_scope(body_lines: list[str]) -> tuple[str | None, list[str]]:
    """МАСШТАБ — первая непустая строка тела блока задачи (сразу после
    заголовка). Найден и снят — не попадает в текст тела ни находки, ни
    комментария; не найден — scope=None (см. partition_tasks: отсутствие
    поля не значит «отдельно» молча, #426)."""
    for i, line in enumerate(body_lines):
        stripped = line.strip()
        if not stripped:
            continue
        match = SCOPE_RE.match(stripped)
        if match:
            return match.group(1), body_lines[:i] + body_lines[i + 1:]
        break  # первая непустая строка не МАСШТАБ — поля нет вовсе
    return None, body_lines


def blocked_by_numbers(body: str) -> list[int] | None:
    """Номера issue из ОБЯЗАТЕЛЬНОЙ строки «БЛОКИРУЕТСЯ: …» — ПОСЛЕДНЕЙ
    непустой строки тела задачи (см. `BLOCKED_BY_RE`). `None` — строка
    отсутствует или не по формату (тело от старого/чужого формата, до
    введения этого контракта, #371) — отличается от `[]` («ничем», явный
    ответ «нет зависимостей»): вызывающая сторона решает, что делать с
    отсутствием (parse_tasks отбрасывает блок целиком; file_tasks.py на
    уже созданной issue деградирует — заводит без нативной связи, громко
    предупреждает)."""
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    if not lines:
        return None
    match = BLOCKED_BY_RE.match(lines[-1])
    if not match:
        return None
    if match.group(1) == "ничем":
        return []
    return [int(n) for n in re.findall(r"#(\d+)", match.group(1))]


def parse_tasks(answer: str) -> list[dict]:
    """Блоки ЗАДАЧА: … КОНЕЦ ЗАДАЧИ из ответа. Незакрытый/пустой блок, как и
    блок БЕЗ обязательной строки «БЛОКИРУЕТСЯ: …» последней (#371) —
    отбрасывается целиком: полузадача в пуле хуже отсутствия задачи. Отброс
    по недостающей зависимости — громкий (`::warning::` в лог ревью), не
    тихий: иначе несоблюдение моделью нового формата промпта осталось бы
    незамеченным, а беклог просто получал бы меньше задач без объяснения."""
    tasks: list[dict] = []
    title: str | None = None
    body: list[str] = []
    for line in (answer or "").splitlines():
        stripped = line.strip()
        if title is None:
            match = TASK_OPEN_RE.match(stripped)
            if match:
                title = match.group(1).strip()
                body = []
        elif stripped == TASK_CLOSE:
            scope, rest = _split_scope(body)
            full_body = "\n".join(rest).strip()
            if blocked_by_numbers(full_body) is None:
                print(
                    f"::warning::AI-ревью: задача «{title}» без обязательной "
                    f"последней строки «БЛОКИРУЕТСЯ: #N … | ничем» — блок "
                    f"отброшен, в беклог не идёт (#371)"
                )
            else:
                tasks.append({"title": title, "body": full_body, "scope": scope})
            title, body = None, []
        else:
            body.append(line.rstrip())
    return tasks


def partition_tasks(tasks: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Судьба находки по МАСШТАБ (#426): `отдельно` — в беклог (issue заведёт
    file_tasks.py); `хвост` — дописать в этом же PR, issue не заводим;
    отсутствие поля — НЕ трактуется молча как «отдельно» (fail loud): видимо
    в комментарии отдельным разделом, но не заводится и не считается хвостом
    без явного решения ревьюера."""
    backlog = [t for t in tasks if t.get("scope") == SCOPE_SEPARATE]
    tail = [t for t in tasks if t.get("scope") == SCOPE_TAIL]
    unscoped = [t for t in tasks if t.get("scope") not in (SCOPE_SEPARATE, SCOPE_TAIL)]
    return backlog, tail, unscoped


def findings_of(answer: str, tasks: list[dict] | None = None,
                 remarks: list[dict] | None = None) -> str:
    """Проза ответа без строк вердикта, блоков задач И блоков замечаний
    (#462): маркер вердикта отражается в reviewer:, задачи переезжают в
    канонические фенсы, замечания — в чеклист тела PR (review_checklist).
    Ничего из трёх категорий не должно задваиваться в свободной прозе
    комментария."""
    tasks = tasks if tasks is not None else parse_tasks(answer)
    remarks = remarks if remarks is not None else review_checklist.parse_remarks(answer)
    task_titles = {t["title"] for t in tasks}
    remark_titles = {r["title"] for r in remarks}
    lines: list[str] = []
    in_task = False
    in_remark = False
    for line in (answer or "").splitlines():
        stripped = line.strip()
        if VERDICT_RE.match(stripped):
            continue
        if in_task:
            if stripped == TASK_CLOSE:
                in_task = False
            continue
        if in_remark:
            if stripped == review_checklist.REMARK_CLOSE:
                in_remark = False
            continue
        match = TASK_OPEN_RE.match(stripped)
        if match and match.group(1).strip() in task_titles:
            in_task = True
            continue
        rmatch = review_checklist.REMARK_OPEN_RE.match(stripped)
        if rmatch and rmatch.group(1).strip() in remark_titles:
            in_remark = True
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip("\n").strip()


def build_comment(number: int, sha: str, verdict: str, findings: str,
                  tasks: list[dict], diff_fp: str | None = None,
                  remarks: list[dict] | None = None,
                  chain_provider: str | None = None,
                  reset_hint: str | None = None,
                  reason_tag_value: str | None = None) -> str:
    """Канонический комментарий-вердикт. Шапка-факты — САМЫЕ ПЕРВЫЕ строки,
    до первого пустой строки (инвариант: file_tasks.py парсит ТОЛЬКО эту
    зону и фенсы задач, проза и заборы не могут притвориться фактами).

    diff_fp — отпечаток диффа PR на момент вердикта (review_labels.
    diff_fingerprint, #252): check_pr.py читает его из поля `diff:` шапки,
    чтобы решить, сохранять ли ai:*-метку при следующем пуше. Необязателен
    (None не добавляет строку) — не ломает старые вызовы/тесты, которые
    факта diff не ждут.

    tasks делится по МАСШТАБ (#426, partition_tasks): в фенсы (то, что
    file_tasks.py читает и заводит issue'ами) попадают ТОЛЬКО `отдельно`.
    `хвост` уходит прозой в раздел «доделай в этом PR» — содержимое не
    теряется, issue не заводится. Без поля — тоже прозой, но с явным
    предупреждением: отсутствие МАСШТАБ не трактуется молча как «отдельно»
    (fail loud) — граница проведена здесь, а не в file_tasks.py, ОДНИМ
    местом правды: file_tasks.py читает только фенсы, значит незафенсенное
    физически не может быть заведено issue.

    remarks — блоки ЗАМЕЧАНИЕ (#462, третья категория находок): сам чеклист
    живёт в ТЕЛЕ PR (review_checklist.merge_checklist, отдельный PATCH), не
    здесь — комментарий только указывает, что чеклист обновлён, чтобы автор
    не искал замечания в прозе комментария, которую отсюда убрал findings_of.

    chain_provider/reset_hint (#727) — факты цепочки провайдеров: имя
    провайдера, фактически ответившего (видимость «кто обслужил ход», не
    только «какой провайдер настроен в vars»), и даты сброса опробованных —
    читает scheduler.py::trigger_ai_review (header_facts), чтобы не жечь
    авто-повтор (#196) вслепую в ту же квоту. Обе строки опциональны (пусто —
    не добавлены вовсе), как diff_line выше.

    reason_tag_value — тег причины verdict=error (review_labels.reason_tag,
    #431): факт `reason:` в шапке, который scheduler.trigger_ai_review читает
    для решения о бюджете автоповторов ЭТОЙ эпохи. None (verdict != "error"
    или вызов без классификации) не добавляет строку — та же обратная
    совместимость, что у diff_fp."""
    diff_line = f"diff: {diff_fp}\n" if diff_fp else ""
    provider_line = f"provider: {chain_provider}\n" if chain_provider else ""
    reset_line = f"reset-at: {reset_hint}\n" if reset_hint else ""
    reason_line = f"reason: {reason_tag_value}\n" if reason_tag_value else ""
    head = (
        f"pr: {number}\nhead: {sha}\nreviewer: {verdict}\n"
        f"{diff_line}{provider_line}{reset_line}{reason_line}\n"
        f"🤖 AI-ревью — второй гейт конвейера (#18). Вердикт: {verdict}."
    )
    backlog, tail, unscoped = partition_tasks(tasks)
    body = findings.strip()
    if tail:
        tail_text = "\n\n".join(f"- **{t['title']}**\n  {t['body']}" for t in tail)
        body += (
            "\n\n### Доделай в этом PR (масштаб «хвост» — issue не заводится)\n\n"
            f"{tail_text}"
        )
    if unscoped:
        unscoped_text = "\n\n".join(f"- **{t['title']}**\n  {t['body']}" for t in unscoped)
        body += (
            "\n\n### ⚠️ Без объявленного МАСШТАБА — не заведено автоматически\n"
            "Ревьюер не указал МАСШТАБ (хвост/отдельно) у находки ниже — контракт "
            "не угадывает поле молча (fail loud, #426). Заведи issue вручную, если "
            "это реально отдельная работа, либо допиши прямо здесь, если это хвост.\n\n"
            f"{unscoped_text}"
        )
    if remarks:
        titles = "\n".join(f"- {r['title']}" for r in remarks)
        body += (
            "\n\nНекритичные замечания (не блокируют мерж) — в чеклисте тела PR:\n"
            f"{titles}"
        )
    if backlog:
        close = "`" * len(TASK_FENCE[: TASK_FENCE.index("з")])  # ровно столько же бэктиков, сколько в открывающем
        blocks = "\n\n".join(
            f"{TASK_FENCE}\n{t['title']}\nМАСШТАБ: {SCOPE_SEPARATE}\n{t['body']}\n{close}"
            for t in backlog
        )
        body += (
            f"\n\nЗадачи в беклог из этого ревью — завести одной командой:\n"
            f"    python scripts/review/file_tasks.py --pr {number}\n\n{blocks}"
        )
    return f"{head}\n\n{body}\n".strip() + "\n"


def tasks_from_comment(comment_body: str) -> list[dict]:
    """Канонические фенсы задач из комментария ревью (не из сырого ответа):
    комментарий — долговременное место правды для file_tasks.py. Закрывается
    строкой из ≥4 бэктиков: внутренний ```-фенс остаётся телом задачи."""
    tasks: list[dict] = []
    lines = (comment_body or "").splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() == TASK_FENCE:
            block: list[str] = []
            i += 1
            while i < len(lines) and not FENCE_CLOSE_RE.match(lines[i].strip()):
                block.append(lines[i].rstrip())
                i += 1
            if block and i < len(lines):  # забор закрыт
                scope, rest = _split_scope(block[1:])
                tasks.append({
                    "title": block[0].strip(),
                    "body": "\n".join(rest).strip(),
                    "scope": scope,
                })
        i += 1
    return [t for t in tasks if t["title"]]


# ── gather: факты, дифф-пак, промпт ──────────────────────────────────────────

def is_not_found(error: RuntimeError) -> bool:
    """«Запрошенной issue нет» — по ТОЧНОЙ форме gh «Not Found (HTTP 404)»,
    не по подстроке «404»: она ловит и URL (issues/404), из-за чего отказ
    сети/права по задаче с «404» в номере молчно считался бы «не задача»."""
    return "HTTP 404" in str(error)


NO_TASK_MESSAGE = (
    "Задача из пула: у PR нет открытой задачи с меткой task (orchestra:skip или "
    "сопровождение) — ревьюй по документации репозитория и здравому смыслу."
)


def task_section(pull: dict, repo: str) -> str:
    """Задача пула, которую закрывает PR — резолвится ОДНИМ источником
    правды `task_ref.resolve_pr_task` (#259, #394): единственный источник —
    имя agent-ветки, тело PR не читается вовсе (решение владельца
    2026-09-06). Любое упоминание номера в прозе
    (`task_ref.extract_task_refs`) сюда не годится — этим классом бага
    ai_review.py путал задачу PR с первым попавшимся числом в описании
    (живой замер #259: #253 судили по #120 из прозы вместо объявленного
    #227, #248 — по #119 вместо #201, #247 — по #43 вместо своей задачи,
    #263 — по #4 вместо #255).

    Нет задачи (orchestra:skip, dependabot, ручной PR без декларации) — так
    и пишем: «нет задачи». Резолвнутый номер не читается (404) — тоже
    «нет задачи»: сослаться на несуществующую issue равносильно её
    отсутствию. Любой другой отказ (права, сеть, 5xx) роняет шаг громко —
    молча ревьюить без контекста задачи нельзя (silent-wrong).
    """
    number = task_ref.resolve_pr_task(pull)
    if number is None:
        return NO_TASK_MESSAGE
    try:
        issue = gh(f"repos/{repo}/issues/{number}")
    except RuntimeError as error:
        if is_not_found(error):
            return NO_TASK_MESSAGE
        raise
    if "pull_request" in issue or issue.get("state") != "open":
        return NO_TASK_MESSAGE
    if "task" not in {label["name"] for label in issue["labels"]}:
        return NO_TASK_MESSAGE
    return (
        f"Задача из пула, которую закрывает этот PR: #{number} «{issue['title']}»\n\n"
        f"{issue.get('body') or ''}"
    )


def cmd_gather(args: argparse.Namespace) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    pull = gh(f"repos/{repo}/pulls/{args.pr}")
    # Постранично (#294): review_labels.list_pr_files — то же место правды,
    # что и у check_pr.py; первая страница у PR за сотню файлов молча теряла
    # хвост (недосчёт added, невидимая правка для diff_fingerprint в verdict).
    files = review_labels.list_pr_files(repo, args.pr, gh)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if not files:
        # Пустой список файлов САМ ПО СЕБЕ — недостаточное основание объявить
        # «дифф пуст» (#687, находка вердикта ai-review PR #686): список — тот
        # же API-вызов, что молча терял хвосты пагинацией до #294, и пустой
        # ответ при фактически непустом диффе (сетевой край, транзиентное
        # состояние, обрезанная выдача) раньше приводил к тому же исходу, что
        # настоящий пустой дифф — дорогой прогон DSH пропускался БЕЗ второго
        # источника. Асимметрия: на обычном пути (files непуст, ниже) второго
        # запроса не появилось — только здесь, в редкой ветке, где мы
        # собираемся отказаться от ревью, второе независимое подтверждение
        # обязательно.
        diff_run = pr_diff(args.pr)
        if diff_run.returncode == 0 and diff_run.stdout.strip():
            # Источники разошлись: список файлов пуст, а фактический дифф —
            # нет. Это НЕ empty_diff (там дифф пуст по ОБОИМ источникам) и НЕ
            # тихое продолжение с доверием пустому списку (иначе ревью молча
            # пропустилось бы на реальном изменении — направление отказа,
            # которого этот гейт не имеет права допускать). Выбор между
            # терминальным вердиктом и жёстким отказом шага (RuntimeError):
            # терминальный вердикт — тот же принцип, что уже закрыл #658 для
            # empty_diff — красный шаг ДО job'а verdict оставляет PR без
            # единой ai:*-метки НАВСЕГДА (job verdict не запускается вовсе),
            # а терминальный ai:failed с ясной причиной хотя бы виден и
            # автоповтор по таймеру (#196) даёт второй шанс на случай, если
            # расхождение было транзиентным. Оба факта — в лог (для
            # диагностирующего) и в причину вердикта (для владельца, см.
            # error_reason) — тихо мимо этого расхождения не проходим.
            (out / "failure_reason.txt").write_text("diff_source_mismatch", encoding="utf-8")
            (out / "answer.txt").write_text("", encoding="utf-8")
            print(f"::error::gather: PR #{args.pr} источники диффа разошлись — "
                  f"список файлов PR пуст (0), но `gh pr diff` вернул непустой "
                  f"дифф ({len(diff_run.stdout)} байт) — ревью НЕ пропущено как "
                  "empty_diff, вердикт уйдёт как ai:failed с причиной "
                  "diff_source_mismatch")
            return 0
        if diff_run.returncode != 0:
            # Не удалось даже подтвердить/опровергнуть: gh pr diff сам упал
            # при пустом списке файлов. Тот же класс, что и симметричный
            # отказ ниже (files непуст, diff упал) — молчать нельзя, а
            # разница между «дифф пуст» и «сеть легла» здесь принципиальна:
            # первое терминально (повтор бессмыслен), второе — транспортный
            # отказ (повтор может помочь). Раз не смогли различить —
            # RuntimeError, тем же путём, что и ниже.
            raise RuntimeError(
                f"gh pr diff {args.pr}: rc={diff_run.returncode} при пустом "
                f"списке файлов — не удалось подтвердить, что дифф "
                f"действительно пуст: {diff_run.stderr.strip()[:200]}")
        # diff_run.returncode == 0 и stdout пуст — оба источника согласны:
        # дифф действительно пуст (живой факт #658, 2026-09-07: ветка forge/*
        # слилась с main через update-branch ровно в момент, когда идентичный
        # фикс уже попал в main другим PR — содержимое схлопнулось в ничто).
        # Терминальный исход — тот же файловый контракт, что уже использует
        # ai_dsh.sh при транспортном отказе (failure_reason.txt/answer.txt,
        # см. error_reason): job review остаётся зелёным, шаг «Ревью агентом
        # (DSH)» ai-review.yml пропускается по этому же маркеру
        # (steps.gather.outputs.empty_diff), a job verdict читает
        # failure_reason=empty_diff и ставит ai:failed с ясной причиной —
        # той же дорогой, которой уже идёт любой другой transport-отказ, без
        # второй копии логики.
        (out / "failure_reason.txt").write_text("empty_diff", encoding="utf-8")
        (out / "answer.txt").write_text("", encoding="utf-8")
        print(f"::warning::gather: PR #{args.pr} дифф пуст (0 изменённых файлов, "
              "подтверждено вторым источником `gh pr diff`) — сливать нечего, "
              "дорогой прогон DSH пропущен, вердикт уйдёт как ai:failed с "
              "причиной empty_diff")
        return 0
    diff_run = pr_diff(args.pr)
    if diff_run.returncode != 0 or not diff_run.stdout.strip():
        # files непуст, а `gh pr diff` пуст/упал — это уже не «дифф пуст по
        # факту», а отказ самого вызова (сеть/права): молчать тут нельзя.
        raise RuntimeError(f"gh pr diff {args.pr}: rc={diff_run.returncode}, "
                           f"diff пуст при непустом списке файлов: {diff_run.stderr.strip()[:200]}")
    diff = diff_run.stdout
    added = sum(f["additions"] for f in files)
    listing = "\n".join(f"{f['filename']} (+{f['additions']}/-{f['deletions']})" for f in files)

    pack = out / "pack.txt"
    pack.write_text(f"FILES:\n{listing}\n\nADDITIONS: {added}\n\nDIFF:\n{diff}", encoding="utf-8")

    template = string.Template((SCRIPT_DIR / "ai_prompt.md").read_text(encoding="utf-8"))
    prompt = template.safe_substitute(
        pr=args.pr,
        title=pull.get("title", ""),
        branch=pull["head"]["ref"],
        author=(pull.get("user") or {}).get("login", ""),
        context_pack=pack,
        task_section=task_section(pull, repo),
    )
    (out / "prompt.md").write_text(prompt, encoding="utf-8")
    # Переходная совместимость: bridge на main (до мержа этого PR) берёт head
    # для сверки из meta.json; НОВЫЙ bridge берёт step-output фактов, а файл
    # остаётся диагностическим артефактом gather. Удалить вместе со старым
    # bridge после мержа (задача в беклоге).
    (out / "meta.json").write_text(
        json.dumps({"pr": args.pr, "head": pull["head"]["sha"]}), encoding="utf-8")
    print(f"gather: PR #{args.pr} head {pull['head']['sha'][:12]}, "
          f"+{added} строк, промпт {len(prompt)} байт, пак {pack}")
    return 0


# ── should-run: нужен ли дорогой прогон вообще (#294) ────────────────────────
#
# Вызывается из шага «trusted facts» самого ai-review.yml ДО чекаута
# pr-head/gather/DSH: PR уже прошёл проверку `review:ok` (её делает bash-код
# facts-шага), эта команда решает, оправдан ли дорогой вызов модели, ту же
# функцию, что читает check_pr.py для решения «сохранить ли метку»
# (review_labels.should_run_ai_review — одно место правды, а не вторая копия
# условия в YAML).
#
# Утечка денег владельца (#399, аудит 197 платных прогонов за 2026-09-05/06):
# 44 из них (22%) — ручные workflow_dispatch, дублирующие уже идущий или уже
# завершённый прогон на том же PR, без ai:failed. До этой правки
# ai-review.yml выставлял --force БЕЗУСЛОВНО для любого workflow_dispatch
# (см. Дельта 2026-09-05 в docs/decisions/0007-ai-review-gate.md) — сверка
# отпечатка не выполнялась вовсе, повод «владелец хочет пересмотра» не
# отличался от «оркестратор нажал Run workflow по инерции».
#
# --force остаётся (владелец, не согласный с окончательным вердиктом на
# неизменном диффе, теряет иначе единственный газ пересмотра — находка 1
# вердикта ai-review PR #294), но теперь это ОТДЕЛЬНЫЙ, осознанный вход:
# ai-review.yml просит --force, только если ручной запуск явно нёс
# input force: true (default false) — не любой workflow_dispatch. Без него
# ручной запуск проходит ТУ ЖЕ сверку, что и автоматический
# (review_labels.should_run_ai_review), плюс гонку «прогон уже идёт прямо
# сейчас» (review_labels.other_active_ai_review_runs) — теперь для ОБОИХ
# путей триггера (#779, разрыв 1). Раньше эту гонку спрашивал только ручной
# путь в предположении «автоматический прогон сериализован своей
# concurrency-группой» — предположение оказалось неверным: группа событийного
# пути — id породившего рана pr-review (github.event.workflow_run.id),
# уникальный на КАЖДОЕ событие, значит два разных pr-review-события одного и
# того же PR попадают каждое в свою группу и НЕ сериализуются вовсе (замер
# issue #779: 20 событийных прогонов пересеклись по времени с другим
# событийным прогоном того же PR за одни сутки). Правка ключа группы —
# отдельный вопрос (issue #779 целиком, критерии 1-2: выбор между отменой и
# очередью), эта правка про число прогонов, а не про их упорядочение.

def manual_dispatch_busy_reason(pr: int, other_run: dict) -> str:
    """Текст отказа: на PR #pr уже идёт другой прогон ai-review.yml прямо
    сейчас — второй одновременно бессмыслен, а не просто дорог."""
    url = other_run.get("html_url")
    where = f" ({url})" if url else ""
    return (
        f"::warning::ручной прогон PR #{pr} отклонён: уже идёт другой прогон "
        f"ai-review.yml (run {other_run.get('id')}, статус "
        f"{other_run.get('status', '?')}){where} — второй прогон параллельно "
        "ничего не даст. Дождитесь его завершения; принудительно запустить "
        "поверх летящего прогона нельзя даже через force: true."
    )


def event_dispatch_duplicate_reason(pr: int, other_run: dict) -> str:
    """Текст короткого замыкания событийного прогона (workflow_run от
    pr-review) — не «отказ» владельцу (событийный путь никто не просил
    руками), а факт в лог: этот прогон — ДУБЛЬ уже летящего прогона того же
    PR, а не расхождение по содержимому диффа. Причина названа отдельным
    текстом от manual_dispatch_busy_reason (адресат разный: там — предупреждение
    тому, кто дёрнул workflow_dispatch, здесь — только след в логе job'а) и
    отдельным от manual_dispatch_skip_reason (та — «дифф не изменился»,
    другая причина того же go=false; AGENTS.md «алерт не гадает» — читатель
    обязан отличить «дубль» от «код уже видели»).

    Разрыв 1 (#779): до этой правки событийный путь (`workflow_run` от
    pr-review) вообще не спрашивал `other_active_ai_review_runs` — только
    workflow_dispatch. 68 из 134 прогонов ai-review.yml за сутки (замер
    2026-09-08) шли именно этим путём и ни один дубль на нём не отклонялся.
    Событийный прогон, в отличие от ручного, уже СОЗДАН GitHub'ом — отменить
    его нельзя, можно только замкнуть коротко без вызова модели, ровно так
    же, как cmd_should_run уже делает для ручного пути."""
    url = other_run.get("html_url")
    where = f" ({url})" if url else ""
    return (
        f"::notice::прогон PR #{pr} останавливается без вызова модели: другой "
        f"прогон ai-review.yml для этого же PR уже идёт (run {other_run.get('id')}, "
        f"статус {other_run.get('status', '?')}){where} — это дубль, а не "
        "расхождение по содержимому диффа. Вердикт вынесет прогон, который уже летит."
    )


def _verdict_label_and_age(current_labels, ai_comment: dict | None) -> tuple[str, str]:
    """Общая часть текста «дифф не изменился» для обоих путей триггера —
    вердикт и его возраст, без адресата (тот разный у manual/event, см.
    manual_dispatch_skip_reason и event_dispatch_skip_reason)."""
    names = review_labels._names(current_labels)
    verdict_label = next(
        (label for label in (review_labels.AI_OK, review_labels.AI_CHANGES) if label in names),
        "неизвестный вердикт")
    age = "неизвестно когда"
    created_at = (ai_comment or {}).get("created_at")
    # review_labels.parse_github_timestamp — единственное место разбора этой
    # метки (#780, доводка #779): возвращает None на битую ИЛИ наивную (без
    # "Z"/"+HH:MM") строку, не бросая ValueError/TypeError наружу — раньше
    # этот except ловил только ValueError, и наивная строка падала на
    # `now - created` (aware - naive) необработанной.
    created = review_labels.parse_github_timestamp(created_at)
    if created is not None:
        minutes = max(0, int((datetime.now(timezone.utc) - created).total_seconds() // 60))
        age = f"{minutes} мин назад"
    return verdict_label, age


def manual_dispatch_skip_reason(pr: int, current_labels, ai_comment: dict | None) -> str:
    """Текст отказа: на PR #pr уже стоит окончательный вердикт на этом же
    диффе — повторный дорогой прогон денег не оправдывает. Называет вердикт,
    сколько минут назад он вынесен, и что делать вместо ручного повтора
    (правило репозитория: отказ без «что дальше» не принимается)."""
    verdict_label, age = _verdict_label_and_age(current_labels, ai_comment)
    return (
        f"::notice::ручной прогон PR #{pr} отклонён: дифф не изменился с "
        f"последнего вердикта {verdict_label} ({age}) — второй прогон на том "
        "же коде ничего нового не покажет. Автоматический повтор придёт сам, "
        "если дифф изменится; принудительный пересмотр ровно этого же "
        "диффа — запуск с явным входом force: true."
    )


def event_dispatch_skip_reason(pr: int, current_labels, ai_comment: dict | None) -> str:
    """Текст события-пути (workflow_run от pr-review) на том же go=false, что
    manual_dispatch_skip_reason, — «дифф не изменился с последнего вердикта».
    Отдельная функция, не переиспользование manual_dispatch_skip_reason:
    адресат другой (лог job'а, никто руками этот прогон не дёргал) и текст не
    должен звать прогон «ручным», раз он им не является (#779, разрыв 2: до
    этой правки печать была заперта условием `and manual` — событийный путь,
    самый частый (68 из 134 прогонов, замер 2026-09-08), на этой ветке молчал
    в stderr вовсе, хотя шаг ai-review.yml утверждает читателю, что точная
    причина уже напечатана строкой выше)."""
    verdict_label, age = _verdict_label_and_age(current_labels, ai_comment)
    return (
        f"::notice::прогон PR #{pr} останавливается без вызова модели: дифф "
        f"не изменился с последнего вердикта {verdict_label} ({age}) — "
        "повторный вызов модели на том же коде ничего нового не покажет. "
        "Автоматический повтор придёт сам, если дифф изменится."
    )


def cmd_should_run(args: argparse.Namespace) -> int:
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    # Гонка «прогон уже идёт прямо сейчас» — проверяется ПЕРВОЙ, ДО --force
    # и ДО сверки отпечатка, для ОБОИХ путей триггера (#779, разрыв 1: до
    # этой правки предикат спрашивался только при workflow_dispatch —
    # событийный путь, workflow_run от pr-review, 68 из 134 прогонов за
    # сутки в замере 2026-09-08, не спрашивал его никогда). Один и тот же
    # предикат (review_labels.other_active_ai_review_runs), что уже читают
    # scheduler.update_branch и scheduler.trigger_ai_review — второй копии
    # не заводим.
    #
    # Разница исходов между путями — не только текст лога: ручной путь ещё
    # НЕ создал прогон, отказ здесь означает «не тратить деньги вовсе»
    # (manual_dispatch_busy_reason). Событийный прогон GitHub уже СОЗДАЛ —
    # отменить его нельзя, только замкнуть коротко без вызова модели
    # (event_dispatch_duplicate_reason) — тот же газ, что и у ручного пути,
    # но другая причина отказа: «дубль» (этот раздел), не «дифф не
    # изменился» (manual_dispatch_skip_reason ниже, другая причина того же
    # go=false — читатель обязан их различать, AGENTS.md «алерт не гадает»).
    #
    # Разрыв 4 (#779): для ai:failed should_run_ai_review ниже возвращает
    # True БЕЗУСЛОВНО (газ автоповтора #196 не должен зависеть от того,
    # менялся ли дифф) — именно поэтому активность прогона обязана
    # проверяться РАНЬШЕ и НЕЗАВИСИМО от фингерпринта, а не встраиваться в
    # саму функцию отпечатка (`should_run_ai_review` не принимает и не
    # обязана принимать «летит ли прогон» — второй предикат внутри неё был
    # бы третьей копией). С этой проверкой выше по стеку эффективное правило
    # для ai:failed становится «нужен повтор, если прогона сейчас не летит»
    # — один повтор на живой отпечаток, не три подряд (живой замер PR #711
    # в issue #779): следующий повтор возможен, только когда текущий
    # действительно завершился (успехом, ошибкой или отменой).
    repo = os.environ["GITHUB_REPOSITORY"]
    active = review_labels.other_active_ai_review_runs(
        repo, args.pr, os.environ.get("GITHUB_RUN_ID", ""), gh)
    if active:
        if manual:
            print(manual_dispatch_busy_reason(args.pr, active[0]), file=sys.stderr)
        else:
            print(event_dispatch_duplicate_reason(args.pr, active[0]), file=sys.stderr)
        print("false")
        return 0
    if getattr(args, "force", False):
        # Осознанный ручной повтор (не занят — проверено выше) не заходит в
        # сеть дальше: решение не зависит от отпечатка диффа, а необращение к
        # gh здесь же и доказывает мутацией
        # (test_cmd_should_run_force_skips_fingerprint_check_no_network_call).
        print("true")
        return 0
    pull = gh(f"repos/{repo}/pulls/{args.pr}")
    current_labels = {label["name"] for label in pull["labels"]}
    files = review_labels.list_pr_files(repo, args.pr, gh)
    current_fp = review_labels.diff_fingerprint(files)
    ai_comment = review_labels.latest_ai_comment(repo, args.pr, gh)
    stored_fp = (review_labels.header_facts(ai_comment.get("body") or "").get("diff")
                 if ai_comment else None)
    run_needed = review_labels.should_run_ai_review(current_labels, stored_fp, current_fp)
    if not run_needed:
        # Разрыв 2 (#779): печать была заперта условием `and manual` —
        # событийный путь (самый частый, #779 разрыв 1) молчал в stderr, хотя
        # шаг ai-review.yml утверждает читателю, что причина уже напечатана
        # строкой выше. Оба пути обязаны печатать СВОЙ текст — читатель
        # различает «ручной» от «событийный», не гадает (AGENTS.md).
        if manual:
            print(manual_dispatch_skip_reason(args.pr, current_labels, ai_comment), file=sys.stderr)
        else:
            print(event_dispatch_skip_reason(args.pr, current_labels, ai_comment), file=sys.stderr)
    # Единственная строка на stdout — bash-шаг ai-review.yml читает её как
    # $(...), никакого другого вывода в этой команде быть не должно.
    print("true" if run_needed else "false")
    return 0


# ── verdict: разбор ответа, комментарий, метка ────────────────────────────────

def apply_large_ok(repo: str, pr: int, added: int, current_labels, verdict: str) -> None:
    """Проводка чистого large_ok_decision: ставит review:large-ok сама, либо
    эскалирует владельцу по каналу pulse_guard.escalate (#204, п.3). Молчит
    на «skip» — дифф не review:large или AI не одобрил, ничего не меняется."""
    decision = large_ok_decision(added, current_labels, verdict)
    if decision == "skip":
        return
    if decision == "ok":
        run_gh("api", "-X", "POST", f"repos/{repo}/issues/{pr}/labels",
               "-f", f"labels[]={review_labels.LARGE_OK}")
        print(f"large-ok: +{added} строк ≤ {check_pr.LARGE_DIFF_HUGE_LINES} — "
              f"{review_labels.LARGE_OK} поставлена автоматически")
        return
    text = huge_diff_escalation_text(pr, added)
    result = pulse_guard.escalate(repo, pulse_guard.WATCHDOG_ISSUE, text)
    print(f"::warning::large-ok: +{added} строк > {check_pr.LARGE_DIFF_HUGE_LINES} — "
          f"эскалация владельцу ({result})")


def notify_head_moved(repo: str, pr: int, verdict: str, old_head: str, new_head: str) -> None:
    """Комментарий в PR — единственный видимый след того, что вердикт `verdict`
    НЕ применён из-за смены head (см. вызовы в cmd_verdict). Job остаётся
    зелёным (гонка — не ошибка кода, см. AGENTS.md), но без этого следа
    единственная улика — ::warning:: в логе шага, который никто не открывает
    без повода.

    Дедуп по marker+shas (#488): листает уже опубликованные комментарии PR и
    молчит, если переход ИМЕННО old_head→new_head уже отмечен — повторный
    вызов на тот же переход (retry job'а verdict, не новый пуш) не плодит
    вторую копию того же следа."""
    marker = f"{HEAD_MOVED_MARKER_PREFIX}{old_head}:{new_head} -->"
    for comment in review_labels.list_pages(f"repos/{repo}/issues/{pr}/comments?per_page=100", gh):
        if marker in (comment.get("body") or ""):
            return
    body = (
        f"{marker}\n"
        f"⏭️ Вердикт `{verdict}` не применён: head PR сменился "
        f"`{old_head[:12]}` → `{new_head[:12]}` во время ревью — по новому "
        "head поднимется новое ревью."
    )
    run_gh("api", "-X", "POST", f"repos/{repo}/issues/{pr}/comments", "-f", f"body={body}")


def cmd_verdict(args: argparse.Namespace) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    answer = Path(args.answer).read_text(encoding="utf-8") if Path(args.answer).exists() else ""
    verdict = parse_verdict(answer)
    tasks = parse_tasks(answer)
    remarks = review_checklist.parse_remarks(answer)
    findings = redact(findings_of(answer, tasks, remarks))
    tasks = [{"title": redact(t["title"]).strip(), "body": redact(t["body"]).strip(),
              "scope": t.get("scope")}
             for t in tasks]
    tasks = [t for t in tasks if t["title"]]
    remarks = [{"title": redact(r["title"]).strip(), "body": redact(r["body"]).strip()}
               for r in remarks]
    remarks = [r for r in remarks if r["title"]]

    # Причина «error» — вычисляется ДО комментария: четыре разных состояния
    # не смешиваются ни в логе, ни в тексте для человека (silent-wrong класс:
    # ошибка провайдера не должна выглядеть как «модель ответила криво», а
    # временный RATE_LIMIT — как настоящая поломка, #419).
    reason = (error_reason(answer, args.dsh_rc, args.failure_reason, args.reset_hint)
              if verdict == "error" else None)
    if reason and not findings.strip():
        findings = reason
    # Тег для шапки комментария (#431) — независимо от findings/reason (та
    # проза может оказаться текстом самой модели, а не error_reason, см.
    # review_labels.FAILURE_REASON_* докстринг): scheduler.trigger_ai_review
    # решает бюджет автоповторов по структурному факту `reason:`, не по
    # пересказу.
    reason_tag_value = review_labels.reason_tag(args.dsh_rc, args.failure_reason) if verdict == "error" else None

    pull = gh(f"repos/{repo}/pulls/{args.pr}")
    if pull["head"]["sha"] != args.head:
        print(f"::warning::head PR #{args.pr} сменился ({args.head[:12]} → "
              f"{pull['head']['sha'][:12]}) — вердикт {verdict} не применяю: "
              f"новый пуш заведёт свежее ревью")
        notify_head_moved(repo, args.pr, verdict, args.head, pull["head"]["sha"])
        return 0

    # Файлы читаются ДО применения вердикта (находка 1 вердикта ai-review
    # PR #294, гонка): list_pr_files — отдельный сетевой вызов (возможно,
    # несколько страниц), и между проверкой головы выше и этим моментом
    # проходит время, в которое автор успевает запушить новый коммит. Раньше
    # это не было опасно — протухший ai:ok безусловно умирал на следующем
    # check_pr.py; но #252 научил его переживать неизменный дифф, и та же
    # гонка делает его вечным: комментарий уйдёт с `diff:` от НЕревьюенных
    # файлов, ai_verdict_keep будет подтверждать его при каждом пуше. Поэтому
    # СРАЗУ после чтения файлов голова сверяется ЕЩЁ РАЗ, и до единой строчки
    # правки (метка/большой-ok/комментарий) — если она уехала от args.head,
    # выходим без применения вердикта вовсе.
    files = review_labels.list_pr_files(repo, args.pr, gh)
    pull_after_files = gh(f"repos/{repo}/pulls/{args.pr}")
    if pull_after_files["head"]["sha"] != args.head:
        print(f"::warning::head PR #{args.pr} сменился во время чтения файлов "
              f"({args.head[:12]} → {pull_after_files['head']['sha'][:12]}) — "
              f"вердикт {verdict} не применяю: новый пуш заведёт свежее ревью")
        notify_head_moved(repo, args.pr, verdict, args.head, pull_after_files["head"]["sha"])
        return 0

    current = {label["name"] for label in pull["labels"]}
    label = AI_OK if verdict == "approve" else (AI_CHANGES if verdict == "rework" else AI_FAILED)
    # Тот же класс идемпотентности, что вердикт-метка review:* в check_pr.py
    # (#203): повторный вердикт ТОГО ЖЕ значения (автоповтор ai:failed по
    # таймеру #196, повторный approve после подтягивания main) не выполняет
    # ни одного изменяющего вызова — ни лишних unlabeled/labeled в таймлайне;
    # смена вердикта переставляет метку как раньше (решение —
    # verdict_label_changes, одно место правды с check_pr.py).
    stale_verdicts, need_verdict_post = review_labels.verdict_label_changes(
        current, label, AI_VERDICTS)
    for old in stale_verdicts:
        run_gh("api", "-X", "DELETE", f"repos/{repo}/issues/{args.pr}/labels/{old}")
    if need_verdict_post:
        run_gh("api", "-X", "POST", f"repos/{repo}/issues/{args.pr}/labels",
               "-f", f"labels[]={label}")
    # Множество меток ПОСЛЕ свопа — то, что реально осталось на сервере
    # (раньше сюда уходило current | {label} без снятых старых ai:*).
    labels_after = (current - set(stale_verdicts)) | ({label} if need_verdict_post else set())

    # Commit Status API — тот же вердикт вторым каналом, параллельно метке
    # (#345): allow_auto_merge читает required status checks, не метки.
    # error → pending, не failure (review_labels.ai_status_state): сбой
    # провайдера/транспорта — не решение о коде, у него свой газ — автоповтор
    # по таймеру (#196), failure держал бы проверку красной до нового пуша.
    status_description = f"ai-review: error — {reason}" if verdict == "error" else f"ai-review: {verdict}"
    review_labels.post_commit_status(
        repo, args.head, review_labels.STATUS_AI_REVIEW,
        review_labels.ai_status_state(verdict), status_description,
        run_gh, review_labels.run_target_url(repo))

    # Газ к тормозу review:large (#204): подтверждение размера опирается на
    # состоявшийся вердикт AI, а не на факт запуска — added считается по
    # files, уже сверенным с головой ВЫШЕ, поэтому не может прийти из уехавшей
    # головы (тот же баг, что и протухший diff_fp, закрыт одной сверкой).
    added = sum(f["additions"] for f in files)
    apply_large_ok(repo, args.pr, added, labels_after, verdict)

    # Третья категория находок (#462): блоки ЗАМЕЧАНИЕ сливаются в чеклист
    # ТЕЛА PR, не в комментарий — тело переживает прокрутку и не пропадает
    # среди прочих комментариев. merge_checklist сама решает, нужен ли PATCH
    # вовсе (None — новых пунктов нет, отмеченные автором чекбоксы не трогаем).
    if remarks:
        new_pr_body = review_checklist.merge_checklist(pull_after_files.get("body") or "", remarks)
        if new_pr_body is not None:
            run_gh("api", "-X", "PATCH", f"repos/{repo}/pulls/{args.pr}",
                   "-f", "body=" + new_pr_body)
            print(f"checklist: {len(remarks)} замечаний слито в тело PR")

    # Отпечаток диффа (#252) — в шапку комментария, чтобы check_pr.py на
    # следующем пуше мог сравнить и сохранить метку, если PR не изменился
    # (см. review_labels.diff_fingerprint/diff_unchanged). files — те же,
    # что уже сверены с головой выше.
    diff_fp = review_labels.diff_fingerprint(files)
    body = build_comment(args.pr, args.head, verdict, findings, tasks, diff_fp=diff_fp,
                          remarks=remarks, chain_provider=args.chain_provider,
                          reset_hint=args.reset_hint, reason_tag_value=reason_tag_value)
    run_gh("api", "-X", "POST", f"repos/{repo}/issues/{args.pr}/comments",
           "-f", "body=" + body)

    if verdict == "error":
        tail = redact("\n".join((answer or "").splitlines()[-12:]))
        print(f"::error::ответ не соответствует контракту вердикта ({reason}) "
              f"— ai:failed, ревью повторится")
        print(f"хвост ответа:\n{tail}")
        return 1
    print(f"verdict: {verdict} — {label}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    gather = sub.add_parser("gather", help="факты PR + дифф-пак + промпт")
    gather.add_argument("--pr", type=int, required=True)
    gather.add_argument("--out", required=True)
    gather.set_defaults(func=cmd_gather)

    should_run = sub.add_parser(
        "should-run", help="нужен ли дорогой прогон (печатает true/false, #294)")
    should_run.add_argument("--pr", type=int, required=True)
    # Осознанный ручной повтор (input force:true, #399, было — любой
    # workflow_dispatch безусловно): пропускает сверку отпечатка целиком,
    # печатает true без обращения к сети.
    should_run.add_argument("--force", action="store_true", default=False,
                             help="явный вход force:true (workflow_dispatch) — не сверять отпечаток диффа")
    should_run.set_defaults(func=cmd_should_run)

    verdict = sub.add_parser("verdict", help="разбор ответа + комментарий + метка")
    verdict.add_argument("--pr", type=int, required=True)
    verdict.add_argument("--answer", required=True)
    verdict.add_argument("--head", required=True)
    # Код возврата dsh (ai_dsh.sh::dsh_rc.txt) — различает «транспорт упал»
    # от «дсш вернул текст не по контракту». Необязателен (default=""):
    # ручной запуск verdict без этого аргумента не должен падать — просто
    # теряет уточнение причины (см. transport_failed: пусто → не транспорт).
    verdict.add_argument("--dsh-rc", default="")
    # Тег причины из $AI_WORK/failure_reason.txt (ai_dsh.sh, #419): quota_exhausted
    # | rate_limit_retry_budget_exceeded | all_providers_exhausted (#727) | пусто.
    # Необязателен по той же причине, что и --dsh-rc — ручной запуск без него
    # просто теряет уточнение.
    verdict.add_argument("--failure-reason", default="")
    # Цепочка провайдеров (#727): имя провайдера, фактически ответившего
    # (пусто на отказе), и даты сброса опробованных ($AI_WORK/chain_provider.txt
    # / chain_reset_hint.txt, ai_dsh.sh) — оба необязательны, тот же принцип.
    verdict.add_argument("--chain-provider", default="")
    verdict.add_argument("--reset-hint", default="")
    verdict.set_defaults(func=cmd_verdict)

    args = parser.parse_args()
    # RuntimeError (gh() — сеть/права/битый ответ API) ловится ЗДЕСЬ, внутри
    # main(), а не в блоке if __name__ снаружи (было — регрессия #416/#399,
    # живой прогон 34009775887, PR #333): should-run вызывается из
    # ai-review.yml как `run_needed=$(python ... should-run ...)` — bash
    # command substitution забирает ТОЛЬКО stdout процесса. print() без
    # file=sys.stderr писал причину сбоя в stdout — она уезжала в
    # переменную $run_needed и никогда не попадала в лог job'а: шаг падал
    # (echo "::error::не смог решить...") без единой подсказки почему.
    # Различие «решили не запускать» (false, exit 0 — все ветки
    # cmd_should_run/cmd_verdict/cmd_gather явно возвращают 0) и «не смогли
    # решить» (RuntimeError, exit 1) не терялось само по себе — терялось
    # ТОЛЬКО видимое обоснование второго исхода. Тот же приём, что уже у
    # scripts/lib/claim_task.py::main — try/except внутри функции, а не
    # снаружи, потому и тестируем вызовом main() напрямую (test_ai_review.py).
    try:
        return args.func(args)
    except RuntimeError as error:
        print(f"::error::ai-review: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
