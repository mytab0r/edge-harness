#!/usr/bin/env python3
"""
Автоуборка мёртвых worktree'ов (слитые/закрытые PR без работы).

Retention story (находка ревью PR #893, второй раунд: докстринг раньше
обещал «24 часа после слияния PR», а код держал другое число и мерил
другое событие — три разных ответа на один вопрос): worktree удаляется не
раньше `retention_hours` (по умолчанию 1 час, `WorktreeAnalyzer.__init__`)
С МОМЕНТА ПОСЛЕДНЕГО ИЗМЕНЕНИЯ КОРНЕВОГО КАТАЛОГА дерева (`os.stat(...)
.st_mtime` в `get_worktree_age_hours`) — не с момента слияния PR (это
событие здесь не читается вовсе, у него нет отдельного часового пояса
проверки). Честно: это прокси, не точное время слияния, и он может
отставать или опережать реальный мерж на часы — единственная причина, по
которой он безопасен, это то, что retention НЕ единственный гейт: дерево
всё равно не снимется, пока PR не перестанет быть `open` (см. условие 3
ниже) — то есть retention отсекает только «слишком свежее», финальное
решение «уже можно» несёт статус PR, не mtime.
Цель: освободить диск, не потеряв активную работу.

Безопасность:
  - Никогда не удаляет дерево с файлом-маркером `.worktree-keep`
  - Никогда не удаляет дерево с незакоммиченными изменениями
  - Никогда не удаляет дерево, для которого не ДОКАЗАНО, что коммиты
    существуют где-то ещё — origin-ветка синхронна, ИЛИ содержимое HEAD
    равно живому апстриму/origin/main напрямую, ИЛИ (последний рубеж) PR
    ветки доказанно merged (см. условие 2 — три яруса, честная граница
    названа там же)
  - Никогда не удаляет дерево открытого PR
  - Fail loud вместо молчаливого удаления — три исхода, не два (носитель
    `scripts/lib/check_result.py`, issue #1096): «доказано сохранено»,
    «доказано НЕ сохранено», «доказать не удалось». Третий исход НИКОГДА
    не читается как разрешение снять дерево.

Условия удаления (ВСЕ должны быть выполнены):
  0. Нет файла-маркера `.worktree-keep` в корне дерева — объявленный ручной
     способ защитить конкретное дерево (не хардкод имени/номера, см.
     `WorktreeAnalyzer.KEEP_MARKER_NAME`); --force НЕ отменяет
  1. Нет незакоммиченных изменений (git status --porcelain пусто); --force
     НЕ отменяет. Газ (issue #1250): закоммить и запушить (тогда решает
     условие 2), либо осознанно отбросить правки (`git checkout -- . &&
     git clean -fd`), либо поставить `.worktree-keep`, если дерево нужно
     оставить как есть надолго — сообщение отказа называет это явно.
  2. Работа доказанно существует где-то ещё, не только в этом дереве
     (`check_unpushed_commits`, --force НЕ отменяет никогда, ни один из
     трёх ярусов). Три яруса по убыванию силы сигнала, дословный докстринг
     метода — источник правды (три РЕАЛЬНЫХ раунда живого замера, не
     гипотеза):
       (а) классика: апстрим-ветка резолвится и локальный HEAD не впереди
           неё (`git rev-list --count @{u}..` == 0) — коммиты уже на origin;
       (б) squash-safe против ЖИВОГО апстрима: апстрим существует, но
           переписан (ребейз/force-push) — содержимое дерева файлов
           идентично `@{u}` напрямую (не sha-историю) — коммиты физически
           лежат на origin под другим sha;
       (в) squash-safe против main: апстрим полностью удалён — содержимое
           идентично `origin/main` напрямую;
       (г) ПОСЛЕДНИЙ рубеж, единственный, реально снимающий деревья на
           живых данных этого репозитория (issue #1250, раунды 1-2 дали
           `Removed: 0` на 73-76 живых деревьях БЕЗ этого яруса, включая
           заведомо merged #1109/#1027 — main и апстрим постоянно копят
           довески от других каналов перед слиянием, полное сравнение
           дерева файлов не масштабируется): (а)/(б)/(в) не доказали —
           доверяем статусу PR (`gh pr list`, уже кэширован условием 3):
           `merged` ⇒ ok(), иначе ⇒ violation. ЧЕСТНО НАЗВАННЫЙ ОСТАТОЧНЫЙ
           РИСК: коммит, сделанный ПОСЛЕ слияния PR и никогда никуда не
           запушенный, будет ошибочно принят за сохранённый — узкий,
           признанный, задокументированный компромисс (полный разбор —
           докстринг `check_unpushed_commits`), не молчаливый регресс.
       `unknown` — только когда сама команда сравнения физически не
       выполнилась технически (например `origin/main` не резолвится) ИЛИ
       статус PR не удалось получить на последнем рубеже — не когда
       сравнение прошло и показало расхождение (это `violation`).
  3. Associated PR не в статусе open (см. ниже про сигнал "жива ли ветка")
  4. Дерево старше retention_hours (по умолчанию 1 час, мерится по mtime
     КОРНЕВОГО каталога дерева — прокси, не точное время слияния PR, см.
     Retention story ниже; инъекция параметром — не жёсткая константа,
     чтобы тесты не зависели от системных часов)
  5. По опции --force пропускается ТОЛЬКО проверка 3 (статус PR как
     ОТДЕЛЬНЫЙ гейт "PR ещё open", debug-путь). Пункты 0, 1, 2, 4 обязательны
     ВСЕГДА и не читают `self.force` нигде в своём коде — --force никогда
     не может привести к потере незакоммиченной работы. Оговорка: условие 2
     САМО вызывает `get_pr_status` на своём последнем рубеже (г) — это
     НЕ то же самое, что проверка 3 (открыт ли PR прямо сейчас), и от
     `self.force` тоже не зависит; см. честную границу в условии 2 выше.

Почему сигнал не "ветка удалена на origin" (замер #891, живой прогон на
edge-harness): в этом репозитории оркестратор сливает PR, но НЕ удаляет
ветку на origin — из 128 живых деревьев 127 показывали "ветка ещё есть на
origin", включая ветки PR, смердженных месяцами ранее. Проверка по факту
присутствия ветки на origin делала бы уборку бессмысленной для этого
репозитория (0 кандидатов навсегда) — авторитетный сигнал "работа ещё не
закончена" здесь только статус PR (gh pr list), не факт существования ветки.

Каталоги без `.git` под `.claude/worktrees/` (issue #1250, пункт 5): этот
скрипт видит только то, что знает `git worktree list --porcelain` — обычный
каталог без `.git` там НЕ зарегистрирован как worktree и структурно не
может быть ни удалён, ни защищён этим кодом. Найденные при замере
(`432-gate1-decided`, `_scratch_pool`, `pr-workflow-sessions-8f1ab5`, все три
2026-09-14) не признаны ни мусором, ни чужой инфраструктурой — решение
`git worktree remove`/`rm -rf` этого скрипта не касается: происхождение
неизвестно (не текущий git worktree, не гарантированно неиспользуемый
scratch), удалять вслепую — риск потери чужой работы того же класса,
которого эта задача и избегает. `scan_orphan_directories()` только
ОТЧИТЫВАЕТСЯ о них построчно в сводке (не молчаливое игнорирование) —
решение, что с ними делать, за оператором.

Fail loud на «снято 0» (issue #1250, пункт 3): прогон, не снявший НИЧЕГО,
пока среди удержанных есть деревья СТАРШЕ retention с кодом из
`worktree_snapshot.STUCK_CODES` (unpushed/unknown_work/unknown_pr — газ у
них недостижим или внешненосителен), неотличим в логе от «убирать было
нечего» — именно так «0 из 73» и прополз мимо всех. `print_summary`
печатает отдельный блок с маркером `!!!` (грепается), называющий деревья
и их коды; смерть этого сигнала в масштабе репозитория видит инвариант 24
(`repo_invariants.check_worktree_cleanup_records`).

Канал наблюдаемости (issue #1250, пункт 4): `--publish-snapshot` (передаёт
прод-вызывающий `scripts/git/task-branch`) публикует запись прогона
(total/removed/устаревшие копии гвардий/запертые деревья) на ветку данных
`data/worktree-cleanup` (`scripts/lib/worktree_snapshot.py` — состав записи
и транспорт, второй копии не заводим). Измерение живёт ЗДЕСЬ, где деревья
физически есть, — инвариант на раннере repo-ci/orchestra видит пустой
`.claude/worktrees` всегда и мерять не может. Публикация best-effort:
отказ (сеть/права/песочница) печатается с причиной и НЕ ломает уборку —
смерть канала ловит инвариант по свежести записей, а не красный task-branch.
"""

import subprocess
import os
import sys
import json
import base64
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Tuple, List

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

# --- check_result bootstrap (носитель третьего состояния, issue #1096/#1250) ---
_check_result_spec = importlib.util.spec_from_file_location(
    "check_result", Path(__file__).resolve().parent.parent / "lib" / "check_result.py")
check_result = importlib.util.module_from_spec(_check_result_spec)
_check_result_spec.loader.exec_module(check_result)
# --- конец check_result bootstrap ---

# --- worktree_snapshot bootstrap (канал наблюдаемости для инварианта 24, #1250) ---
_worktree_snapshot_spec = importlib.util.spec_from_file_location(
    "worktree_snapshot", Path(__file__).resolve().parent.parent / "lib" / "worktree_snapshot.py")
worktree_snapshot = importlib.util.module_from_spec(_worktree_snapshot_spec)
_worktree_snapshot_spec.loader.exec_module(worktree_snapshot)
# --- конец worktree_snapshot bootstrap ---


def run_cmd(cmd: str, cwd: Optional[str] = None, check: bool = False) -> Tuple[str, int]:
    """Выполнить команду, вернуть (output, returncode)"""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=cwd,
            shell=True,
            timeout=30
        )
        return result.stdout.strip(), result.returncode
    except subprocess.TimeoutExpired:
        if check:
            raise RuntimeError(f"Command timeout: {cmd}")
        return "TIMEOUT", 1
    except Exception as e:
        if check:
            raise RuntimeError(f"Command failed: {cmd}\n{e}")
        return str(e), 1


class WorktreeAnalyzer:
    def __init__(
        self,
        repo_root: str,
        force: bool = False,
        verbose: bool = False,
        retention_hours: float = 1,
        now_ts: Optional[float] = None,
    ):
        self.repo_root = repo_root
        self.force = force
        self.verbose = verbose
        self.retention_hours = retention_hours
        # Инъекция «текущего времени» (не time.time()/datetime.now() напрямую
        # внутри get_worktree_age_hours) — тесты передают now_ts явно, чтобы
        # не зависеть от скорости выполнения и системных часов (класс
        # «тесты-бомбы», AGENTS.md). None — боевой путь, берём реальное время.
        self.now_ts = now_ts
        # Кэш статусов PR — один batched запрос на весь прогон, не один
        # запрос на дерево (класс: раньше get_pr_status дёргал `gh pr list`
        # ПЕРСОНАЛЬНО на каждое дерево; после того как PR-статус стал
        # обязательным гейтом почти для всех кандидатов — это десятки/сотни
        # последовательных сетевых вызовов на один прогон, минуты и квота
        # API. Один пакетный запрос ~5с против сотен по ~1с, замер #891).
        # Он же переиспользуется условием 2 (squash-safe признак, #1250) —
        # второй пакетный запрос НЕ заводится, кэш общий.
        self._pr_status_cache: Optional[dict] = None
        self._pr_status_cache_ok: bool = False

    def get_worktrees(self) -> List[dict]:
        """Получить список всех worktree'ов"""
        output, rc = run_cmd("git worktree list --porcelain", cwd=self.repo_root)
        if rc != 0:
            raise RuntimeError(f"Failed to list worktrees: {output}")

        worktrees = []
        current = {}

        for line in output.split('\n'):
            if line.startswith('worktree '):
                if current:
                    worktrees.append(current)
                current = {'path': line.replace('worktree ', '').strip()}
            elif line.startswith('HEAD '):
                current['head'] = line.replace('HEAD ', '').strip()
            elif line.startswith('branch '):
                current['branch'] = line.replace('branch ', '').strip()
            elif line.startswith('detached'):
                current['branch'] = 'DETACHED'

        if current:
            worktrees.append(current)

        # Filter to task worktrees only
        return [
            w for w in worktrees
            if '.claude/worktrees' in w.get('path', '')
        ]

    def branch_name(self, branch_ref: str) -> str:
        """Извлечь имя ветки из ref"""
        if branch_ref.startswith('refs/heads/'):
            return branch_ref.replace('refs/heads/', '')
        return branch_ref

    def check_dirty(self, worktree_path: str) -> bool:
        """Проверить наличие незакоммиченных изменений"""
        output, rc = run_cmd('git status --porcelain', cwd=worktree_path)
        if rc != 0:
            return True  # Если не смогли проверить — считаем грязным
        return bool(output.strip())

    @staticmethod
    def _diff_state(spec: str, cwd: str) -> Tuple[str, str]:
        """("same"|"diverged"|"failed", диагностика) для `git diff --quiet A B`.

        Отказ инструмента обязан читаться НЕ тем же сигналом, что семантический
        ответ (находка ревью PR #1257, чеклист): run_cmd при таймауте/исключении
        возвращает rc=1 с output "TIMEOUT"/текстом исключения — тот же rc, что у
        `git diff --quiet` в семантике «различия есть». Сам diff --quiet в обоих
        семантических исходах печатает в stdout ПУСТО, поэтому rc==1 с непустым
        stdout — отказ инструмента: "failed" (вызывающий код обязан дать
        unknown(), не content_diverged)."""
        output, rc = run_cmd(spec, cwd=cwd)
        if rc == 0:
            return "same", ""
        if rc == 1 and not output.strip():
            return "diverged", ""
        return "failed", (
            f"{spec} завершился кодом {rc}"
            + (f", stdout={output[:120]}" if output.strip() else "")
        )

    def check_unpushed_commits(self, worktree_path: str, branch: str) -> "check_result.CheckResult":
        """Доказана ли сохранность коммитов где-то ещё, кроме этого дерева.

        Три исхода (`check_result.CheckResult`, issue #1096/#1250), не bool:
          - ok(): доказано сохранено — апстрим синхронен (классика), ИЛИ
            содержимое HEAD равно текущему апстриму/main, ИЛИ PR ветки
            доказанно merged (см. три яруса ниже).
          - violation([...]): ни один ярус не доказал сохранность, а PR
            доказанно НЕ merged (open/closed) — содержимое НЕ доказано
            сохранённым нигде.
          - unknown(reason): ни один ярус физически не выполнился и статус
            PR тоже не удалось получить — вызывающий код обязан трактовать
            это как отказ (fail loud), не как "можно".

        ЖИВОЙ ЗАМЕР (issue #1250, три прогона на реальных 73-76 деревьях)
        заставил пересмотреть эту функцию дважды — оба раза честно, не
        задним числом:

        Раунд 1 (чистое сравнение дерева файлов с origin/main, без статуса
        PR вовсе): технически корректно отличает "мой вклад совпадает с
        main" от "не совпадает", но НЕПРИМЕНИМО на практике — main
        непрерывно копит чужие изменения от всех остальных слитых PR,
        поэтому полное сравнение дерева почти ВСЕГДА показывает расхождение
        для ветки старше пары часов, даже если её СОБСТВЕННЫЙ вклад давно
        слит. Живой замер: `Removed: 0` из 75 деревьев — фикс не снимал
        НИ ОДНОГО дерева, включая заведомо смёрженные (#1109 PR #1140
        MERGED, #1027 PR #1030 MERGED) — оба диффят и от main, И от
        собственного живого апстрима (см. ниже), потому что апстрим этих
        веток сам получил дополнительные ревью-фиксапы от другого канала
        ДО финального слияния — содержимое локальной копии всегда меньше
        итогового, а доказать "моё — подмножество итогового" дёшево
        технически невозможно (сравнение деревьев различает "равно"/
        "не равно", не "подмножество").

        Раунд 2 (двухъярусное сравнение содержимого — с живым апстримом,
        затем с main, без статуса PR): решает узкий частный случай "просто
        переписанный апстрим той же самой правкой" (`mechanical_rebase.py
        --force-with-lease`), но НЕ решает основной живой случай выше
        (апстрим/main получили ДОПОЛНИТЕЛЬНЫЕ изменения, не только
        переписывание) — на тех же 75 деревьях всё ещё `Removed: 0`.

        Раунд 3 (этот код): сравнение содержимого остаётся ПЕРВЫМ и
        предпочтительным сигналом (два яруса ниже, сильные и узкие — не
        нуждаются в сети), но когда оба яруса показали расхождение,
        последним рубежом становится статус PR (`gh pr list`, уже кэширован
        для условия 3) — GitHub authoritative: если PR ветки доказанно
        `merged`, ВСЁ его финальное содержимое (каким бы оно ни стало к
        моменту слияния — с любыми довесками от любого канала) уже в main,
        и локальная копия, отличающаяся от main, просто СТАРЕЕ финальной
        версии, а не содержит НЕЧТО, чего нет нигде. `violation` наступает,
        только когда содержимое отличается И PR доказанно НЕ merged
        (open/closed) — это по-прежнему блокирует реальный "рабочий" случай
        (см. `can_remove_worktree`, условие 3, где `open` уже гейтит отдельно
        задолго до этого).

        ЧЕСТНО НАЗВАННЫЙ ОСТАТОЧНЫЙ РИСК (issue #1250, «не потеряй работу» —
        главное ограничение задачи, обсуждалось явно, не по умолчанию):
        коммит, сделанный ПОСЛЕ того, как PR этой же ветки уже смёржен, и НИ
        РАЗУ не запушенный никуда, окажется классифицирован как `ok()` —
        `pr_status == 'merged'` побеждает расхождение содержимого без
        дальнейшей проверки. Условие 1 (`check_dirty`) по-прежнему абсолютно
        и ловит НЕЗАКОММИЧЕННЫЕ хвосты такой работы; не ловит ЗАКОММИЧЕННЫЙ
        локальный хвост поверх уже смёрженного PR. В операционной модели
        этого репозитория (`AGENTS.md`: «Закрытая задача не переоткрывается
        никогда», ветки переиспользуются после CLOSED, не после MERGED) это
        осознанный редкий анти-паттерн, не типичный путь — но он РЕАЛЕН и
        назван здесь явно, а не спрятан. Альтернатива (не доверять
        merged-статусу вовсе) была опробована (раунды 1–2 выше) и не даёт ни
        одного снятого дерева на живых данных — тормоз без названного газа
        хуже, чем узкий названный риск.

        Без "2>/dev/null" — run_cmd уже вызывается с capture_output=True
        (stderr идёт в result.stderr, не на консоль), а сама редирекция вида
        "2>/dev/null" ломает команду под shell=True на нативном Windows
        cmd.exe (нет /dev/null): rc становится ненулевым ВСЕГДА, и это
        дерево навсегда считается "опасным" (баг найден поведенческим
        тестом на реальном git-репозитории, не текстовой гвардией, #891).
        """
        content_diverged = False

        output, rc = run_cmd('git rev-list --count @{u}..', cwd=worktree_path)
        if rc == 0:
            try:
                ahead = int(output.strip() or 0)
            except ValueError:
                ahead = None
            if ahead == 0:
                return check_result.ok()
            # ahead>0 (или не распарсилось) — апстрим есть, но локально
            # впереди него. Ярус 1: сверяем содержимое с ЖИВЫМ апстримом —
            # сильный, узкий сигнал (переписанная той же правкой ветка).
            # (Огрех самой команды rev-list здесь не различается с «апстрим
            # не резолвится» — не страшно: упавший git проявит себя на
            # следующем же шаге в _diff_state как "failed" → unknown().)
            state, diag = self._diff_state('git diff --quiet HEAD @{u}', worktree_path)
            if state == "same":
                return check_result.ok()
            if state == "diverged":
                content_diverged = True
            else:
                return check_result.unknown(
                    f"{diag} — не удалось сравнить дерево с апстримом в "
                    f"{worktree_path} (отказ инструмента, не ответ «разошлось»)"
                )
        else:
            # Апстрим не резолвится вовсе (ветка удалена на origin) — ярус 2:
            # сравнение с origin/main напрямую (слабее яруса 1, но лучше,
            # чем ничего, когда апстрима больше нет вовсе).
            state, diag = self._diff_state('git diff --quiet HEAD origin/main', worktree_path)
            if state == "same":
                return check_result.ok()
            if state == "diverged":
                content_diverged = True
            else:
                return check_result.unknown(
                    f"{diag} — не удалось сравнить дерево с main в "
                    f"{worktree_path} (отказ инструмента, не ответ «разошлось»)"
                )

        # Оба доступных яруса содержимого показали расхождение — ярус 3
        # (последний рубеж, issue #1250 раунд 3, см. докстринг выше):
        # доверяем статусу PR как единственному сигналу, переживающему живой
        # паттерн "апстрим получил довески от другого канала перед слиянием".
        assert content_diverged
        pr_status = self.get_pr_status(branch)
        if pr_status is None:
            return check_result.unknown(
                f"содержимое ветки {branch} отличается и от апстрима/main, а "
                f"статус PR определить не удалось ({self._pr_lookup_diagnosis(branch)})"
            )
        if pr_status == 'merged':
            return check_result.ok()
        return check_result.violation([
            f"содержимое ветки {branch} отличается от апстрима/main, а PR "
            f"не merged (status={pr_status}) — коммиты не доказаны "
            "сохранёнными нигде"
        ])

    def get_worktree_age_hours(self, worktree_path: str) -> float:
        """Получить возраст worktree'а в часах (по времени последнего доступа)"""
        try:
            stat = os.stat(worktree_path)
            now = self.now_ts if self.now_ts is not None else datetime.now().timestamp()
            age_seconds = now - stat.st_mtime
            return age_seconds / 3600
        except:
            return 0

    # Приоритет статуса при нескольких PR на одну ветку (находка ревью PR
    # #893, второй раунд): ветка `agent/<N>-<slug>` в этом репозитории
    # детерминирована номером задачи и переиспользуется при перезапуске
    # задачи после закрытого PR — на одну ветку может существовать и старый
    # closed/merged PR, и новый open. `gh pr list` отдаёт записи по created
    # desc, без гарантии порядка для нашей цели; "кто последний в JSON
    # выигрывает" молча пропускал гейт "PR открыт", когда open была НЕ
    # последней записью. `open` обязан побеждать любой другой статус —
    # ветка с хоть одним живым PR не мертва, независимо от того, сколько
    # закрытых/слитых PR на неё было раньше.
    _PR_STATUS_PRIORITY = {'open': 2, 'merged': 1, 'closed': 1}

    def _load_pr_status_cache(self) -> None:
        """Один пакетный `gh pr list --state all` на весь прогон (не на
        дерево) — см. комментарий в __init__. `--json headRefName,state`,
        сверка ТОЧНЫМ именем ветки (не `--search "head:<префикс>"`: GitHub
        `head:` в `--search` матчит ПОДСТРОКОЙ — живой замер #891, `--search
        "head:agent/1"` вернул 30 посторонних веток agent/170-…/agent/140-…/
        agent/131-… и т. д., ни одна не agent/1-*).

        Агрегация по ветке — приоритет `open` (см. `_PR_STATUS_PRIORITY`), не
        порядок записи в ответе API."""
        cmd = 'gh pr list --state all --json headRefName,state --limit 2000'
        output, rc = run_cmd(cmd, cwd=self.repo_root)
        cache: dict = {}
        ok = False
        if rc == 0 and output:
            try:
                for item in json.loads(output):
                    ref = item.get('headRefName')
                    state = item.get('state')
                    if not ref or not state:
                        continue
                    state = state.lower()
                    existing = cache.get(ref)
                    if existing is None or self._PR_STATUS_PRIORITY.get(
                        state, 0
                    ) > self._PR_STATUS_PRIORITY.get(existing, 0):
                        cache[ref] = state
                ok = True
            except (ValueError, TypeError, AttributeError):
                ok = False
        self._pr_status_cache = cache
        self._pr_status_cache_ok = ok

    def get_pr_status(self, branch: str) -> Optional[str]:
        """Получить статус PR по ТОЧНОЙ ветке (merged/closed/open/not-found).
        None — либо PR по этой ветке не найден, либо весь пакетный запрос
        не удался (сеть/gh недоступен) — вызывающий код (can_remove_worktree)
        обязан трактовать None как отказ, не как "можно удалять" (fail loud).
        Различие между "не найден" и "сеть недоступна" — `_pr_lookup_diagnosis`.
        """
        if not branch.startswith('agent/'):
            return None
        if self._pr_status_cache is None:
            self._load_pr_status_cache()
        if not self._pr_status_cache_ok:
            return None
        return self._pr_status_cache.get(branch)

    def _pr_lookup_diagnosis(self, branch: str) -> str:
        """Человекочитаемая причина, ПОЧЕМУ `get_pr_status` вернул None —
        три разных факта, а не один общий "не определилось" (issue #1250,
        пункт 2: третье состояние обязано называть, что с ним делать):
          - ветка не `agent/*` — поиск PR структурно неприменим (например
            DETACHED HEAD) — это состояние НАВСЕГДА и это ожидаемо;
          - `gh pr list` в этом прогоне не удался — временное состояние,
            следующий прогон может его снять сам;
          - `gh pr list` отработал, но PR с такой веткой не найден вообще —
            ветка либо никогда не публиковалась, либо PR был удалён без
            merge/close, а не просто "статус неизвестен" — требует решения
            оператора, само не рассосётся.
        """
        if not branch.startswith('agent/'):
            return "ветка не agent/* — поиск PR по номеру задачи не применим"
        if self._pr_status_cache is None:
            self._load_pr_status_cache()
        if not self._pr_status_cache_ok:
            return "gh pr list --state all не удался в этом прогоне (сеть/токен недоступны) — повтори позже"
        if branch not in self._pr_status_cache:
            return "gh pr list не нашёл ни одного PR с этой веткой — PR либо не создавался, либо удалён без merge/close"
        return "статус определён"  # не должно вызываться в этом случае

    # Файл-маркер ручной защиты (известный хвост #891, живой случай: при
    # прогоне-замере на 133 деревьях пришлось РУКАМИ исключить
    # `749-ci-guard-catalog` и собственное рабочее дерево агента —
    # продакшн-скрипт не нёс механизма исключения вовсе). Объявленный
    # способ, не хардкод имени/номера дерева: любой оператор (человек или
    # агент), знающий, что дерево используется прямо сейчас, несмотря на то
    # что PR уже смёржен/закрыт (пример: доводка после мержа, общая
    # инфраструктура, на которую ссылаются другие незавершённые PR), кладёт
    # пустой файл `.worktree-keep` в КОРЕНЬ дерева — снимается тем же
    # оператором вручную, когда защита больше не нужна. Не gitignore'ится
    # намеренно НЕ проверяется (файл живёт вне git commit'а дерева, не
    # мешает check_dirty: сам факт наличия untracked-файла уже считается
    # "грязным" проверкой 1 ниже, так что маркер и без этой проверки уже
    # защищает дерево, — но это ПОБОЧНЫЙ эффект, не контракт: команда для
    # снятия защиты хочет и коммитить чистое дерево, и не терять защиту,
    # поэтому маркер проверяется явным отдельным условием ДО check_dirty).
    KEEP_MARKER_NAME = ".worktree-keep"

    def has_keep_marker(self, worktree_path: str) -> bool:
        return (Path(worktree_path) / self.KEEP_MARKER_NAME).exists()

    def can_remove_worktree(self, worktree_info: dict) -> Tuple[bool, str, str]:
        """
        Проверить, безопасно ли удалять worktree.
        Возвращает (can_remove, reason_code, reason_message). reason_code —
        машиночитаемый ключ статистики (семантика, не парсинг подстроки
        сообщения — AGENTS.md «Семантика важнее подстроки»); reason_message
        — человекочитаемое сообщение, обязано называть газ (что сделать,
        чтобы дерево стало снимаемым), кроме кодов, где газ не нужен (PR
        сам смёржится/закроется, retention сам истечёт).
        """
        path = worktree_info['path']
        branch = self.branch_name(worktree_info.get('branch', 'unknown'))

        # Проверка 0: ручная защита файлом-маркером — --force НЕ отменяет,
        # ровно как и остальные проверки безопасности ниже (--force снимает
        # ТОЛЬКО проверку статуса PR, см. проверку 3).
        if self.has_keep_marker(path):
            return False, "protected", (
                f"Protected by {self.KEEP_MARKER_NAME} marker — газ: удалить файл "
                f"{self.KEEP_MARKER_NAME} из корня дерева, когда защита больше не нужна."
            )

        # Проверка 1: наличие незакоммиченных изменений — --force НЕ отменяет
        if self.check_dirty(path):
            return False, "dirty", (
                "Has uncommitted changes — газ: закоммить и запушить (дальше решает "
                "проверка сохранности коммитов), либо осознанно отбросить правки "
                "(git checkout -- . && git clean -fd) и прогнать уборщик снова, либо "
                f"поставить {self.KEEP_MARKER_NAME}, если дерево нужно оставить как есть."
            )

        # Проверка 2: работа доказанно существует где-то ещё — --force НЕ
        # отменяет (squash-safe признак, issue #1250, см. докстринг файла).
        work = self.check_unpushed_commits(path, branch)
        if work.status == check_result.STATUS_VIOLATION:
            return False, "unpushed", (
                "Has unpushed commits (" + "; ".join(work.violations) + ") — газ: "
                "запушить ветку на origin, или открыть/дождаться merge PR, чтобы "
                "работа существовала хотя бы в одном месте, кроме этого дерева."
            )
        if work.status == check_result.STATUS_UNKNOWN:
            return False, "unknown_work", (
                f"Could not determine if work is preserved ({work.reason}) — газ: "
                "свериться руками (git -C <дерево> diff HEAD origin/main, gh pr list "
                "--head <ветка>) и снять дерево вручную (git worktree remove --force "
                "<путь>), если сохранность подтвердится."
            )

        # Проверка 3: статус PR — единственная проверка, пропускаемая
        # --force (debug-путь). Не "ветка есть на origin": оркестратор этого
        # репозитория не удаляет ветку после слияния (см. докстринг файла).
        # Не смогли определить статус (сеть/gh недоступен, PR не найден) —
        # отказ, а не молчаливое разрешение (fail loud, не silent-wrong).
        if not self.force:
            pr_status = self.get_pr_status(branch)
            if pr_status is None:
                diag = self._pr_lookup_diagnosis(branch)
                return False, "unknown_pr", (
                    f"Could not determine PR status ({diag}) — keeping to be safe."
                )
            if pr_status == 'open':
                return False, "open_pr", "Associated PR is still open"

        # Проверка 4: retention (дерево достаточно старое) — не зависит от --force
        age_hours = self.get_worktree_age_hours(path)
        if age_hours < self.retention_hours:
            return False, "young", f"Too young (age: {age_hours:.1f}h < {self.retention_hours}h retention)"

        return True, "ok", "Safe to remove"

    def scan_orphan_directories(self) -> List[str]:
        """Каталоги под `.claude/worktrees/`, о которых `git worktree list`
        НЕ знает (обычно — нет `.git`, не настоящий worktree). Этот скрипт
        их не удаляет и не защищает — структурно ограничен тем, что видит
        git (issue #1250, пункт 5, см. докстринг файла целиком). Единственная
        обязанность — не молчать про них."""
        base = Path(self.repo_root) / ".claude" / "worktrees"
        if not base.is_dir():
            return []
        known_paths = set()
        for wt in self.get_worktrees():
            try:
                known_paths.add(str(Path(wt['path']).resolve()))
            except OSError:
                known_paths.add(wt['path'])
        orphans = []
        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            try:
                resolved = str(entry.resolve())
            except OSError:
                resolved = str(entry)
            if resolved in known_paths:
                continue
            orphans.append(entry.name)
        return orphans

    def remove_worktree(self, worktree_path: str) -> bool:
        """Удалить worktree, вернуть True если успешно"""
        try:
            output, rc = run_cmd(f'git worktree remove "{worktree_path}"', cwd=self.repo_root)
            if rc != 0:
                print(f"ERROR: Failed to remove {worktree_path}: {output}", file=sys.stderr)
                return False
            return True
        except Exception as e:
            print(f"ERROR: Exception removing {worktree_path}: {e}", file=sys.stderr)
            return False

    def analyze_and_cleanup(self) -> dict:
        """Анализировать и очистить worktree'ы, вернуть статистику"""
        worktrees = self.get_worktrees()

        stats = {
            'total': len(worktrees),
            'removed': 0,
            'kept': 0,
            'protected': [],
            'dirty': [],
            'unpushed': [],
            'unknown_work': [],
            'open_pr': [],
            'unknown_pr': [],
            'young': [],
            'errors': [],
            'orphan_dirs': self.scan_orphan_directories(),
            # (ветка, код, возраст_часов) удержанных деревьев СТАРШЕ retention,
            # чей код — из worktree_snapshot.STUCK_CODES: носитель fail-loud
            # сводки (пункт 3 #1250) и записи снимка (пункт 4). Возраст считается
            # здесь, а не в can_remove_worktree, — к моменту отказа по кодам
            # unpushed/unknown_work/unknown_pr проверка retention ещё не
            # выполнялась (она ниже по коду).
            'stuck_old': [],
        }

        for wt in worktrees:
            path = wt['path']
            branch = self.branch_name(wt.get('branch', 'unknown'))

            can_remove, code, message = self.can_remove_worktree(wt)

            if can_remove:
                if self.verbose:
                    print(f"Removing: {branch}")
                if self.remove_worktree(path):
                    stats['removed'] += 1
                else:
                    stats['errors'].append((branch, "Remove failed"))
            else:
                stats['kept'] += 1
                if self.verbose:
                    print(f"Keeping: {branch} ({message})")
                stats.setdefault(code, []).append((branch, message))
                if code in worktree_snapshot.STUCK_CODES:
                    age_hours = self.get_worktree_age_hours(path)
                    if age_hours >= self.retention_hours:
                        stats['stuck_old'].append((branch, code, age_hours))

        return stats

    # Реестр разделов сводки — код категории (из can_remove_worktree),
    # заголовок и общий газ (что делать), если он один на всю категорию.
    # Семантика по коду, не парсинг подстроки текста сообщения (AGENTS.md
    # «Семантика важнее подстроки») — тот же принцип, что и в
    # can_remove_worktree/analyze_and_cleanup.
    _SECTIONS = [
        ("protected", "Protected (.worktree-keep)",
         f"Газ: удалить файл {KEEP_MARKER_NAME} из корня дерева, когда защита больше не нужна."),
        ("dirty", "Dirty (uncommitted changes)",
         "Газ: закоммить и запушить, либо осознанно отбросить правки "
         "(git checkout -- . && git clean -fd), либо поставить .worktree-keep."),
        ("unpushed", "Unpushed / unproven work",
         "Газ: запушить ветку на origin, или открыть/дождаться merge PR."),
        ("unknown_work", "Could not prove work is preserved",
         "Газ: свериться руками (git diff HEAD origin/main, gh pr list --head <ветка>) "
         "и снять вручную (git worktree remove --force), если сохранность подтвердится."),
        ("open_pr", "Open PR (not merged)",
         "Газ не нужен — дерево станет кандидатом само после слияния или закрытия PR."),
        ("unknown_pr", "Unknown PR status (kept to be safe)",
         "Газ: см. причину у каждой ветки — либо повтори прогон позже (gh был "
         "недоступен), либо реши руками (открыть PR или git worktree remove --force), "
         "если PR для этой ветки никогда не создавался."),
        ("young", "Too young (retention)",
         "Газ не нужен — дерево станет кандидатом само после окончания retention-окна."),
    ]

    def print_summary(self, stats: dict):
        """Вывести сводку"""
        print(f"\n=== WORKTREE CLEANUP SUMMARY ===")
        print(f"Total worktrees: {stats['total']}")
        print(f"Removed: {stats['removed']}")
        print(f"Kept: {stats['kept']}")

        for code, title, guidance in self._SECTIONS:
            entries = stats.get(code) or []
            if not entries:
                continue
            print(f"\n{title}: {len(entries)}")
            print(f"  {guidance}")
            for branch, _message in entries[:5]:
                print(f"  - {branch}")
            if len(entries) > 5:
                print(f"  ... and {len(entries) - 5} more")

        if stats.get('errors'):
            print(f"\nErrors: {len(stats['errors'])}")
            for branch, error in stats['errors']:
                print(f"  - {branch}: {error}")

        # Fail loud на «снято 0 при запертых деревьях» (issue #1250, пункт 3,
        # находка ревью PR #1257): «0 снято» в логе неотличимо от «убирать
        # было нечего» — именно так 0 из 73 и прополз мимо всех. Маркер `!!!`
        # в начале строк — грепаемый след прогона, который НИЧЕГО не снял,
        # хотя работа по снятию объективно есть.
        stuck_old = stats.get('stuck_old') or []
        if stats.get('removed', 0) == 0 and stuck_old:
            print(
                f"\n!!! ВНИМАНИЕ: снято 0 из {stats['total']} — {len(stuck_old)} "
                f"деревьев старше retention ({self.retention_hours}h) заперты "
                "без достижимого газа (issue #1250):"
            )
            for branch, code, age_hours in stuck_old[:10]:
                print(f"!!!   - {branch} [{code}] (возраст {age_hours:.0f}h)")
            if len(stuck_old) > 10:
                print(f"!!!   ... and {len(stuck_old) - 10} more")
            print(
                "!!! Газ: unpushed — запушить/слить PR ветки; unknown_work/unknown_pr — "
                "свериться руками (git diff HEAD origin/main, gh pr list --head) и снять "
                "(git worktree remove --force), если сохранность подтвердится. Смерть "
                "этого сигнала в масштабе репозитория видит инвариант 24 "
                "(repo_invariants.check_worktree_cleanup_records)."
            )

        orphans = stats.get('orphan_dirs') or []
        if orphans:
            print(f"\nNon-worktree directories under .claude/worktrees (no .git, not managed by this script): {len(orphans)}")
            print("  Не удаляются и не защищаются этим скриптом — происхождение не проверено, реши руками (issue #1250, п.5).")
            for name in orphans[:5]:
                print(f"  - {name}")
            if len(orphans) > 5:
                print(f"  ... and {len(orphans) - 5} more")


def publish_run_record(repo_root: str, stats: dict, analyzer: "WorktreeAnalyzer",
                       mode: str) -> None:
    """Опубликовать запись прогона на ветку данных `data/worktree-cleanup`
    (issue #1250, пункт 4; `--publish-snapshot` — только прод-вызывающий
    `scripts/git/task-branch`). Измерение устаревших копий гвардий и запертых
    деревьев живёт здесь, где деревья физически есть; состав записи —
    `worktree_snapshot.make_record`, транспорт — `data_branch_writer`.

    Дешёвый антидубль-гейт ДО клона ветки (один `gh api` GET вместо полного
    clone): те же числа в окне `RECORD_MIN_INTERVAL_MINUTES` — публиковать
    нечего. Любой сбой гейта (gh недоступен, файл ещё не существует) — НЕ
    повод пропустить запись: авторитетную проверку дубля `is_duplicate`
    писателя делает по факту с сервера после громкого fetch/checkout (#882).
    Порядок гейтов объявлен в publish_snapshot_record: безопасность канала
    (origin == целевой репозиторий) проверяется ДО наличия объекта измерения —
    песочница не публикует, даже когда измерять нечего."""
    base = Path(repo_root) / ".claude" / "worktrees"
    target_repo = os.environ.get("GITHUB_REPOSITORY", "")
    guard_copies, stale_guard = (
        worktree_snapshot.count_stale_guard_copies(base) if base.is_dir() else (0, 0))
    stuck_by_code = {code: 0 for code in worktree_snapshot.STUCK_CODES}
    for _branch, code, _age_hours in stats.get('stuck_old') or []:
        stuck_by_code[code] = stuck_by_code.get(code, 0) + 1
    record = worktree_snapshot.make_record(
        ts=datetime.now(timezone.utc).isoformat(),
        mode=mode,
        total=stats['total'],
        removed=stats['removed'],
        kept=stats['kept'],
        guard_copies=guard_copies,
        stale_guard_copies=stale_guard,
        stuck_old_by_code=stuck_by_code,
        retention_hours=analyzer.retention_hours,
    )
    if target_repo:
        out, rc = run_cmd(
            f'gh api "repos/{target_repo}/contents/{worktree_snapshot.SNAPSHOT_PATH}'
            f'?ref={worktree_snapshot.DATA_BRANCH}"',
            cwd=repo_root,
        )
        if rc == 0 and out:
            try:
                blob = json.loads(out)
                rows = worktree_snapshot.read_rows(
                    base64.b64decode(blob["content"]).decode("utf-8"))
                last = worktree_snapshot.last_record(rows)
                if worktree_snapshot.is_duplicate_observation(
                        last, record, now=datetime.now(timezone.utc)):
                    print("worktree-snapshot: последняя запись уже несёт те же "
                          "числа в окне антишума — не публикую")
                    return
            except (ValueError, KeyError, TypeError):
                pass  # нечитаемая история — решает авторитетная проверка писателя
    worktree_snapshot.publish_snapshot_record(repo_root, record, target_repo, measure_base=base)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Skip PR status check only (debug/offline path) — never skips '
             'the dirty/unproven-work/keep-marker safety checks (issue #1250)'
    )
    parser.add_argument(
        '--verbose',
        '-v',
        action='store_true',
        help='Verbose output'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Analyze only, do not remove'
    )
    parser.add_argument(
        '--publish-snapshot',
        action='store_true',
        help='Публиковать запись прогона на data-ветку %s (канал инварианта 24, '
             'issue #1250 п.4) — прод-путь scripts/git/task-branch; best-effort, '
             'не гейт уборки' % worktree_snapshot.DATA_BRANCH
    )

    args = parser.parse_args()

    # repo_root — от ТЕКУЩЕГО КАТАЛОГА ВЫЗОВА (os.getcwd()), не от
    # расположения самого файла скрипта (__file__). Разница критична: этот
    # скрипт вызывается из scripts/git/task-branch БЕЗ смены каталога — cwd
    # в проде это репозиторий агента, а в тестовом песочном прогоне
    # (scripts/git/test/task-branch.test.sh, `cd "$WORK/x-main" && ... bash
    # task-branch`) это ИЗОЛИРОВАННЫЙ временный git-репозиторий теста. Резолв
    # от __file__ проигнорировал бы песочницу и запустил бы настоящую уборку
    # по НАСТОЯЩЕМУ репозиторию разработчика при каждом прогоне теста
    # task-branch — обнаружено при подключении вызова (#891), не в проде.
    repo_root = os.getcwd()

    try:
        analyzer = WorktreeAnalyzer(repo_root, force=args.force, verbose=args.verbose)

        if args.dry_run:
            print("DRY RUN MODE - No worktrees will be removed\n")
            # Temporarily disable removal
            original_remove = analyzer.remove_worktree
            analyzer.remove_worktree = lambda path: True  # Mock removal

        stats = analyzer.analyze_and_cleanup()
        analyzer.print_summary(stats)

        if args.publish_snapshot:
            publish_run_record(repo_root, stats, analyzer,
                               mode="dry-run" if args.dry_run else "apply")

        # Exit with non-zero if there were errors
        if stats['errors']:
            sys.exit(1)

    except Exception as e:
        print(f"FATAL ERROR: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
