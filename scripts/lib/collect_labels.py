#!/usr/bin/env python3
"""Сборщик литералов GitHub-меток, реально используемых кодом (задача #207).

Класс проблемы: реестр меток, набранный руками, отстаёт от кода ровно так же,
как отстаёт любая ручная копия одного места правды — новая метка появляется
в `scripts/`/workflow, и никто не обязан вспомнить про реестр. Список здесь
не хардкодится значениями — он СОБИРАЕТСЯ:

  1. Метки-вердикты ревью (review:*, ai:*) — импортом настоящих констант из
     scripts/lib/review_labels.py (существующее место правды, задача #18) —
     не переписываются строками.
  2. Остальные метки — регулярками по четырём формам записи литерала, реально
     встречающимся в этом репозитории:
       - Python-константы `*_LABEL = "значение"` (contract_check.py,
         scheduler.py: SKIP_LABEL, TASK_LABEL, CONFLICT_LABEL);
       - прямые литералы в вызовах gh/jq (`labels[]=contract:failed`,
         `labels[]=task`, `select(.name == "task")`, `--add-label blocked`);
       - YAML-шапка шаблонов issue (`labels: [task]`);
       - YAML-дропдаун `options:` шаблонов issue (`area:worker`/`area:process`
         и т.п. — `.github/ISSUE_TEMPLATE/task.yml`, поле «Площадь»). Найдено
         задачей #361: до этой правки дропдаун не сканировался вовсе — все
         пять `area:*` были невидимы гвардии реестра меток
         (`test_label_registry.py`), хотя реально применяются как метки
         (`gh issue edit --add-label area:worker` по конвенции агентов —
         подтверждено живым запросом: 27/28 issues несут `area:worker`/
         `area:orchestra`). Регэксп ограничен строками, ЦЕЛИКОМ совпадающими
         с `_LABEL_TOKEN` (нижний регистр, `[a-z0-9:_-]`) — не ловит
         содержательные Cyrillic-предложения дропдауна «Тип» в
         `white-spot.yml` («Противоречие (док ↔ код...)» и т.п.), у которых
         нет ни одного полного совпадения со схемой литерала метки.

Гвардия расширяется правкой ЭТИХ regex'ов, если код начнёт метить иначе —
не строкой в списке меток.

Запуск как модуль: python -m scripts.lib.collect_labels (или importlib,
паттерн review_labels — скрипты этого репозитория запускаются как файлы).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ISSUE_TEMPLATE_DIR = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
WORKER_SRC_DIR = REPO_ROOT / "cf-worker" / "src"

_LABEL_TOKEN = r"[a-z][a-z0-9:_-]*"

# Форма 1: Python-константа `*_LABEL = "значение"` — contract_check.py/scheduler.py.
_PY_LABEL_CONST_RE = re.compile(
    r'^[A-Z][A-Z0-9_]*_LABEL\s*=\s*"(' + _LABEL_TOKEN + r')"', re.MULTILINE
)
# Форма 2а: query-string литерал `labels[]=значение` (НЕ f-строка с {переменной}).
_LABELS_QS_RE = re.compile(r"labels\[\]=(" + _LABEL_TOKEN + r")\b")
# Форма 2б: jq-проверка `select(.name == "значение")`.
_JQ_SELECT_RE = re.compile(r'select\(\.name == "(' + _LABEL_TOKEN + r')"\)')
# Форма 2в: bash CLI `--add-label значение`.
_ADD_LABEL_RE = re.compile(r"--add-label\s+(" + _LABEL_TOKEN + r")\b")
# Форма 3: YAML-шапка шаблона issue `labels: [значение, ...]`.
_YAML_LABELS_RE = re.compile(r"^labels:\s*\[([^\]]*)\]", re.MULTILINE)
# Форма 3б: YAML-дропдаун `options:\n  - значение\n  - значение2` шаблона
# issue (поле «Площадь», area:*) — блок отступленных `- ...` строк сразу
# после `options:`, до первой строки, что не начинается с `-` (следующий
# ключ шаблона). Внутри блока берутся ТОЛЬКО строки, целиком совпадающие с
# `_LABEL_TOKEN` — «- Противоречие (док ↔ код...)» дропдауна «Тип» в
# white-spot.yml не матчит целиком (Cyrillic, пробелы, скобки) и отсеивается
# на этом шаге, не блочным регэкспом.
_YAML_OPTIONS_BLOCK_RE = re.compile(r"options:\s*\n((?:[ \t]+-[ \t].*\n?)+)")
_YAML_OPTION_ITEM_RE = re.compile(r"^\s*-\s*(" + _LABEL_TOKEN + r")\s*$", re.MULTILINE)
# Форма 4: массив меток в TS-коде морды `labels: ["task", "source:inbox"]`
# (cf-worker/src ставит метки при создании issue из инбокса; без этого скана
# метка появлялась в морде молча, мимо реестра — ревью PR #173).
_TS_LABELS_RE = re.compile(r"labels:\s*\[([^\]]*)\]")
_TS_QUOTED_RE = re.compile(r"['\"](" + _LABEL_TOKEN + r")['\"]")


def _review_labels_module():
    spec = importlib.util.spec_from_file_location(
        "review_labels", Path(__file__).resolve().parent / "review_labels.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scan_py_files() -> set[str]:
    found: set[str] = set()
    for path in SCRIPTS_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        found.update(_PY_LABEL_CONST_RE.findall(text))
        found.update(_LABELS_QS_RE.findall(text))
    return found


def _scan_shell_files() -> set[str]:
    found: set[str] = set()
    for path in SCRIPTS_DIR.rglob("*.sh"):
        text = path.read_text(encoding="utf-8")
        found.update(_JQ_SELECT_RE.findall(text))
        found.update(_ADD_LABEL_RE.findall(text))
    return found


def _scan_workflow_files() -> set[str]:
    found: set[str] = set()
    if not WORKFLOWS_DIR.is_dir():
        return found
    for path in list(WORKFLOWS_DIR.glob("*.yml")) + list(WORKFLOWS_DIR.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        found.update(_JQ_SELECT_RE.findall(text))
        found.update(_LABELS_QS_RE.findall(text))
    return found


def _scan_issue_templates() -> set[str]:
    found: set[str] = set()
    if not ISSUE_TEMPLATE_DIR.is_dir():
        return found
    for path in ISSUE_TEMPLATE_DIR.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        for match in _YAML_LABELS_RE.findall(text):
            found.update(item.strip() for item in match.split(",") if item.strip())
        for block in _YAML_OPTIONS_BLOCK_RE.findall(text):
            found.update(_YAML_OPTION_ITEM_RE.findall(block))
    return found


def _scan_worker_ts_files() -> set[str]:
    found: set[str] = set()
    if not WORKER_SRC_DIR.is_dir():
        return found
    for path in WORKER_SRC_DIR.rglob("*.ts"):
        text = path.read_text(encoding="utf-8")
        for match in _TS_LABELS_RE.findall(text):
            found.update(_TS_QUOTED_RE.findall("[" + match + "]"))
    return found


def collect_labels() -> set[str]:
    """Все строковые литералы меток, реально используемые кодом репозитория."""
    review_labels = _review_labels_module()
    found: set[str] = {
        review_labels.REVIEW_OK,
        review_labels.REVIEW_CHANGES,
        review_labels.REVIEW_LARGE,
        review_labels.LARGE_OK,
        review_labels.AI_OK,
        review_labels.AI_CHANGES,
        review_labels.AI_FAILED,
    }
    found |= _scan_py_files()
    found |= _scan_shell_files()
    found |= _scan_workflow_files()
    found |= _scan_issue_templates()
    found |= _scan_worker_ts_files()
    return found


if __name__ == "__main__":
    for label in sorted(collect_labels()):
        print(label)
