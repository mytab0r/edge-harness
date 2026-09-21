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
            matches.append({**candidate, "score": score})  # type: ignore[typeddict-item]
    return sorted(matches, key=lambda match: match["score"], reverse=True)


# ── CLI: fetch + печать для scripts/gh/issue-create (без сети — только тут) ──


def fetch_open_task_candidates(repo: str) -> list[Candidate]:
    """`gh issue list` сам обходит страницы до `--limit` (в отличие от сырого
    `gh api .../issues?per_page=100` — класс #308, здесь не воспроизведён,
    потому что используется другой, постранично-агрегирующий интерфейс gh
    CLI, а не одностраничный REST-вызов). `--limit` с запасом над измеренным
    размером пула (~110 задач, #310).

    Тестовый шов: `DUPLICATE_GUARD_FIXTURE=<путь>` подменяет реальный вызов
    чтением готового JSON-файла — ТОЛЬКО для тестов, гоняющих bash-обёртку
    (scripts/gh/test/issue-create-duplicate-guard.test.sh и
    scripts/lib/test_issue_create_repo_resolution.py — второй запускает ту же
    обёртку процессом из pytest, #1395). Причина шва именно здесь, а не
    monkeypatch subprocess (как test_claim_task.py): этот путь вызывается ИЗ
    bash (scripts/gh/issue-create) новым процессом python3, куда monkeypatch
    вызывающего не достаёт. СНЯТИЕ переменной — тоже шов, и намеренный: без
    неё вызов идёт в настоящий `gh issue list`, и его отказ воспроизводит
    настоящий сбой выборки пула (класс #1395, ветка FETCH_FAILED_RC)."""
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


# Код возврата «открытый пул прочитать не удалось» (#1395, находка ai-ревью
# PR #1403). Отдельный и от 0 («прочитан, результат в stdout»), и от 2 («не та
# форма вызова»): прежняя редакция возвращала 0 и на этом исходе, печатая WARN
# и пустой stdout, — для вызывающего «не смог посмотреть» выглядело ровно как
# «дублей нет». Это тот же класс, ради которого заведена #1395, только этажом
# ниже. Одно место правды на обе стороны шва — читает scripts/gh/issue-create.
FETCH_FAILED_RC = 3


def main(argv: list[str]) -> int:
    """`check <repo> <title>` печатает совпадения (TSV: score\\tnumber\\ttitle\\turl,
    одна строка на кандидата, пусто — совпадений нет) в stdout. Коды возврата
    РАЗЛИЧАЮТ три исхода, а не два (#1395): 0 — пул прочитан, результат в
    stdout (пусто = совпадений нет, это ФАКТ); 2 — не та форма вызова;
    FETCH_FAILED_RC — пул прочитать НЕ удалось, совпадений не «ноль», их
    просто никто не считал. Решение «блокировать/пропустить» принимает вызывающий
    (`scripts/gh/issue-create`) по содержимому stdout, не по коду возврата
    (симметрично task-branch: сбой ЭТОЙ, необязательной проверки — не повод
    ронять создание issue) — но теперь у него есть, ЧЕМ отличить «дублей нет»
    от «посмотреть не смогли»: до #1403 сбой сети/gh давал 0 и пустой stdout,
    и issue-create доходил до `gh issue create` с одной WARN-строкой в логе
    (исполнено ревью на подменённом `gh`, не выведено из чтения)."""
    if len(argv) != 4 or argv[1] != "check":
        print("использование: duplicate_guard.py check <repo> <title>", file=sys.stderr)
        return 2
    _, _, repo, title = argv
    try:
        candidates = fetch_open_task_candidates(repo)
    except Exception as error:  # noqa: BLE001 — сбой инструмента, не повод блокировать
        print(f"WARN: duplicate_guard не смог получить открытый пул ({error}) — "
              f"сверка на дубли НЕ выполнена (это не «дублей нет»).", file=sys.stderr)
        return FETCH_FAILED_RC
    for match in find_similar_open_tasks(title, candidates):
        print(f"{match['score']:.2f}\t{match['number']}\t{match['title']}\t{match['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
