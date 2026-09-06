#!/usr/bin/env python3
"""Похожесть заголовка новой задачи на уже открытые — гвардия «несколько
агентов независимо диагностируют один и тот же дефект» (#566).

Класс проблемы: единственная сегодня защита от параллельной работы —
аренда номера задачи (`claim_task.py`, #121, ADR 0006). Она бессильна ДО
того, как номер существует, — а диагностика симптома (агент видит красный
прогон/пустой экран/упавший шаг) всегда начинается без номера. Измеренный
случай (2026-09-06, три агента одной сессии, один и тот же дефект — пустой
шелл морды после коллизии локали `settings.plugins`): issue #518, #547,
#564 заведены независимо; #548 и #564 закрыты как дубликат УЖЕ ПОСЛЕ того,
как под ними были открыты PR (#557, #563) — час(ы) работы потрачены впустую,
потому что заведение issue ничего не искало среди уже открытого пула.

Момент вмешательства — заведение issue: это первый артефакт «начинаю
работу над этим», который существует раньше диагностики вглубь и раньше
кода (см. `scripts/gh/issue-create`, единственный документированный вход
для агента/человека из терминала, #526). Сравнение — только с ОТКРЫТЫМИ
задачами пула: закрытые исключены осознанно — правило репозитория
(AGENTS.md, «Закрытая задача не переоткрывается никогда») САНКЦИОНИРУЕТ
новую, более узкую задачу со ссылкой на уже закрытую; сравнение с закрытой
issue наказывало бы разрешённый паттерн, а не ловило бы дубль.

## Метод и честный потолок

Токенная схожесть (Jaccard по значимым словам заголовка, без стоп-слов и
коротких токенов) — не полнотекстовый пересказ и не эмбеддинги: инструмент
того же уровня, что уже применяет `stall_detector._normalize_warn` для
своих предупреждений, только здесь нет готового структурированного
отпечатка (диагностику ещё не довели до кода) — сравнение обязано быть
текстовым. Ограничение измерено на РЕАЛЬНЫХ заголовках инцидента:

  - #562 «deploy-dsh-edge: автооткат (#549) оставляет рассинхрон версий —
    следующий wrangler secret put падает» vs #564 «deploy-dsh-edge:
    автооткат (#549) блокирует следующий деплой — wrangler secret put
    падает VERSION_NOT_DEPLOYED» — score ≈0.53, уверенно ловится.
  - #518 «deploy-dsh-edge.yml красный после бампа 0.11.1: ...» vs #548
    «Шелл dsh-edge пуст после бампа 0.11.1 — коллизия локали
    settings.plugins...» — score ≈0.18, НЕ ловится: разные слова описывают
    один и тот же корень (красная сборка vs пустой экран). Это предел
    лексического сравнения, а не тихая недоделка: функция возвращает
    только то, что реально совпало по словам заголовка.

## Честная граница

Это вход из терминала/промпта агента (`scripts/gh/issue-create`) — ровно тот
канал, которым были заведены #518/#547/#564. Программные создатели issue
(`scripts/lib/pool_issue.py::create_pool_issue`, которую зовут
`stall_detector.py`/`upstream_drift.py`/`file_tasks.py`/`scheduler.py::after_merge`/
`pulse_guard.py::failure_watch`) этим модулем НЕ покрыты: у
`stall_detector`/`upstream_drift` уже есть своя, более точная дедупликация
(машинный отпечаток/точное сравнение версий); `file_tasks.py` уже сравнивает
заголовки точным совпадением. ЖИВОЙ случай дубля из программного пути уже
случился для `failure_watch` (#578/#580/#589/#592/#598, дефект #610) и закрыт
НЕ подключением `find_similar_open_tasks` сюда, а точным (не Jaccard)
сравнением заголовка в самом вызывающем — заголовок там шаблонный
(`f"CI: {workflow} падает — {job_name}"`), и токенная схожесть эту опасность
только создала бы: «job-0» и «job-1» после токенизации по словам совпадают
Jaccard'ом в 1.0 (замер), а обязаны оставаться РАЗНЫМИ классами. Урок общий
для следующего программного создателя: на шаблонных заголовках нужен точный
compare в вызывающем, не opt-in подключение этого модуля.

Запуск тестов: python -m pytest scripts/lib/test_duplicate_guard.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence, TypedDict

# Порог схожести — одно место правды (тюнинг только здесь, не в вызывающих).
# 0.3 comfortably ниже измеренного положительного случая (0.53) и выше
# измеренного отрицательного (0.18) — см. докстринг модуля.
DEFAULT_THRESHOLD = 0.3

# Стоп-слова — общие RU/EN предлоги/союзы, не несущие смысла темы. Список
# короткий нарочно: цель не грамматический разбор, а отсеять шум, который
# иначе раздувает join-множество токенов и топит реальное совпадение.
STOPWORDS = {
    "и", "в", "во", "не", "на", "с", "со", "по", "для", "из", "к", "у",
    "от", "до", "за", "о", "об", "что", "это", "как", "или", "а", "но",
    "уже", "ещё", "после", "перед", "при", "без", "же", "то", "ли",
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is",
    "are", "with", "at", "by", "not", "no",
}

TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)


def tokenize(text: str) -> set[str]:
    """Значимые слова заголовка: lowercase, разбито по не-буквенно-цифровым
    символам (дефисы/слэши/двоеточия — разделители, не часть токена),
    короткие (<3 симв.) и стоп-слова отброшены."""
    tokens = {token.lower() for token in TOKEN_RE.findall(text or "")}
    return {token for token in tokens if len(token) >= 3 and token not in STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class Candidate(TypedDict):
    number: int
    title: str
    url: str


class Match(Candidate):
    score: float
    reason: str


def find_similar_open_tasks(
    title: str, candidates: Sequence[Candidate], threshold: float = DEFAULT_THRESHOLD,
) -> list[Match]:
    """Кандидаты из уже ОТКРЫТЫХ задач пула, чей заголовок лексически похож
    на `title` не ниже `threshold`. Пустой вход/нет совпадений — пустой
    список, не ошибка. Отсортировано по убыванию схожести (самый похожий
    первым — вызывающему код важен именно он)."""
    query = tokenize(title)
    matches: list[Match] = []
    for candidate in candidates:
        score = jaccard(query, tokenize(candidate.get("title", "")))
        if score >= threshold:
            matches.append({**candidate, "score": score, "reason": "похожесть заголовка"})  # type: ignore[typeddict-item]
    return sorted(matches, key=lambda match: match["score"], reverse=True)


# ── Второй слой (#570): улики в ТЕЛЕ issue, не в заголовке ───────────────────
#
# Идея: лексика заголовка ненадёжна (см. честный потолок выше — #518 vs #548,
# score≈0.18), а УЛИКА дефекта в теле — надёжна: два агента, диагностирующих
# один баг, почти всегда ссылаются на один и тот же артефакт — путь файла,
# номер прогона Actions, номер issue/PR, дословную цитату из блока ```.
#
# Честная граница, измеренная на РЕАЛЬНОМ пуле репозитория (324 задачи с
# меткой task, 2026-09-07, `gh issue list --state all`):
#
#   - Голое совпадение ссылок #N (одна задача упоминает номер другой) само по
#     себе — ШУМ, не улика: почти каждая задача этого репозитория ссылается
#     на 5-15 других номеров как на контекст/предысторию (пишем много прозы
#     со ссылками). Не отфильтрованное самостоятельное совпадение по ссылкам
#     дало 102 срабатывания из 138 открытых задач при пересчёте каждой как
#     «новой» — практически любая задача цепляет что-то. Поэтому упоминание
#     номера ОДНО не считается уликой — только в паре со второй уликой.
#   - Дословная цитата из блока ``` (Jaccard по токенам ВНУТРИ ``` ... ```,
#     не всего тела) — самая надёжная улика: разный перенос строк в
#     обёрнутом логе не должен ронять совпадение (#562 body переносит текст
#     иначе, чем #564, но это тот же самый лог wrangler), поэтому сравнение
#     токенное, не строка-в-строку. Измерено: #562/#564 → score 0.50 (при 15
#     общих токенах) — ловится; #518/#548 → 0.11 (всего 4 общих токена) — не
#     ловится квотой вовсе, это ожидаемо (разные ошибки одного инцидента).
#   - Путь файла — надёжная улика, ЕСЛИ он не common (см. ниже): частые пути
#     вроде `docs/research/30-rejected-alternatives.md` (обязательная строка
#     чеклиста, встречается в 42/324 задач), `AGENTS.md` (35/324),
#     `deploy-dsh-edge.yml` (32/324) — общие для всего репозитория, не
#     специфичны ни одному дефекту.
#   - Частотный фильтр (генерический, не список хардкод-строк): путь/ссылка,
#     встреченные больше чем в `EVIDENCE_COMMON_MAX_SHARE` пула (или больше
#     `EVIDENCE_COMMON_MIN_ABS` штук, что больше) — считаются common и не
#     участвуют в сравнении. Ниже `EVIDENCE_COMMON_MIN_POOL` кандидатов
#     фильтр не включается вовсе (иначе на маленьких тестовых фикстурах он бы
#     выбрасывал реальную улику просто потому, что в фикстуре 2-3 кандидата).
#
# Правило совпадения (что считается уликой дубля):
#   (a) дословная цитата из ``` (Jaccard ≥ EVIDENCE_QUOTE_JACCARD_THRESHOLD И
#       общих токенов ≥ EVIDENCE_QUOTE_MIN_SHARED_TOKENS) — САМОДОСТАТОЧНО;
#   (b) общий номер прогона Actions (`actions/runs/<id>`) — САМОДОСТАТОЧНО
#       (совпадение конкретного числа с прогоном практически невозможно
#       случайно);
#   (c) новое тело явно цитирует номер КОНКРЕТНОГО кандидата (`#N`) И у них
#       есть ОБЩИЙ путь ИЛИ ОБЩАЯ (не common) ссылка на третью задачу — ни
#       голая цитата номера, ни голое совпадение пути/ссылки поодиночке НЕ
#       считаются уликой, только их пересечение.
#
# На этом правиле #518 vs #548 ловится через (c): #548 дословно содержит
# «#518» И оба ссылаются на #505/#513 (бамп 0.11.1). #562 vs #564 ловится
# через (a): дословная цитата ошибки wrangler.
#
# Контрольная выборка (все 138 открытых задач репозитория пересчитаны как
# «новые» против всего пула 324): 28/138 дают хотя бы одно совпадение — само
# по себе не ноль, но ручная проверка каждого совпадения не нашла ни одной
# пары ЗАВЕДОМО РАЗНЫХ задач (все делят редкий путь/ссылку на тот же
# конкретный трекер, не общий шаблонный файл) — граница честная, а не
# подогнанная: см. отчёт агента, приложивший разбор конкретных 28 пар.
#
# Область (#570 vs #566): в отличие от первого слоя (только ОТКРЫТЫЕ, чтобы
# не наказывать санкционированный паттерн «закрыли → завели новую, более
# узкую, related»), этот слой сравнивает и с ЗАКРЫТЫМИ задачами тоже — сам
# санкционированный паттерн не блокируется НАВСЕГДА, он просто проходит через
# тот же газ `--confirm-not-duplicate`, что и обычный дубль.

EVIDENCE_COMMON_MIN_POOL = 3
EVIDENCE_COMMON_MAX_SHARE = 0.02
EVIDENCE_COMMON_MIN_ABS = 3

EVIDENCE_QUOTE_JACCARD_THRESHOLD = 0.4
EVIDENCE_QUOTE_MIN_SHARED_TOKENS = 5

CODE_FENCE_RE = re.compile(r"```(?:[^\n]*)\n(.*?)```", re.DOTALL)
ISSUE_REF_RE = re.compile(r"#(\d{1,6})\b")
RUN_URL_RE = re.compile(r"actions/runs/(\d+)")
PATH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:mjs|jsx?|tsx?|py|sh|ya?ml|json|md)\b")


class Evidence(TypedDict):
    paths: set[str]
    refs: set[str]
    runs: set[str]
    quote_tokens: set[str]


class CandidateWithBody(Candidate):
    body: str


class EvidenceMatch(Candidate):
    score: float
    reason: str


def extract_evidence(body: str) -> Evidence:
    """Улики тела issue: пути файлов, ссылки на issue/PR (`#N`), номера
    прогонов Actions (из URL `actions/runs/<id>`), значимые слова ВНУТРИ
    блоков ``` ... ``` (не всего тела — вне кода текст сравнивает первый
    слой через заголовок, здесь нужна именно дословная цитата ошибки)."""
    body = body or ""
    quoted = " ".join(match.group(1) for match in CODE_FENCE_RE.finditer(body))
    return {
        "paths": set(PATH_RE.findall(body)),
        "refs": set(ISSUE_REF_RE.findall(body)),
        "runs": set(RUN_URL_RE.findall(body)),
        "quote_tokens": tokenize(quoted),
    }


def _common_items(key: str, pool: Sequence[Evidence]) -> set[str]:
    """Пути/ссылки, встреченные в подозрительно большой доле пула — не
    улика конкретного дефекта, а общая для репозитория деталь (чеклист,
    имя воркфлоу, обязательный файл). Ниже `EVIDENCE_COMMON_MIN_POOL`
    кандидатов фильтр не включается — на маленьком пуле частота ничего не
    доказывает (см. докстринг раздела выше)."""
    if len(pool) < EVIDENCE_COMMON_MIN_POOL:
        return set()
    counts: dict[str, int] = {}
    for evidence in pool:
        for item in evidence[key]:  # type: ignore[literal-required]
            counts[item] = counts.get(item, 0) + 1
    limit = max(EVIDENCE_COMMON_MIN_ABS, len(pool) * EVIDENCE_COMMON_MAX_SHARE)
    return {item for item, count in counts.items() if count > limit}


def find_evidence_matches(
    body: str, candidates: Sequence[CandidateWithBody], number: int | None = None,
) -> list[EvidenceMatch]:
    """Кандидаты (ОТКРЫТЫЕ и ЗАКРЫТЫЕ — см. докстринг раздела), чьё тело
    делит с `body` улику дефекта. Правило совпадения — САМОДОСТАТОЧНАЯ
    дословная цитата ``` или общий прогон Actions, либо явная ссылка на
    номер кандидата ВМЕСТЕ с общим (не common) путём/ссылкой — см. докстринг
    раздела выше за измеренным обоснованием каждого условия. Отсортировано
    по убыванию score (quote-score для (a), 1.0 для детерминированных (b)/(c)
    — это не «схожесть», а бинарная улика)."""
    new_evidence = extract_evidence(body)
    pool_evidence = [extract_evidence(candidate.get("body", "")) for candidate in candidates]
    common_paths = _common_items("paths", pool_evidence)
    common_refs = _common_items("refs", pool_evidence)

    matches: list[EvidenceMatch] = []
    for candidate, cand_evidence in zip(candidates, pool_evidence):
        if number is not None and candidate["number"] == number:
            continue
        self_cite = str(candidate["number"]) in new_evidence["refs"]
        shared_paths = (new_evidence["paths"] & cand_evidence["paths"]) - common_paths
        shared_runs = new_evidence["runs"] & cand_evidence["runs"]
        shared_refs = (
            (new_evidence["refs"] & cand_evidence["refs"])
            - common_refs - {str(candidate["number"])} - ({str(number)} if number is not None else set())
        )
        quote_score = jaccard(new_evidence["quote_tokens"], cand_evidence["quote_tokens"])
        shared_quote_tokens = new_evidence["quote_tokens"] & cand_evidence["quote_tokens"]

        reasons: list[str] = []
        score = 0.0
        if quote_score >= EVIDENCE_QUOTE_JACCARD_THRESHOLD and len(shared_quote_tokens) >= EVIDENCE_QUOTE_MIN_SHARED_TOKENS:
            reasons.append(f"дословная цитата из блока ``` (score {quote_score:.2f})")
            score = max(score, quote_score)
        if shared_runs:
            reasons.append("общий прогон Actions #" + ", #".join(sorted(shared_runs)))
            score = 1.0
        if self_cite and (shared_paths or shared_refs):
            reasons.append(f"новая задача ссылается на #{candidate['number']}")
            if shared_paths:
                reasons.append("общий путь: " + ", ".join(sorted(shared_paths)))
            if shared_refs:
                reasons.append("общая ссылка: #" + ", #".join(sorted(shared_refs)))
            score = 1.0
        if reasons:
            matches.append({**candidate, "score": score, "reason": "; ".join(reasons)})  # type: ignore[typeddict-item]
    return sorted(matches, key=lambda match: match["score"], reverse=True)


# ── CLI: fetch + печать для scripts/gh/issue-create (без сети — только тут) ──


def fetch_open_task_candidates(repo: str) -> list[Candidate]:
    """`gh issue list` сам обходит страницы до `--limit` (в отличие от сырого
    `gh api .../issues?per_page=100` — класс #308, здесь не воспроизведён,
    потому что используется другой, постранично-агрегирующий интерфейс gh
    CLI, а не одностраничный REST-вызов). `--limit` с запасом над измеренным
    размером пула (~110 задач, #310).

    Тестовый шов: `DUPLICATE_GUARD_FIXTURE=<путь>` подменяет реальный вызов
    чтением готового JSON-файла — ТОЛЬКО для тестов bash-обёртки
    (scripts/gh/test/issue-create-duplicate-guard.test.sh). Причина шва
    именно здесь, а не monkeypatch subprocess (как test_claim_task.py):
    этот путь вызывается ИЗ bash (scripts/gh/issue-create) новым процессом
    python3, а не из pytest, где monkeypatch недоступен физически."""
    fixture = os.environ.get("DUPLICATE_GUARD_FIXTURE")
    if fixture:
        return json.loads(Path(fixture).read_text(encoding="utf-8"))
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--state", "open", "--label", "task",
         "--json", "number,title,url", "--limit", "500"],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh issue list завершился с ошибкой")
    return json.loads(result.stdout or "[]")


def fetch_evidence_candidates(repo: str) -> list[CandidateWithBody]:
    """Как `fetch_open_task_candidates`, но `--state all` (ОТКРЫТЫЕ И
    ЗАКРЫТЫЕ — см. докстринг раздела «Второй слой» за обоснованием) и с
    телом (`body`) — без него не из чего извлечь улики. `--limit` с запасом
    над измеренным размером пула (324 задачи с меткой task, 2026-09-07).

    Тестовый шов: `DUPLICATE_GUARD_EVIDENCE_FIXTURE=<путь>` — отдельная
    переменная от `DUPLICATE_GUARD_FIXTURE` (та фикстура без `body`, не
    годится для улик), тот же принцип (см. `fetch_open_task_candidates`)."""
    fixture = os.environ.get("DUPLICATE_GUARD_EVIDENCE_FIXTURE")
    if fixture:
        return json.loads(Path(fixture).read_text(encoding="utf-8"))
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--state", "all", "--label", "task",
         "--json", "number,title,url,body", "--limit", "1000"],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh issue list завершился с ошибкой")
    return json.loads(result.stdout or "[]")


def _print_matches(matches: Sequence[Match | EvidenceMatch]) -> None:
    for match in matches:
        print(f"{match['score']:.2f}\t{match['number']}\t{match['title']}\t{match['url']}\t{match['reason']}")


def main(argv: list[str]) -> int:
    """Две подкоманды, обе печатают TSV (score\\tnumber\\ttitle\\turl\\treason,
    одна строка на кандидата, пусто — совпадений нет) в stdout и ВСЕГДА
    возвращают 0 — решение «блокировать/пропустить» принимает вызывающий
    (`scripts/gh/issue-create`) по содержимому stdout, не по коду возврата
    (симметрично task-branch: сбой ЭТОЙ, необязательной проверки — не повод
    ронять создание issue). Сбой сети/gh — предупреждение в stderr, пустой
    stdout (как будто совпадений нет): недоступность инструмента дедупликации
    не должна блокировать реальную работу агента.

    - `check <repo> <title>` — первый слой (похожесть заголовка, #566).
    - `check-evidence <repo>` — второй слой (#570), тело новой issue читает
      из STDIN (не argv — тело многострочное и содержит спецсимволы, argv
      для этого не годится)."""
    if len(argv) == 4 and argv[1] == "check":
        _, _, repo, title = argv
        try:
            candidates = fetch_open_task_candidates(repo)
        except Exception as error:  # noqa: BLE001 — сбой инструмента, не повод блокировать
            print(f"WARN: duplicate_guard не смог получить открытый пул ({error}) — "
                  f"проверка похожести пропущена.", file=sys.stderr)
            return 0
        _print_matches(find_similar_open_tasks(title, candidates))
        return 0
    if len(argv) == 3 and argv[1] == "check-evidence":
        _, _, repo = argv
        body = sys.stdin.read()
        try:
            candidates = fetch_evidence_candidates(repo)
        except Exception as error:  # noqa: BLE001 — сбой инструмента, не повод блокировать
            print(f"WARN: duplicate_guard не смог получить пул для улик ({error}) — "
                  f"проверка улик пропущена.", file=sys.stderr)
            return 0
        _print_matches(find_evidence_matches(body, candidates))
        return 0
    print("использование: duplicate_guard.py check <repo> <title> | check-evidence <repo> (<STDIN: тело>)",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
