#!/usr/bin/env python3
"""Канал наблюдаемости уборки рабочих деревьев (issue #1250, пункты 3-4).

Класс дефекта: инцидент #1250 был виден ТОЛЬКО расследованием — уборщик
(`scripts/git/worktree-cleanup.py`) снимал 0 деревьев из 73, а 41 дерево
несло копию `scripts/orchestra/pulse_guard.py` без гвардии #1074, и ни один
механизм это число не измерял. Измерить число может только тот путь, где
деревья физически есть, — сам уборщик (его единственный прод-вызов —
`scripts/git/task-branch`); `scripts/orchestra/repo_invariants.py` гоняется
на раннерах со свежим чекаутом, где `.claude/worktrees` пуст ВСЕГДА:
инвариант, меряющий деревья с раннера, был бы молча зелёным навсегда
(«пустое множество кандидатов», тот же класс, что #882 — шаг рапортует
success, ничего не измерив).

Поэтому канал двухсторонний (тот же рисунок, что `scripts/measure/
pipeline_health.py` + инвариант 12, #882, и `scripts/measure/dispatch_tail.py`,
ADR 0005):

  - пишет ЗДЕСЬ (уборщик, `--publish-snapshot`, вызывается `task-branch`);
  - читает инвариант 24 `repo_invariants.py` через Contents API этой же
    ветки.

Носитель — JSONL на отдельной git-ветке `data/worktree-cleanup` (не main —
не засоряет историю; не DO SQLite — бюджет DO узкий, #575). Транспорт —
`data_branch_writer` (одно место правды на «append + push с ретраем», #882),
второй копии не заводим.

Запись обязана различать «деревьев нет» и «объект ненаблюдаем отсюда»
(находка ревью PR #1257: инвариант, который не различает эти два факта,
молча читает «канал мёртв» как «всё чисто»):
  - «объект ненаблюдаем» — записей нет НИ РАЗУ (`check_worktree_cleanup_
    records`, kind="no-records") или канал молчит дольше `RECORD_STALE_AFTER_
    DAYS` (kind="stale-channel"): из самой записи причину не установить —
    это честно называется в находке, а не угадывается;
  - «деревьев нет» — свежая запись с `total == 0`: прогон СОСТОЯЛСЯ и деревьев
    не нашёл. Это здоровое состояние, не отсутствие наблюдения.

Публикация — best-effort и осознанно ЗАЩИЩЁННАЯ от песочниц: запись
публикуется только если `origin` чекаута, из которого уборщик запущен, —
тот же репозиторий, что целевой (`GITHUB_REPOSITORY`; `publisher_is_safe`).
Тестовые песочницы (`task-branch.test.sh` в repo-ci, guard-тесты) держат
локальный origin — они отпадают на этой проверке ДО сети и не могут
загрязнить прод-канал измерениями синтетических репозиториев.
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
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# data_branch_writer — одно место правды на «append строку + push с ретраем
# на data-ветку» (#882). Библиотечный модуль без своей точки входа, поэтому
# console_utf8 здесь подключает вызывающая точка входа (уборщик) — тот же
# приём, что у самого data_branch_writer.
_DBW_SPEC = importlib.util.spec_from_file_location(
    "data_branch_writer", Path(__file__).resolve().parent / "data_branch_writer.py")
data_branch_writer = importlib.util.module_from_spec(_DBW_SPEC)
_DBW_SPEC.loader.exec_module(data_branch_writer)  # type: ignore[union-attr]


# ── Одно место правды: путь, пороги, состав записи ────────────────────────

DATA_BRANCH = "data/worktree-cleanup"
SNAPSHOT_PATH = "docs/research/data/worktree-cleanup.jsonl"

# Канал считается живым, пока записи приходят хотя бы раз в этот интервал.
# Прогон уборщика — каждое создание НОВОЙ ветки задачи (task-branch), то есть
# многократно в сутки при живом конвейере; 3 суток тишины — уже факт сломанного
# канала или остановившейся работы, а не «выходной».
RECORD_STALE_AFTER_DAYS = 3

# Антишум: повторный прогон с ТЕМИ ЖЕ числами в пределах этого окна не пишет
# вторую строку (иначе каждое создание ветки — новая строка, десятки в сутки
# без новой информации). ИЗМЕНИВШИЕСЯ числа пишутся немедленно — смена
# состояния и есть информация (класс: инвариант 12 ловит «механизм молчит»,
# а не «механизм бормочет одно и то же»).
RECORD_MIN_INTERVAL_MINUTES = 60

# Коды отказа can_remove_worktree, у которых газ недостижим или внешненосителен
# (issue #1250: «45 из 73 деревьев заперты в состоянии, из которого нет выхода»):
# дерево с таким кодом и возрастом >= retention — «запертое». Один источник
# для fail-loud сводки уборщика, записи снимка и инварианта — три копии
# списка были бы отложенным рецидивом.
STUCK_CODES = ("unpushed", "unknown_work", "unknown_pr")

COMMIT_IDENTITY = ("-c", "user.name=edge-harness worktree-snapshot",
                   "-c", "user.email=7416604+mytab0r@users.noreply.github.com")


# ── Чистая логика (тестируется без сети) ──────────────────────────────────

def make_record(*, ts: str, mode: str, total: int, removed: int, kept: int,
                guard_copies: int, stale_guard_copies: int,
                stuck_old_by_code: dict, retention_hours: float) -> dict:
    """Собрать запись снимка. Состав полей — контракт с инвариантом 19
    (repo_invariants.check_worktree_cleanup_records): оба читают ЭТОТ модуль,
    а не копию словаря. stuck_old_by_code — {код: число}, ключи из STUCK_CODES;
    лишние ключи отбрасываются (тихо незнакомое поле записи — будущий класс
    «писатель расширил, читатель не заметил», поэтому незнакомое не попадает
    в запись вовсе)."""
    known = {code: int(stuck_old_by_code.get(code, 0)) for code in STUCK_CODES}
    return {
        "ts": ts,
        "mode": mode,
        "total": int(total),
        "removed": int(removed),
        "kept": int(kept),
        "guard_copies": int(guard_copies),
        "stale_guard_copies": int(stale_guard_copies),
        "stuck_old_total": sum(known.values()),
        **{f"stuck_old_{code}": value for code, value in known.items()},
        "retention_hours": float(retention_hours),
    }


def read_rows(text: str) -> list[dict]:
    """JSONL → список записей. Пустой текст — пустой список (ветки/файла ещё
    нет). Повреждённая строка роняет разбор ГРОМКО (ValueError с номером
    строки): тихо потерянная запись канала — ровно тот silent-wrong, против
    которого весь этот канал (тот же контракт, что pipeline_health.read_rows)."""
    rows = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as error:
            raise ValueError(
                f"{SNAPSHOT_PATH}: строка {line_number} не разбирается как JSON: "
                f"{error}") from error
    return rows


def last_record(rows: list[dict]) -> Optional[dict]:
    """Самая свежая запись по `ts` (не «последняя в файле»: порядок строк —
    порядок записи, но читатель не обязан ему доверять, а одновременные
    писатели могут дописать строки в любом порядке гонки)."""
    if not rows:
        return None
    return max(rows, key=lambda row: str(row.get("ts", "")))


def parse_ts(value: str) -> datetime:
    """ISO-время записи → aware datetime (наивное трактуется как UTC —
    единственный потребитель формата здесь сам и пишет aware)."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_duplicate_observation(last: Optional[dict], record: dict, *,
                             now: datetime,
                             min_interval_minutes: int = RECORD_MIN_INTERVAL_MINUTES) -> bool:
    """True — последняя запись уже несёт ту же информацию (те же наблюдаемые
    числа) и моложе окна антишума: вторую строку писать не нужно. Любое
    изменившееся число или окно старше — не дубль, писать. `last` с нечитаемым
    `ts` не считается дублем (fall through в запись — fail loud по-соседству:
    повреждённая история не должна и запретить новую запись, и замаскироваться
    под свежую)."""
    if not last:
        return False
    numbers = ("total", "removed", "kept", "guard_copies", "stale_guard_copies",
               "stuck_old_total")
    try:
        same_numbers = all(last.get(key) == record.get(key) for key in numbers)
        age_minutes = (now - parse_ts(str(last.get("ts", "")))).total_seconds() / 60
    except (TypeError, ValueError):
        return False
    return same_numbers and age_minutes < min_interval_minutes


def normalize_owner_repo(url: str) -> str:
    """git-URL/путь → сопоставимый ключ репозитория: `https://github.com/o/r.git`
    и `git@github.com:o/r.git` → "o/r"; локальный путь → сам путь без хвостового
    .git (сопоставимый с другим тем же путём, не с github-ключом). Нижний регистр
    (GitHub имена репозиториев нечувствительны к регистру в URL)."""
    text = (url or "").strip().lower()
    if text.endswith(".git"):
        text = text[:-len(".git")]
    marker = "github.com"
    position = text.find(marker)
    if position != -1:
        tail = text[position + len(marker):].lstrip(":/")
        return tail.strip("/")
    return text.rstrip("/")


def publisher_is_safe(current_repo_origin: str, target_repo: str) -> bool:
    """Публикация безопасна, только если origin чекаута, из которого запущен
    уборщик, — тот же репозиторий, что целевой канал записи (`GITHUB_REPOSITORY`).
    Песочницы (task-branch.test.sh, guard-тесты: origin — локальный bare-репозиторий,
    а GITHUB_REPOSITORY унаследован НАСТОЯЩИЙ) и форки отпадают ДО сети — иначе
    синтетические измерения легли бы в прод-канал и маскировали бы живое состояние
    до `RECORD_STALE_AFTER_DAYS`."""
    return bool(target_repo) and (
        normalize_owner_repo(current_repo_origin) == normalize_owner_repo(target_repo))


def count_stale_guard_copies(worktrees_base: Path) -> tuple[int, int]:
    """Замер из задачи #1250: сколько рабочих деревьев несут
    `scripts/orchestra/pulse_guard.py` и сколько из них — версию БЕЗ гвардии
    #1074 (`prod_writes_allowed`). Возвращает (копий_всего, устаревших).
    Читает файлы как они лежат на диске — это и есть носитель исполняемого
    кода гвардий, ради которого замер существует."""
    total = stale = 0
    if not worktrees_base.is_dir():
        return 0, 0
    for entry in sorted(worktrees_base.iterdir()):
        if not entry.is_dir():
            continue
        guard = entry / "scripts" / "orchestra" / "pulse_guard.py"
        if not guard.is_file():
            continue
        total += 1
        try:
            text = guard.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # Нечитаемый файл — не доказательство «гвардия есть»; честнее
            # посчитать копию устаревшей, чем спрятать её из замера.
            stale += 1
            continue
        if "prod_writes_allowed" not in text:
            stale += 1
    return total, stale


# ── I/O: публикация (тонкая обвязка, только сеть) ─────────────────────────

def publish_snapshot_record(
    repo_root: str,
    record: dict,
    target_repo: str,
    *,
    measure_base: Optional[Path] = None,
    origin: Optional[str] = None,
    is_duplicate: Optional[Callable[[str], bool]] = None,
    log: Callable[[str], None] = print,
) -> bool:
    """Записать снимок на DATA_BRANCH через data_branch_writer.append_and_push.
    Возвращает True — записано и запушено; False — публикация НЕ состоялась,
    причина НАПЕЧАТАНА (fail loud в лог прогона), исключение наружу не идёт:
    публикация — телеметрия, а не гейт уборки (task-branch зовёт уборщик
    best-effort), а смерть канала — нарушение инварианта 24, не повод блокировать
    создание ветки задачи. Гейты в порядке объявления (порядок — решение, не
    случайность): (1) цель записи задана; (2) БЕЗОПАСНОСТЬ канала — origin
    чекаута тот же репозиторий, что целевой: песочница/форк не публикует,
    даже когда измерять нечего; (3) наличие объекта измерения: `measure_base`
    задан, но не существует — «деревьев нет здесь» печатается фактом, а не
    подменяется записью с нулями, маскирующей живое состояние других чекаутов.

    `origin` — инъекция для тестов (по умолчанию data_branch_writer.origin_url(),
    т.е. GITHUB_REPOSITORY); `is_duplicate` — инъекция антишума (по умолчанию
    свежий факт с сервера через is_duplicate_observation, тот же контракт, что
    у pipeline_health._snapshot_is_duplicate: решение по содержимому ПОСЛЕ
    громкого fetch/checkout, не по устаревшей локальной копии)."""
    def skip(reason: str) -> bool:
        log(f"worktree-snapshot: запись не публикуется — {reason}")
        return False

    if not target_repo:
        return skip("GITHUB_REPOSITORY не задан (вне GitHub Actions) — некуда публиковать")

    if origin is None:
        origin = data_branch_writer.origin_url()

    # RAW-URL из конфига, не `git remote get-url`: get-url применяет
    # url.<base>.insteadOf-переписывание, и чекаут с легальным вместо-of
    # (зеркала, тестовые песочницы) выглядел бы «чужим» origin, хотя
    # хранит прод-URL. Сравнивать надо то, что ПОЛОЖЕНО в remote.origin.url.
    remote_out = subprocess.run(
        ["git", "-C", repo_root, "config", "remote.origin.url"],
        capture_output=True, text=True, encoding="utf-8")
    if remote_out.returncode != 0:
        return skip(f"origin этого чекаута не читается: {remote_out.stderr.strip()[:200]}")
    if not publisher_is_safe(remote_out.stdout.strip(), target_repo):
        return skip(
            f"origin этого чекаута ({normalize_owner_repo(remote_out.stdout.strip())}) "
            f"не совпадает с целевым репозиторием записи ({normalize_owner_repo(target_repo)}) "
            "— песочница или форк; измерение синтетического репозитория не должно "
            "попадать в прод-канал")

    if measure_base is not None and not measure_base.is_dir():
        return skip(".claude/worktrees в этом чекауте не существует: объект "
                    "измерения отсутствует (это факт «деревьев нет здесь», не "
                    "«наблюдение состоялось и чисто»)")

    workdir = tempfile.mkdtemp(prefix="worktree-snapshot-")
    try:
        # Свежий клон ветки данных (или main, если ветки ещё нет — путь
        # первого создания, #882 т.3): append_and_push пишет В подготовленный
        # чекаут, сам его не заводит (тот же контракт, что у pipeline_health.
        # snapshot_and_store: clone_data_branch → append_and_push).
        data_branch_writer.clone_data_branch(origin, workdir, DATA_BRANCH)
        if is_duplicate is None:
            def is_duplicate(current_text: str) -> bool:
                last = last_record(read_rows(current_text)) if current_text else None
                duplicate = is_duplicate_observation(last, record, now=datetime.now(timezone.utc))
                if duplicate:
                    log("worktree-snapshot: последняя запись уже несёт те же числа "
                        "в окне антишума — не дублирую")
                return duplicate

        written = data_branch_writer.append_and_push(
            workdir=workdir,
            data_branch=DATA_BRANCH,
            rel_path=SNAPSHOT_PATH,
            commit_identity=COMMIT_IDENTITY,
            commit_message=f"worktree snapshot {record['ts']}",
            is_duplicate=is_duplicate,
            render_next=lambda current: current + json.dumps(
                record, ensure_ascii=False, sort_keys=True) + "\n",
        )
    except Exception as error:  # noqa: BLE001 — лучший доступный канал: печать с причиной
        log(f"ПРЕДУПРЕЖДЕНИЕ: worktree-snapshot не записан ({DATA_BRANCH}): {error} — "
            "уборка продолжается без записи; молчание канала увидит инвариант 24 "
            "(repo_invariants), здесь это не гейт")
        return False
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if written:
        log(f"worktree-snapshot: запись опубликована на {DATA_BRANCH}:{SNAPSHOT_PATH}")
    return written
