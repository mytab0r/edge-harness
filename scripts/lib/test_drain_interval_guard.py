#!/usr/bin/env python3
"""Гвардия issue #1048: интервал фонового дрена морды (DRAIN_INTERVAL_SECS)
не должен молча вернуться к 1с — старому значению, при котором почти каждое
событие транскрипта оплачивало полный ре-скан истории сессии
(`EdgeSessionStore.appendHarnessEvents`,
dsh-edge/patches/0004-harness-ingest.patch:260-282: `openAgentForTurn` +
`snapshotEvents()` ради `baseTurn` — O(вся история) чтений на КАЖДЫЙ
`POST /api/sessions/:id/ingest`, независимо от размера батча). Замер
#678 (docs/research/20-cloudflare-free.md): ≈427 rows_read/событие при 1с —
причина, по которой квота account-wide `rows_read` упиралась в потолок
раньше архитектурного (~22–23 прогона worker.yml/сутки вместо ~31 по
записям).

Класс, который эта гвардия закрывает: дефолт интервала был ЗАДУБЛИРОВАН
буквальным литералом `${DRAIN_INTERVAL_SECS:-1}` в ДВУХ файлах
(scripts/lib/dsh-edge-session.sh и scripts/hands/dsh_task.sh) — правка
одного места оставляла второй незамеченным регрессом. Фикс #1048 свёл
дефолт к ОДНОЙ константе (`DSH_EDGE_DRAIN_INTERVAL_DEFAULT_SECS` в
dsh-edge-session.sh); эта гвардия делает возврат к дублированному хардкоду
структурно видимым:

1. Константа-дефолт объявлена РОВНО ОДИН РАЗ во всём репозитории, и
   ровно в scripts/lib/dsh-edge-session.sh (единственное место правды).
2. Её значение не ниже безопасного пола (10с) — регресс к 1с (или к
   любому числу ниже пола) красит CI, а не проходит тихо.
3. Ни один другой shell-скрипт или workflow (кроме тестовых
   фикстур/смоуков, которые СОЗНАТЕЛЬНО пришпиливают интервал к 1 ради
   скорости прогона) не хардкодит свой числовой дефолт/значение для
   DRAIN_INTERVAL_SECS — обязаны читать разделяемую константу.

Тесты, смоук-фикстуры (scripts/lib/test/**, файлы, где путь содержит
"smoke") исключены из правила 3 — тот же принцип, что уже несёт
test_dsh_stderr_redact_guard.py: синтетические/застабленные данные, не
продовый транспорт, их значение — сознательное решение теста, не молчаливый
регресс прода.

Доказано мутацией (см. отчёт задачи #1048): вернуть в
scripts/hands/dsh_task.sh `DRAIN_INTERVAL_SECS="${DRAIN_INTERVAL_SECS:-1}"`
(убрать ссылку на константу) — красит test_no_other_file_hardcodes_a_literal_interval;
вернуть DSH_EDGE_DRAIN_INTERVAL_DEFAULT_SECS=1 в dsh-edge-session.sh —
красит test_default_defined_exactly_once_above_floor.

Запуск: python -m pytest scripts/lib/test_drain_interval_guard.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_FILE = REPO_ROOT / "scripts" / "lib" / "dsh-edge-session.sh"
CONST_NAME = "DSH_EDGE_DRAIN_INTERVAL_DEFAULT_SECS"
# Пол безопасности: ниже него частота вызовов ingest возвращается в режим
# «почти каждое событие — отдельный вызов» (замер #678, mean gap событий
# ~6.3с) — тот же класс отказа, что и старый дефолт 1с.
MIN_ACCEPTABLE_DEFAULT_SECS = 10

CONST_DEF_RE = re.compile(rf"^{CONST_NAME}=(\d+)\s*$", re.M)
# Хардкод DRAIN_INTERVAL_SECS с ЧИСЛОВЫМ литералом — bash-фоллбэк `:-N`,
# прямое присвоение `=N`/`="N"`, YAML env `: N`/`: "N"`. Не матчит ссылку на
# разделяемую константу (`:-$DSH_EDGE_DRAIN_INTERVAL_DEFAULT_SECS>` —
# после оператора идёт `$`, не цифра).
LITERAL_RE = re.compile(r"\bDRAIN_INTERVAL_SECS\b\s*(?::-|=|:)\s*[\"']?\d+")


def _is_test_fixture(path: Path) -> bool:
    rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    return "/test/" in f"/{rel}" or "smoke" in rel or rel.startswith("scripts/lib/test_")


def _all_candidate_files() -> list[Path]:
    files = list((REPO_ROOT / "scripts").rglob("*.sh"))
    files += list((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    files += list((REPO_ROOT / ".github" / "workflows").glob("*.yaml"))
    return sorted(set(files))


def test_default_defined_exactly_once_above_floor():
    hits = []
    for path in _all_candidate_files():
        text = path.read_text(encoding="utf-8")
        for m in CONST_DEF_RE.finditer(text):
            hits.append((path, int(m.group(1))))
    assert hits, (
        f"{CONST_NAME} нигде не объявлена присвоением `{CONST_NAME}=<число>` — "
        "единственное место правды исчезло"
    )
    assert len(hits) == 1, (
        f"{CONST_NAME} объявлена {len(hits)} раз(а) — единственное место "
        f"правды раздвоилось: {[str(p.relative_to(REPO_ROOT)) for p, _ in hits]}"
    )
    (path, value), = hits
    assert path == CANONICAL_FILE, (
        f"{CONST_NAME} объявлена в {path}, а не в {CANONICAL_FILE.relative_to(REPO_ROOT)} — "
        "каноническое место правды переехало без обновления гвардии (или это регресс)"
    )
    assert value >= MIN_ACCEPTABLE_DEFAULT_SECS, (
        f"{CONST_NAME}={value} ниже безопасного пола {MIN_ACCEPTABLE_DEFAULT_SECS}с — "
        "это тот же класс отказа, что и старый дефолт 1с (issue #1048, замер #678: "
        "почти каждое событие транскрипта снова будет оплачивать полный ре-скан "
        "истории сессии в dsh-edge)"
    )


def test_no_other_file_hardcodes_a_literal_interval():
    offenders = []
    for path in _all_candidate_files():
        if path == CANONICAL_FILE or _is_test_fixture(path):
            continue
        text = path.read_text(encoding="utf-8")
        code_lines = [
            (idx, line)
            for idx, line in enumerate(text.splitlines(), 1)
            if not line.lstrip().startswith("#")
        ]
        for idx, line in code_lines:
            if LITERAL_RE.search(line):
                rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
                offenders.append(f"{rel}:{idx} — {line.strip()}")
    assert not offenders, (
        "Найден хардкод числового DRAIN_INTERVAL_SECS вне единственного места "
        f"правды ({CANONICAL_FILE.relative_to(REPO_ROOT)}) — класс #1048 "
        f"(два файла с одинаковым `:-1`) вернулся: {offenders}. Читай "
        f"{CONST_NAME} из dsh-edge-session.sh, не заводи свой дефолт"
    )


def test_canonical_file_actually_uses_the_constant_as_fallback():
    """Сама константа обязана реально ПРИМЕНЯТЬСЯ как фоллбэк дрена — иначе
    объявление есть, а поведение осталось на другом хардкоде."""
    text = CANONICAL_FILE.read_text(encoding="utf-8")
    assert re.search(rf"DRAIN_INTERVAL_SECS:-\${CONST_NAME}\b", text), (
        f"{CANONICAL_FILE.relative_to(REPO_ROOT)} объявляет {CONST_NAME}, но фоновый "
        "дрен (dsh_edge_start_drain) больше не использует её как фоллбэк "
        "DRAIN_INTERVAL_SECS — константа осиротела"
    )
