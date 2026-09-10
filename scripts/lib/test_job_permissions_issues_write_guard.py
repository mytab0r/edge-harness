#!/usr/bin/env python3
"""Гвардия класса «job-level permissions заменяет workflow-level целиком,
issues: write теряется молча» (#884).

Живой прогон ai-review 34520146758, job `verdict` (id 103022310318), шаг
«Вердикт: комментарий + метка», PR #870, HEAD_REVIEWED 7469fa64:

    ##[warning]след в #120 не оставлен: gh api -X POST: gh: Resource not accessible by integration (HTTP 403)
    ##[warning]TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — сигнал не отправлен
    ##[warning]large-ok: +2472 строк > 2000 — эскалация владельцу (Telegram: НЕ доставлен; след в #120: НЕ оставлен)
    verdict: approve — ai:ok

Класс, не случай: GitHub Actions документирует `permissions:` на уровне
job'а как ПОЛНУЮ ЗАМЕНУ workflow-level набора, а не слияние с ним
(https://docs.github.com/actions/security-guides/automatic-token-authentication) —
job, объявивший СВОЙ блок `permissions:`, теряет любой пункт workflow-level,
который сам не повторил. ai-review.yml объявлял `issues: read` на
workflow-level (нужен шагу fingerprint), но job `verdict` — единственный,
кто реально ПИШЕТ в Issues API (WATCHDOG_ISSUE #120 через apply_large_ok →
pulse_guard.escalate, scripts/review/ai_review.py) — переобъявил
permissions без issues: write вовсе: метка/комментарий на сам PR прошли
(PR обслуживается как issue по pull-requests: write), а комментарий в
НАСТОЯЩИЙ Issue #120 упал 403 при зелёном job'е.

Проверка (по всем .github/workflows/*.yml и *.yaml): для каждого job'а,
который (а) объявляет СВОЙ `permissions:` (полная замена workflow-level —
корень дефекта) и (б) хотя бы один шаг вызывает код, умеющий писать в
НАСТОЯЩИЙ Issue, не в номер PR (ESCALATION_ENTRY_MARKERS ниже) и (в) job
аутентифицируется github.token/GITHUB_TOKEN (широкий PAT — secrets.
GH_PIPELINE_PAT/GH_DISPATCH_TOKEN — вообще не подчиняется блоку
permissions:, гвардия его не касается) — свой блок обязан нести
`issues: write`.

Curated список маркеров (не автоматический static-analysis compilerа
шелл-команд) — тот же приём, что PIPELINE_CONSUMERS в
test_dispatch_token_usage.py: новый источник, способный писать в Issues API
мимо номера PR, добавляется сюда сознательно.

Мутация: убрать `issues: write` из permissions job'а `verdict` в
ai-review.yml — тест краснеет с именем этого job'а в сообщении.

Запуск: python -m pytest scripts/lib/test_job_permissions_issues_write_guard.py -q
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# \b работает и на "_" (word-char) — "test_scheduler.py"/"test_pulse_guard.py"
# (гвардийные же вызовы pytest, не сама эскалация) НЕ матчатся, потому что
# перед именем стоит "_", а не граница слова.
ESCALATION_ENTRY_PATTERNS = (
    re.compile(r"ai_review\.py\s+verdict"),   # apply_large_ok -> pulse_guard.escalate(WATCHDOG_ISSUE)
    re.compile(r"\bpulse_guard\.py\b"),        # прямой запуск модуля предохранителя
    re.compile(r"\bscheduler\.py\b"),          # оркестратор — эскалации через pulse_guard.escalate
    re.compile(r"scripts/gh/issue-create\b"),  # создание задач напрямую в Issues API
)

GITHUB_TOKEN_MARKERS = ("github.token", "secrets.GITHUB_TOKEN")


def _workflow_files():
    return sorted(p for p in WORKFLOWS_DIR.iterdir() if p.suffix in (".yml", ".yaml"))


def _job_text(job: dict) -> str:
    """Весь текст, которым job может выдать «я пишу в Issues API» и «я хожу
    под github.token»: run-скрипты шагов + их env (значения ${{ ... }})."""
    parts = []
    for step in job.get("steps") or []:
        parts.append(str(step.get("run") or ""))
        parts.append(str(step.get("env") or ""))
    parts.append(str(job.get("env") or ""))
    return "\n".join(parts)


def jobs_missing_issues_write() -> list[str]:
    violations = []
    for path in _workflow_files():
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_name, job in (doc.get("jobs") or {}).items():
            own_perms = job.get("permissions")
            if own_perms is None:
                continue  # наследует workflow-level целиком — не тот класс
            text = _job_text(job)
            if not any(pattern.search(text) for pattern in ESCALATION_ENTRY_PATTERNS):
                continue
            if not any(marker in text for marker in GITHUB_TOKEN_MARKERS):
                continue  # широкий PAT — permissions: блок его не ограничивает
            if own_perms.get("issues") != "write":
                violations.append(f"{path.name}::{job_name}")
    return violations


def test_jobs_that_escalate_to_issues_keep_issues_write_in_own_permissions():
    violations = jobs_missing_issues_write()
    assert not violations, (
        "job(ы) переопределяют permissions (job-level ЗАМЕНЯЕТ workflow-level "
        "целиком, не сливает) и вызывают код, способный писать в НАСТОЯЩИЙ "
        f"Issue, но не держат issues: write в СВОЁМ блоке: {violations} — "
        "комментарий/метка на Issue (не PR) получит 403 при зелёном job'е "
        "(см. докстринг этого файла, #884)"
    )


def test_ai_review_verdict_job_is_the_documented_live_case():
    """Живой случай (#884) обязан быть виден этой гвардии без вспомогательных
    условий — если ai-review.yml перестанет матчить маркер/токен, проверка
    выше может замолчать по неверной причине (не «фикс держится», а «условие
    матчинга сломалось»). Позитивный тест ловит именно это расхождение."""
    doc = yaml.safe_load((WORKFLOWS_DIR / "ai-review.yml").read_text(encoding="utf-8"))
    verdict_job = doc["jobs"]["verdict"]
    text = _job_text(verdict_job)
    assert any(p.search(text) for p in ESCALATION_ENTRY_PATTERNS), (
        "ai-review.yml::verdict больше не матчит ни один ESCALATION_ENTRY_PATTERNS — "
        "гвардия перестала видеть исходный живой случай #884"
    )
    assert any(marker in text for marker in GITHUB_TOKEN_MARKERS)
    assert verdict_job["permissions"]["issues"] == "write", (
        "ai-review.yml::verdict потерял issues: write — регресс #884"
    )
