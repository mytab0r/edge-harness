#!/usr/bin/env python3
"""Гвардия границы доверия персистентного состояния квоты провайдеров
(#857, openspec/changes/provider-quota-gating, design.md «Что читает, что
пишет — граница доверия»): ЛЮБОЙ чейн-раннер (в т.ч. `ai-review.yml` —
недоверенная зона, у её DSH-шага НЕТ GitHub-токена вовсе, #18) читает
`vars.DSH_PROVIDER_QUOTA_UNTIL` контекстом workflow, пишет ТОЛЬКО пульс
оркестратора (`scripts/orchestra/provider_quota_state.py::save_quota_state`,
вызывается из `scheduler.py`, у которого есть `GH_PIPELINE_PAT`/`ORCHESTRA_PAT`).

Нарушение границы — любой workflow пишет переменную напрямую (`gh variable
set`), либо `save_quota_state` вызывается кодом вне `scripts/orchestra/**` —
оба класса означали бы, что недоверенная зона получила бы (или могла бы
получить) право записи в repo vars, которого у неё сегодня нет и не
должно быть.

Запуск: python -m pytest scripts/lib/test_provider_quota_state_guard.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCRIPTS = REPO_ROOT / "scripts"

QUOTA_VAR = "DSH_PROVIDER_QUOTA_UNTIL"
WRITE_RE = re.compile(r"gh\s+variable\s+set\s+" + re.escape(QUOTA_VAR) + r"\b")

# Три consumer'а обязаны ЧИТАТЬ (тот же паттерн, что vars.DSH_PROVIDER_CHAIN)
# — гвардия того, что чтение не потерялось молча (симметрично гвардии
# записи ниже: одностороннее исчезновение чтения тоже регрессия).
READING_WORKFLOWS = ("ai-review.yml", "worker.yml", "hands.yml")
READ_RE = re.compile(r"\$\{\{\s*vars\." + re.escape(QUOTA_VAR) + r"\s*\}\}")


def workflow_text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _non_comment_lines(text: str) -> str:
    """Строки YAML, чей первый непробельный символ — не `#` — тот же приём,
    что уже применяет scripts/lib/test/provider-default.guard.sh (класс
    #153) для различения исполняемого кода от документирующего комментария
    (например, этот самый файл описывает мутацию словами `gh variable set
    DSH_PROVIDER_QUOTA_UNTIL` в докстринге/шаге repo-ci.yml — упоминание
    ФАКТА не должно красить гвардию как нарушение)."""
    return "\n".join(
        line for line in text.splitlines()
        if line.strip()[:1] != "#"
    )


def test_no_workflow_writes_quota_variable_directly():
    """Ни один workflow не зовёт `gh variable set DSH_PROVIDER_QUOTA_UNTIL`
    напрямую — запись идёт кодом пульса, не bash-строкой workflow (иначе
    граница живёт памятью, не структурой)."""
    offenders = [
        path.name for path in sorted(WORKFLOWS.iterdir())
        if path.suffix in {".yml", ".yaml"}
        and WRITE_RE.search(_non_comment_lines(path.read_text(encoding="utf-8")))
    ]
    assert not offenders, f"{QUOTA_VAR} записывается напрямую из workflow: {offenders}"


def test_all_three_consumers_read_quota_variable():
    """Регрессия в другую сторону: чтение не должно молча пропасть из
    какого-то из трёх consumer'ов — иначе персистентная квота гейтирует
    только часть каналов, деля цепочку на «умную» и «слепую» половины."""
    missing = [name for name in READING_WORKFLOWS if not READ_RE.search(workflow_text(name))]
    assert not missing, f"workflows без чтения vars.{QUOTA_VAR}: {missing}"


def test_save_quota_state_called_only_from_orchestra_code():
    """`save_quota_state` (единственная пишущая функция) вызывается только
    из scripts/orchestra/** — ai-review/worker/hands читают состояние через
    dsh-ci.sh (bash, вообще без Python-модуля provider_quota_state), ни
    один из них не должен звать python-писатель."""
    offenders = []
    for path in sorted(SCRIPTS.rglob("*.py")):
        if path.name in {"provider_quota_state.py", "test_provider_quota_state_guard.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        if "save_quota_state(" not in text:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if not rel.startswith("scripts/orchestra/"):
            offenders.append(rel)
    assert not offenders, f"save_quota_state вызывается вне scripts/orchestra/: {offenders}"


def test_ai_review_dsh_step_has_no_github_token():
    """Структурная проверка самой предпосылки, на которой держится вся
    граница доверия (design.md): «Ревью агентом» — единственный шаг без
    GH_TOKEN/GITHUB_TOKEN в env, поэтому физически не может выполнить `gh
    api -X PATCH .../actions/variables/...` даже если бы кто-то попытался
    дописать такой вызов в его bash — запрос отвалится авторизацией
    раньше, чем что-либо запишется."""
    text = workflow_text("ai-review.yml")
    marker = "НЕдоверенный шаг: НИКАКОГО GitHub-токена в env"
    assert marker in text, (
        "ai-review.yml: маркер недоверенного шага пропал — гвардия не может "
        "локализовать шаг, чью границу проверяет"
    )
    start = text.index(marker)
    next_step = text.find("\n      - ", text.index("env:", start) + 4)
    step_text = text[start:next_step if next_step != -1 else len(text)]
    assert "GH_TOKEN" not in step_text and "GITHUB_TOKEN" not in step_text, (
        "ai-review.yml: недоверенный DSH-шаг получил GitHub-токен — граница "
        "доверия провайдер-квоты (только чтение) больше не гарантирована "
        "структурой файла"
    )
