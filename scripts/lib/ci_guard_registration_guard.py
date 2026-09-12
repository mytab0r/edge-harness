#!/usr/bin/env python3
"""Гвардия механизма регистрации CI-гвардий (#749): попытка добавить шаг
гвардии рукописно в `.github/workflows/repo-ci.yml` обязана краснеть, а не
пройти молча — иначе класс «репо-ci.yml как генератор конфликтов» (замер
issue #749: 17 из 40 открытых PR правили один файл ради дописывания своего
`- name:`/`run:`) вернётся следующим же PR, забывшим про каталог
`scripts/ci/guards/`.

Устройство:
  1. `guard_step_names` читает job `test` `.github/workflows/repo-ci.yml`
     (yaml.safe_load, не текстовый греп) и возвращает имена шагов,
     распознанных как регистрация гвардии, ОБЪЕДИНЕНИЕМ двух независимых
     признаков (ревью PR #771, блокирующая 2 — только имя обходилось
     переименованием шага, живая мутация: «Проверка/Канарейка/Инвариант/
     Мутационный тест …» и шаг вовсе без `name:` давали `EXIT=0`, гвардия
     их не видела):
       а. соглашение именования (`Тест…`/`Гвардия…`/`Smoke…`/`Юнит-тест…`,
          якорь на начало строки, регистронезависимо) — тот же приём, что
          уже используют exec_bit_guard.py/orphan_test_guard.py;
       б. СОДЕРЖИМОЕ `run:` — шаг реально исполняет pytest/`node --test`/
          тестовый или guard-файл, вне зависимости от того, как назван —
          `_is_guard_registration_run` переиспользует
          `orphan_test_guard.statement_tokens` (тот же разбор run-текста на
          shell-statement'ы, что уже доказал себя для покрытия тестов,
          второй копии не заводим) и распознаёт: прямой `pytest`/`python -m
          pytest`, `node --test`, `bash`/`sh` тестового/guard-файла (тот же
          критерий «файл в директории test/ с известным расширением», что
          `orphan_test_guard.discover_test_files`) и прямой запуск
          `python …_guard.py`.
     Объединение (не замена «а» на «б»): признак «б» ловит переименованный
     обход, признак «а» сохраняет уже верно поименованные инфраструктурные
     inline-проверки (белые пятна, workflow-докс, concurrency), чей `run:`
     не вызывает отдельный файл теста/гвардии вовсе — как их обход
     содержимым не выразить, они и раньше матчились только именем.
  2. `ALLOWLIST` — замороженный список имён, остающихся рукописными шагами
     НА МОМЕНТ этого PR (#749: миграция механизма + одна гвардия как
     доказательство критерия приёмки; полная миграция остальных — отдельная
     задача, объём которой обязан убывать, см. proposal.md
     openspec/changes/ci-guard-catalog). Список синхронизируется РУКАМИ при
     каждой следующей миграции — та же форма, что уже доказала себя в
     `scripts/lib/test_label_registry.py`/`test_infra_gh_inventory.py`.
     `ALLOWLIST_RATCHET_MAX` — верхняя граница, доказывающая, что список
     может только убывать (ревью PR #771, major 5): без неё текст отказа
     сам предлагал более дешёвый обход «добавь имя в ALLOWLIST» вместо
     переноса гвардии в каталог.
  3. `check_no_undeclared_step` — двусторонняя сверка множества ИЗ ФАЙЛА
     против ALLOWLIST:
       - новое имя в файле, которого нет в ALLOWLIST → кто-то дописал
         гвардию рукописно вместо каталога (класс #749) — красный список с
         точным именем и подсказкой перенести в `scripts/ci/guards/`;
         для шага, чей `run:` сам вызывает файл каталога (класс обхода (б)
         из п.4), подсказка другая — перенести нечего, просто удали
         рукописный шаг (находка ревью PR #902, третий круг: общий совет
         «создай <имя>-guard.sh» здесь советовал бы обёртку, исполняющую
         гвардию дважды);
       - имя ALLOWLIST, которого больше нет в файле → запись устарела
         (шаг мигрирован/переименован/удалён) — тоже красный, чтобы
         ALLOWLIST не тащил мёртвые записи молча (тот же приём, что у
         `test_infra_gh_inventory.py`: мёртвая строка — тоже находка).
  4. `check_catalog_handwritten_overlap` (ревью PR #771, блокирующая 1) —
     сверка ПО СОДЕРЖИМОМУ, а не по имени: пункты 1–3 выше ловят только
     новый/устаревший рукописный шаг по имени/содержимому конкретного шага.
     Живой обход этого: (а) частичный перенос — файл гвардии кладут в
     `scripts/ci/guards/`, а старый рукописный шаг и его запись ALLOWLIST не
     трогают, гвардия исполняется дважды, и мутация-критерий #749 (удалить
     файл каталога → должно покраснеть) молча не срабатывает, потому что
     забытый рукописный шаг продолжает её гонять; (б) рукописный шаг под
     нейтральным именем прямо вызывает файл каталога
     (`run: bash scripts/ci/guards/<имя>.sh`) — `_is_guard_file_path` этот
     путь не ловит. `_extract_run_targets` вытаскивает из `run:` реально
     исполняемые файлы (аргументы pytest/`_guard.py`/`node --test`, файл
     `bash`/`sh`) — множество из каталога не должно пересекаться с
     множеством из ЛЮБОГО шага job `test` (включая ALLOWLIST-шаги: дубль
     может остаться именно под уже занесённым именем). `_is_guard_catalog_
     invocation` закрывает (б) отдельно — путь `scripts/ci/guards/` в `run:`
     рукописного шага считается регистрацией по содержимому вне зависимости
     от имени шага.

Запуск:
  python scripts/lib/ci_guard_registration_guard.py
  python -m pytest scripts/lib/test_ci_guard_registration_guard.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_CI = REPO_ROOT / ".github" / "workflows" / "repo-ci.yml"
GUARD_CATALOG_DIR = REPO_ROOT / "scripts" / "ci" / "guards"

# Тот же разбор run:-текста на shell-statement'ы, что уже доказал себя в
# orphan_test_guard.py (покрытие тестов workflow-шагами) — импорт по пути
# файла, не по пакету (в scripts/lib нет __init__.py), второй копии
# statement_tokens/TEST_DIR_EXTENSIONS/_JS_TEST_NAME_RE/_PY_TEST_RE не
# заводим (правило репозитория «одно место правды»).
_OTG_SPEC = importlib.util.spec_from_file_location(
    "orphan_test_guard", Path(__file__).resolve().parent / "orphan_test_guard.py"
)
_otg = importlib.util.module_from_spec(_OTG_SPEC)
_OTG_SPEC.loader.exec_module(_otg)  # type: ignore[union-attr]

# Якорь на начало строки — тот же приём, что уже используют
# exec_bit_guard.py/orphan_test_guard.py для похожего различения.
_GUARD_NAME_RE = re.compile(r"^(Тест|Гвардия|Smoke|Юнит-тест)", re.IGNORECASE)


def _is_guard_file_path(arg: str) -> bool:
    """`arg` — путь, реально указывающий на тестовый/guard-файл (не просто
    строка с похожим словом): тот же критерий, что `orphan_test_guard.
    discover_test_files` использует для распознавания тестов на файловой
    системе, применённый здесь к аргументу `run:`-команды."""
    path = Path(arg)
    if path.suffix in _otg.TEST_DIR_EXTENSIONS and path.parent.name == "test":
        return True
    if _otg._JS_TEST_NAME_RE.search(arg):
        return True
    if _otg._PY_TEST_RE.match(path.name):
        return True
    return False


def _is_guard_catalog_invocation(arg: str) -> bool:
    """`arg` указывает на файл каталога гвардий `scripts/ci/guards/`
    (ревью PR #771, блокирующая 1(б)): рукописный шаг под нейтральным
    именем, вызывающий файл каталога напрямую (`run: bash
    scripts/ci/guards/<имя>.sh`), — тоже регистрация по содержимому.
    `_is_guard_file_path` этот путь не ловит: критерий «файл в директории
    `test/`» не выполняется для `scripts/ci/guards/` (живая мутация ревью:
    полный repo-ci.yml с дописанным таким шагом под именем «Проверка
    окружения» давал `check_no_undeclared_step` → 0 проблем, EXIT=0)."""
    return "scripts/ci/guards/" in arg.replace("\\", "/")


def _is_guard_registration_run(run_text: str) -> bool:
    """Содержимое `run:` реально регистрирует гвардию — pytest/`node
    --test`/тестовый-или-guard-файл, — вне зависимости от имени шага
    (ревью PR #771, блокирующая 2)."""
    for words in _otg.statement_tokens(run_text):
        if not words:
            continue
        head = words[0]
        if head == "pytest":
            return True
        if head in ("python", "python3"):
            if len(words) >= 3 and words[1] == "-m" and words[2] == "pytest":
                return True
            if any(arg.endswith("_guard.py") for arg in words[1:]):
                return True
        elif head == "node" and "--test" in words:
            return True
        elif head in ("bash", "sh") and len(words) >= 2 and (
            _is_guard_file_path(words[1]) or _is_guard_catalog_invocation(words[1])
        ):
            return True
    return False


def _extract_run_targets(run_text: str) -> set[str]:
    """Реальные файлы, которые исполняет `run_text` (аргументы pytest,
    `_guard.py`-аргументы, файл `bash`/`sh`, аргументы `node --test`) — «что
    физически исполняется», не имя шага. Нужно для перекрёстной сверки
    каталог↔рукописный шаг (ревью PR #771, блокирующая 1(а)): два места,
    исполняющие один и тот же файл, — двойная регистрация, даже если ни то
    ни другое не поймано детекцией имени/содержимого шага — например,
    рукописный шаг остался в ALLOWLIST под старым именем после частичного
    переноса той же гвардии в каталог."""
    targets: set[str] = set()
    for words in _otg.statement_tokens(run_text):
        if not words:
            continue
        head = words[0]
        if head == "pytest":
            targets.update(arg for arg in words[1:] if not arg.startswith("-"))
        elif head in ("python", "python3"):
            if len(words) >= 3 and words[1] == "-m" and words[2] == "pytest":
                targets.update(arg for arg in words[3:] if not arg.startswith("-"))
            else:
                targets.update(arg for arg in words[1:] if arg.endswith("_guard.py"))
        elif head == "node" and "--test" in words:
            idx = words.index("--test")
            targets.update(arg for arg in words[idx + 1:] if not arg.startswith("-"))
        elif head in ("bash", "sh") and len(words) >= 2:
            targets.add(words[1])
    return targets


def _catalog_targets(catalog_dir: Path = GUARD_CATALOG_DIR) -> dict[str, str]:
    """Отображение «исполняемый файл → имя файла каталога, его
    исполняющего», построенное по СОДЕРЖИМОМУ `scripts/ci/guards/*.sh»."""
    targets: dict[str, str] = {}
    if not catalog_dir.is_dir():
        return targets
    for path in sorted(catalog_dir.glob("*.sh")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for target in _extract_run_targets(text):
            targets.setdefault(target, path.name)
    return targets


def _handwritten_targets(repo_ci: Path = REPO_CI) -> dict[str, str]:
    """Отображение «исполняемый файл → имя рукописного шага», построенное по
    СОДЕРЖИМОМУ ЛЮБОГО шага job `test` — не только распознанного как гвардия
    по имени/содержимому: дубль может остаться под именем, уже занесённым в
    ALLOWLIST, сверка обязана включать и такие шаги тоже (ревью PR #771,
    блокирующая 1(а))."""
    doc = yaml.safe_load(repo_ci.read_text(encoding="utf-8")) or {}
    job = (doc.get("jobs") or {}).get("test") or {}
    targets: dict[str, str] = {}
    for step in job.get("steps") or []:
        run_text = step.get("run")
        if not isinstance(run_text, str):
            continue
        name = step.get("name") or "(без имени)"
        for target in _extract_run_targets(run_text):
            targets.setdefault(target, name)
    return targets


def check_catalog_handwritten_overlap(
    repo_ci: Path = REPO_CI, catalog_dir: Path = GUARD_CATALOG_DIR
) -> list[str]:
    """Множество файлов, исполняемых каталогом, не должно пересекаться с
    множеством, исполняемым рукописными шагами job `test` (ревью PR #771,
    блокирующая 1(а)): пересечение — частичный перенос, гвардия исполняется
    дважды, и удаление файла каталога (мутация-критерий #749) не красит
    ничего, потому что забытый рукописный шаг продолжает её гонять."""
    catalog = _catalog_targets(catalog_dir)
    handwritten = _handwritten_targets(repo_ci)
    problems: list[str] = []
    for target in sorted(set(catalog) & set(handwritten)):
        problems.append(
            f"двойная регистрация: {target!r} исполняется и каталогом "
            f"(scripts/ci/guards/{catalog[target]}), и рукописным шагом "
            f"{handwritten[target]!r} .github/workflows/repo-ci.yml — частичный "
            "перенос: убери рукописный шаг (и его запись ALLOWLIST, если есть), "
            "гвардия должна остаться только в каталоге"
        )
    return problems

# Замороженный список имён шагов job `test` .github/workflows/repo-ci.yml,
# остающихся рукописными на момент #749 (миграция механизма каталога +
# одна гвардия как доказательство критерия приёмки). Обновляй руками при
# каждой следующей миграции в scripts/ci/guards/ — число обязано убывать
# (условие issue #749, «оговорка про миграцию существующих 47/66/… шагов»).
ALLOWLIST = frozenset({
    # Девять записей ниже добавлены переходом детекции с «только имя» на
    # «имя ИЛИ содержимое run:» (ревью PR #771, блокирующая 2) — эти шаги
    # существовали в repo-ci.yml и раньше, детекция по имени их просто не
    # видела (обходной стиль именования). Правкой этого PR список честно
    # вырос с 67 до 76 — это исправление недосчёта, не рост долга.
    'DO: агрегат на горячем пути только за кэшем (#320/#321/#575)',
    'pre-commit отклоняет коммит в agent/<N>-* не из своего дерева',
    'task-branch берёт аренду задачи, не арендует дважды за транспортом',
    'Гвардии check_pr.py — аргумент --tree и размерный гейт (#90)',
    'Канарейка осиротевших тестов — живой снимок (#583)',
    'Квота GitHub API — ранняя проверка (инварианты)',
    'Мутационный тест гвардии литерала namespace',
    'Провайдер/модель LLM — одно место правды (vars), без зашитых дефолтов',
    'Рассинхрон тела задачи и графа зависимостей (#371)',
    'Тесты графа блокировок пула task_deps',
    'Тесты create_pool_issue — отказ до сетевого вызова при отсутствии task',
    'Тесты кампании dispatch-tail',
    'Тесты замера rows_read',
    'Тесты предохранителя конвейера orchestra',
    'Тесты гвардии waiting:owner (метка, варианты, эскалация)',
    'Тесты применения решения владельца (owner-decision)',
    'Гвардия синхронности формата кнопки Telegram (TS ↔ Python)',
    'Тесты инвариантов состояния репозитория',
    'Тесты автофикса архивации openspec/changes (#493/#506)',
    'Тесты гвардии бита исполнения',
    'Гвардия бита исполнения — живой снимок workflow (#510/#516)',
    'Тесты экономии диспатча wake_orchestra (#456)',
    'Тесты сборщика квот',
    'Тесты планировщика orchestra (логин в морду, архив сессий, петля состояния PR)',
    'Тесты механического ребейза конфликтных PR',
    'Тесты контракта PR ↔ задача',
    'Тесты детектора устойчивого простоя',
    'Тесты сигнала дрейфа пина апстрима (#134)',
    'Тесты сигнала ослабления защиты main (#370)',
    'Тесты гвардии «PR не заводится на эпик»',
    'Smoke «task-branch отказывает на эпике»',
    'Тесты меток-вердиктов ревью и выборочного подтягивания веток',
    'Тесты аренды задачи claim_task',
    'Тесты извлечения номера задачи task_ref',
    'Тесты исхода PR ветки pr_outcome',
    'Гвардия «резолвер PR → задача, не подстрока прозы»',
    'Гвардия пагинации — списочный ответ GitHub API без обхода страниц',
    'Гвардия «сырой stderr клиента модели без redact» (#743)',
    'Гвардия «газ метки достижим правкой тела PR»',
    'Гвардия пакетного менеджера standalone dsh-edge (#43)',
    'Тесты выбора свободной задачи free_task',
    'Гвардия разделения GitHub-токенов',
    'Гвардия реестра меток — у каждого тормоза-метки объявлен газ',
    'Гвардия инвентаря INFRA-GH.md — каждый workflow назван в таблице',
    'Тесты канарейки осиротевших тестов',
    'Гвардия паритета маскирования секретов (bash ↔ TS)',
    # 'Гвардия массовой установки секретов combo-router (#733)' мигрирована
    # в scripts/ci/guards/provider-secrets-import-guard.sh (#749) — не
    # ALLOWLIST-запись, ratchet ниже уменьшен вместе с этим (77 → 76).
    'Тесты AI-ревью (второй гейт)',
    'Гвардия гейта первого ревью ai-review.yml (#204)',
    'Тесты file_tasks.py — фильтр МАСШТАБ (#426)',
    'Тесты review_checklist.py — категория ЗАМЕЧАНИЕ (#462)',
    'Гвардия триггеров гейтов ревью (#208)',
    'Юнит-тесты плагина dsh-hands-streamer',
    'Юнит-тесты плагина plugin-manager',
    'Юнит-тесты общего разбора тела ошибки (plugins-src/shared)',
    'Юнит-тесты плагина runner-bridge',
    'Юнит-тесты логики инструментов интеграций',
    'Юнит-тесты клиентского бандла интеграций',
    'Юнит-тесты плагина provider-registry',
    'Гвардия «каталог плагинов не отравляет литерал namespace»',
    'Smoke task-branch — проверка на входе и рабочее дерево',
    'Smoke pr-create — Closes/Fixes/Resolves отклоняется до вызова gh (#496)',
    'Тесты паритета scripts/git/pr-create ↔ contract_check.py',
    'Smoke issue-create — issue без task отклоняется до вызова gh (#526)',
    'Тесты чистой логики duplicate_guard (#566)',
    'Smoke issue-create — похожий заголовок отклоняется до вызова gh (#566)',
    'Smoke issue-create — приоритет не объявлен явно отклоняется до вызова gh (#695)',
    'Smoke доводки PR — инструментарий из main, ветка PR отдельным worktree (#476)',
    'Smoke обёрток статусов журнала',
    'Тесты проверки рендера транскрипта (#131)',
    # Значение этого элемента — реальный факт YAML, не опечатка: имя шага в
    # repo-ci.yml несёт " #119)" ПОСЛЕ пробела, а YAML-парсер (тот же
    # yaml.safe_load, что использует эта гвардия) обрезает его как inline-
    # комментарий (`#` после пробела в незакавыченном plain-скаляре) — GitHub
    # Actions использует тот же парсер, поэтому реальное имя шага в UI прогона
    # тоже усечено. Не чинится этим PR (вне области #749), зафиксировано как
    # факт, а не подогнано под ожидание.
    'Smoke bash-клиентов на заглушках (класс Б1',
    'Smoke цепочки LLM-провайдеров (#727)',
    'Гвардия concurrency — не сериализовать разные джобы одной статической группой',
    'Гвардия «workflow из docs существует»',
    'Тесты гейта квоты rate_guard (#454)',
    'Тесты гвардии полноты карты документации',
    'Гвардия полноты карты документации — живой снимок (#670)',
    'Гвардия стыка suite и цепочки провайдеров (#215/#727)',
})

# Верхняя граница ALLOWLIST — «только вниз» (ревью PR #771, major 5): без
# неё текст отказа мог бы читаться как приглашение решить находку дописыванием
# строки во frozenset вместо переноса гвардии в каталог — дешевле, но не то,
# что просит #749. Обнови ЭТУ константу вниз при каждой следующей миграции
# (правка ALLOWLIST без сопутствующего уменьшения этого числа падает).
# 77 — не 76: ребейз PR #771 на main подхватил #732 (слит НЕЗАВИСИМО от
# этого PR, добавил «Гвардия стыка suite и цепочки провайдеров (#215/#727)»)
# — тот же класс, который эта гвардия и ловит, просто легитимный (новая
# гвардия появилась старым способом раньше, чем #749 успел заавтоматизировать
# весь job). Учтена в ALLOWLIST ниже, ratchet поднят РОВНО на один новый учёт,
# не как лазейка на будущее.
# PR #734 (#733, слит независимо, ПОСЛЕ заморозки списка на 77) дописал
# рукописный шаг вместо каталога — тот же класс, который гвардия ловит, не
# легитимный недосчёт. Правильный ответ — не поднять потолок до 78 (что
# было бы дешёвым обходом), а перенести шаг в scripts/ci/guards/
# provider-secrets-import-guard.sh и не добавлять его имя в ALLOWLIST —
# список остаётся ровно тем же (77 записей), потолок не двигается вовсе.
# PR #792 (#789, слит независимо, ПОСЛЕ той же заморозки) дописал ещё один
# рукописный шаг («Гвардия окна контекста env-provider (#789)») — тот же
# класс: живой прогон этого PR (34307740476, job test, шаг
# ci-guard-registration) покраснел ровно на этой находке. Перенесён в
# scripts/ci/guards/context-window-env-provider-guard.sh, имя в ALLOWLIST не
# добавлено — 77 записей и потолок не двигаются.
# При доводке (ребейз на main) этого же PR обнаружились ещё 11 рукописных
# шагов, слитых независимо ПОСЛЕ той же заморозки, тем же классом: «Тесты
# бенчмарка латентности и discovery model id провайдеров», «Тесты
# персистентного состояния квоты провайдеров», «Тесты сборщика снимка
# здоровья конвейера»/«Тесты детектора регрессии здоровья конвейера»/«Тесты
# само-аудита здоровья конвейера» (три шага одного PR — объединены в один
# файл каталога, т.к. проверяют один связанный механизм и делили один
# исходный комментарий), «Тесты гвардии кодировки stdout/subprocess
# (Windows)», «Гвардия разбора метки времени — новое место мимо общего
# хелпера», «Гвардия реестра использования LLM-провайдеров — таблица
# видимости», «Гвардия границы доверия — персистентная квота провайдеров»,
# «Гвардия pnpm для dsh plugin add (класс #83/#842)», «Гвардия быстрого
# провайдера Claude anthropic-oauth-pool (#838)», «Гвардия манифеста
# использования LLM-провайдеров (#823)». Каждый перенесён в свой файл
# scripts/ci/guards/, ни одно имя не добавлено в ALLOWLIST — 77 записей и
# потолок не двигаются. Живая иллюстрация тезиса issue #749: пока PR #771
# не слит, main продолжает копить рукописные шаги тем же способом, который
# эта задача закрывает.
ALLOWLIST_RATCHET_MAX = 77

# Инфраструктурные исключения — шаги job `test`, которые НИКОГДА не мигрируют
# в scripts/ci/guards/, потому что сами не гвардии (перебор каталога — не
# гвардия, а механизм её запуска; #749/design.md). Отдельное множество от
# ALLOWLIST (ревью PR #771, major 5: «инфраструктурные исключения вынеси
# отдельным множеством») — ALLOWLIST обязан убывать к нулю, это множество
# нет. Имя шага-перебора сознательно не начинается с
# Тест/Гвардия/Smoke/Юнит-тест и не исполняет pytest/node --test/guard-файл
# напрямую — обе детекции ниже и так его не видят; запись здесь фиксирует
# это фактом с тестом-регрессией (test_perebor_step_is_infra_exempt), а не
# оставляет неявным совпадением.
INFRA_EXEMPT_STEP_NAMES = frozenset({
    'Каталог гвардий scripts/ci/guards — перебор (#749)',
})


def guard_step_names(repo_ci: Path = REPO_CI) -> set[str]:
    """Имя шага попадает в результат по ЛЮБОМУ из двух признаков — соглашение
    именования ИЛИ содержимое `run:` (см. докстринг модуля) — иначе
    переименование шага в обход `_GUARD_NAME_RE` делает гвардию невидимой
    для всего механизма (ревью PR #771, блокирующая 2)."""
    doc = yaml.safe_load(repo_ci.read_text(encoding="utf-8")) or {}
    job = (doc.get("jobs") or {}).get("test") or {}
    names: set[str] = set()
    for step in job.get("steps") or []:
        name = step.get("name")
        if name in INFRA_EXEMPT_STEP_NAMES:
            continue
        run_text = step.get("run")
        by_name = bool(name) and bool(_GUARD_NAME_RE.match(name))
        by_content = isinstance(run_text, str) and _is_guard_registration_run(run_text)
        if not (by_name or by_content):
            continue
        if name:
            names.add(name)
        else:
            # Шаг без `name:` не может участвовать в ALLOWLIST по ключу —
            # синтетическая метка с фрагментом run: гарантированно не
            # совпадает ни с одной записью ALLOWLIST, поэтому такой шаг
            # ВСЕГДА проходит как «новый незарегистрированный» (ревью PR
            # #771, блокирующая 2 — «закрывает шаг без name:»), а не
            # пропадает из вида молча.
            snippet = next((line.strip() for line in run_text.splitlines() if line.strip()), "")
            names.add(f"(шаг без name:, run начинается с {snippet[:60]!r})")
    return names


def _step_run_text(repo_ci: Path, name: str) -> str | None:
    """`run:` шага `name` job `test` — или None, если шаг не найден/`run:` не
    строка. Нужно только для газа сообщения ниже (_suggest_guard_filename),
    второй копии этого разбора translate_repo_ci не заводит: этот геттер живёт
    здесь, а не в guard_step_translator.py, чтобы не тянуть его модуль на
    каждый вызов guard_step_names (см. _suggest_guard_filename — переносчик
    загружается лениво, только когда находка уже есть)."""
    doc = yaml.safe_load(repo_ci.read_text(encoding="utf-8")) or {}
    steps = ((doc.get("jobs") or {}).get("test") or {}).get("steps") or []
    for step in steps:
        if isinstance(step, dict) and step.get("name") == name:
            run_text = step.get("run")
            return run_text if isinstance(run_text, str) else None
    return None


def _load_guard_step_translator():
    """Ленивая загрузка guard_step_translator.py ТОЛЬКО ради имени файла в
    газе сообщения ниже (issue #897) — по пути файла, тот же приём, что этот
    модуль уже применяет для orphan_test_guard.py. Ленивая (внутри функции,
    не на уровне модуля): guard_step_translator.py САМ грузит свежую копию
    ЭТОГО модуля на своём уровне модуля — если бы эта загрузка тоже была на
    уровне модуля, оба файла грузили бы друг друга при каждом импорте.
    Отложенный вызов (только когда уже есть находка — редкий путь) разрывает
    цикл: к моменту, когда этот модуль просит guard_step_translator, тот уже
    может спокойно догрузить свежую копию ЭТОГО файла (её код на своём
    уровне модуля саму функцию ниже не вызывает — рекурсии нет)."""
    spec = importlib.util.spec_from_file_location(
        "guard_step_translator", Path(__file__).resolve().parent / "guard_step_translator.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Регистрация в sys.modules ДО exec_module: guard_step_translator.py несёт
    # `@dataclass` на классах с отложенными аннотациями — без регистрации
    # сам импорт падает AttributeError на Python 3.11 (см. докстринг
    # guard_step_translator.py::_load_sibling, тот же класс).
    sys.modules["guard_step_translator"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _suggest_guard_filename(repo_ci: Path, name: str) -> str | None:
    """Точное имя файла каталога тем же способом, что реальный перенос
    (guard_step_translator.py::_slug_from_target) — одно место правды для
    «как называть», вторая копия наименования не заводится. Не удалось
    определить (в `run:` нет pytest/node --test/guard-файла, или модуль
    переноса недоступен) — None, сообщение тогда падает на общее текстовое
    правило (см. check_no_undeclared_step)."""
    run_text = _step_run_text(repo_ci, name)
    if not run_text:
        return None
    targets = _extract_run_targets(run_text)
    if not targets:
        return None
    try:
        translator = _load_guard_step_translator()
    except Exception:
        return None
    return f"{translator._slug_from_target(sorted(targets)[0])}.sh"


def check_no_undeclared_step(
    repo_ci: Path = REPO_CI,
    allowlist: frozenset[str] = ALLOWLIST,
    catalog_dir: Path = GUARD_CATALOG_DIR,
) -> list[str]:
    found = guard_step_names(repo_ci)
    added = sorted(found - allowlist)
    removed = sorted(allowlist - found)
    problems: list[str] = []
    problems.extend(check_catalog_handwritten_overlap(repo_ci, catalog_dir))
    for name in added:
        run_text = _step_run_text(repo_ci, name)
        targets = _extract_run_targets(run_text) if run_text else set()
        catalog_invocations = sorted(
            t for t in targets if _is_guard_catalog_invocation(t)
        )
        if catalog_invocations:
            # Ветка газа для класса обхода (б) из #771 (находка ревью PR
            # #902, третий круг): run: шага сам вызывает файл каталога —
            # гвардия уже зарегистрирована, переносить нечего. Старый газ
            # здесь падал на общее правило «создай scripts/ci/guards/
            # <имя>-guard.sh» (_suggest_guard_filename честно вычислял имя
            # обёртки) — человек по инструкции заводил бы ту же обёртку,
            # которую транслятор до этого заводил молча: гвардия исполняется
            # дважды, мутация-критерий #749 («удали файл каталога → должно
            # покраснеть») не срабатывает. Газ для этого класса — удаление
            # шага, каталог не трогается вовсе.
            problems.append(
                f"новый рукописный шаг гвардии в repo-ci.yml: {name!r} — "
                "гвардия УЖЕ зарегистрирована в каталоге: run: этого шага "
                f"вызывает {catalog_invocations} из scripts/ci/guards/ "
                "(#749); переносить нечего и обёртку scripts/ci/guards/"
                "<имя>-guard.sh создавать нельзя — гвардия исполнялась бы "
                "дважды (мутация-критерий #749 «удали файл каталога → "
                "должно покраснеть» молча не срабатывает); просто удали "
                "рукописный шаг целиком из repo-ci.yml (строку `- name: …` "
                "и весь блок `run:` под ней), каталог и ALLOWLIST не трогай"
            )
            continue
        guard_hint = _suggest_guard_filename(repo_ci, name)
        if guard_hint:
            howto = (
                f"создай scripts/ci/guards/{guard_hint} (имя вычислено тем же "
                "способом, что и сам перенос — см. guard_step_translator.py"
                "::_slug_from_target)"
            )
        else:
            howto = (
                "создай scripts/ci/guards/<имя>-guard.sh (имя обычно берётся от "
                "файла теста/гвардии, который вызывает run: этого шага: "
                "test_foo.py → foo-guard.sh, foo.smoke.sh → foo-guard.sh)"
            )
        problems.append(
            f"новый рукописный шаг гвардии в repo-ci.yml: {name!r} — перенеси в "
            f"scripts/ci/guards/ (#749): 1) {howto}; 2) шебанг "
            "#!/usr/bin/env bash + `set -euo pipefail` + дословное тело `run:` "
            "этого шага (бит исполнения не нужен — run_guards.sh зовёт файл "
            "через `bash \"$script\"`, не напрямую); 3) удали из repo-ci.yml "
            "сам шаг целиком (строку `- name: …` и весь блок `run:` под ней), "
            "не оставляя его под другим именем (двойная регистрация); не "
            "дописывай шаг в общий файл (ALLOWLIST — не самообслуживаемый "
            f"обход: ratchet ALLOWLIST_RATCHET_MAX={ALLOWLIST_RATCHET_MAX} не "
            "даёт списку расти)"
        )
    for name in removed:
        problems.append(
            f"ALLOWLIST называет шаг {name!r}, которого больше нет в repo-ci.yml "
            "job `test` — обнови ALLOWLIST в "
            "scripts/lib/ci_guard_registration_guard.py (мигрирован в каталог "
            "или переименован?)"
        )
    if len(allowlist) > ALLOWLIST_RATCHET_MAX:
        problems.append(
            f"ALLOWLIST вырос до {len(allowlist)} записей — потолок "
            f"ALLOWLIST_RATCHET_MAX={ALLOWLIST_RATCHET_MAX} обязан убывать, не расти "
            "(ревью PR #771, major 5); если это правки исправляют недосчёт "
            "детекции (а не рост долга), подвинь ALLOWLIST_RATCHET_MAX вниз вместе "
            "с ALLOWLIST, а не вверх"
        )
    return problems


def main() -> int:
    problems = check_no_undeclared_step()
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return 1
    print(
        f"ci-guard-registration: {len(ALLOWLIST)} рукописных шагов гвардий учтены "
        "в ALLOWLIST, новых незарегистрированных нет"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
