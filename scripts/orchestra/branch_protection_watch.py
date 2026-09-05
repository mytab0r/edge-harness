#!/usr/bin/env python3
"""Сигнал ослабления защиты `main` — событие, не опрос (#370, #341).

Инвариант 6 (`scripts/orchestra/repo_invariants.py::check_branch_protection_drift`,
PR #249, не смёржен) не может ходить в `GET /repos/{o}/{r}/branches/main/
protection`: это требует у токена право `administration`, которого структурно
нет в перечне permissions `GITHUB_TOKEN` (docs/research/21-github-actions.md).
Владелец не принял вариант с новым админским секретом (#370) — вместо
опроса, требующего чтения состояния, используется штатное событие GitHub
Actions `branch_protection_rule` (created/edited/deleted): оно срабатывает
САМИМ фактом изменения защиты и несёт изменённое состояние в payload
(`rule`, `changes`) — читать настройки через API не нужно вовсе, значит не
нужно и право `administration`. Подтверждено живым запросом к api.github.com
и к каноническим примерам payload'ов GitHub (raw.githubusercontent.com/
octokit/webhooks/main/payload-examples/api.github.com/branch_protection_rule/
{created,edited,deleted}.payload.json, 2026-09-06) — поля `rule.admin_enforced`
(bool), `rule.required_status_checks` (список контекстов), `rule.
strict_required_status_checks_policy` (bool), `rule.allow_force_pushes_
enforcement_level` / `rule.allow_deletions_enforcement_level` ("off"/
"non_admins"/"everyone") взяты оттуда, а не из документации по памяти.

EXPECTED_* ниже — временная копия одноимённых констант
`repo_invariants.py::EXPECTED_*` (PR #249, не в main на момент этого файла):
второй копии по духу не заводим, но и не блокируемся на чужом открытом PR
под ai:changes-requested. Когда #249 сольётся — заменить константы на
`from repo_invariants import EXPECTED_*` (имена выбраны совпадающими нарочно,
это должна быть правка в одну строку, не рефакторинг). Значения — то же
единственное место правды прозой: AGENTS.md, «Защита main».

Канал сигнала — общий с предохранителем конвейера: `pulse_guard.escalate`,
issue `WATCHDOG_ISSUE` (#120), Telegram. Второй канал для того же класса
«поломка инфраструктуры» не заводим (тот же принцип, что #119/#174).

Шум: `changes` в payload `edited` показывает, что именно менялось, но решение
принимается по ТЕКУЩЕМУ состоянию правила против EXPECTED_* (тот же приём,
что `check_branch_protection_drift`), а не по факту наличия `changes` —
поэтому добавление ещё одного обязательного контекста (надмножество
EXPECTED_STATUS_CHECK_CONTEXTS) не кричит: контекстов не стало МЕНЬШЕ
ожидаемых.
"""

import json
import os
import sys
from pathlib import Path

from pulse_guard import WATCHDOG_ISSUE, escalate

# ── Единственное место правды на ожидаемое состояние (см. докстринг выше) ────

EXPECTED_ENFORCE_ADMINS = True
EXPECTED_STATUS_CHECKS_STRICT = True
EXPECTED_STATUS_CHECK_CONTEXTS = frozenset({"test", "contract"})
EXPECTED_ALLOW_FORCE_PUSHES = False
EXPECTED_ALLOW_DELETIONS = False

# Защищаем только main — репозиторий сегодня не заводит правил на другие ветки,
# но `branch_protection_rule` репозиторный, не привязан к ref в синтаксисе
# workflow (нет `branches:` фильтра для этого события), поэтому фильтрация —
# в коде, не в YAML.
WATCHED_BRANCH = "main"

MARKER = "[статус: защита main ослаблена]"

# Человекочитаемое следствие на каждое поле — не факт, а что стало возможно.
_CONSEQUENCE = {
    "admin_enforced": (
        "`enforce_admins` выключен: админский токен теперь сливает в main мимо "
        "обязательных проверок test и contract — так 2026-09-06 был слит PR с "
        "красным контрактом."
    ),
    "required_status_checks": (
        "Из обязательных проверок пропали контексты — PR может слиться, не "
        "пройдя их."
    ),
    "strict_required_status_checks_policy": (
        "Ветка PR больше не обязана быть в актуальном состоянии относительно "
        "main перед слиянием (`strict` выключен) — обязательные проверки могут "
        "идти по устаревшему коду."
    ),
    "allow_force_pushes_enforcement_level": (
        "Force-push в main теперь разрешён — история main может переписываться."
    ),
    "allow_deletions_enforcement_level": (
        "Удаление ветки main теперь разрешено."
    ),
    "branch_protection": (
        "Защита main снята целиком — ни одно из перечисленных выше ограничений "
        "(enforce_admins, обязательные проверки, strict, запрет force-push и "
        "удаления) больше не действует."
    ),
}


def evaluate_rule(action: str, rule: dict) -> list[dict]:
    """Список нарушений EXPECTED_* (пусто — состояние в норме, сигнала не будет).

    action == 'deleted' — правило снято целиком, это всегда критично
    независимо от последнего известного `rule` (сам факт правила пропал).
    Отсутствующее в payload поле — не гадаем, просто не проверяем его (не
    даём неизвестной форме породить ложную тревогу)."""
    if action == "deleted":
        return [{"field": "branch_protection", "expected": "present", "actual": "removed"}]

    violations = []

    admin_enforced = rule.get("admin_enforced")
    if admin_enforced is not None and admin_enforced is not EXPECTED_ENFORCE_ADMINS:
        violations.append({
            "field": "admin_enforced",
            "expected": EXPECTED_ENFORCE_ADMINS,
            "actual": admin_enforced,
        })

    raw_contexts = rule.get("required_status_checks")
    if raw_contexts is not None:
        contexts = set(raw_contexts)
        missing = EXPECTED_STATUS_CHECK_CONTEXTS - contexts
        if missing:
            violations.append({
                "field": "required_status_checks",
                "expected": sorted(EXPECTED_STATUS_CHECK_CONTEXTS),
                "actual": sorted(contexts),
                "missing": sorted(missing),
            })

    strict = rule.get("strict_required_status_checks_policy")
    if strict is not None and strict is not EXPECTED_STATUS_CHECKS_STRICT:
        violations.append({
            "field": "strict_required_status_checks_policy",
            "expected": EXPECTED_STATUS_CHECKS_STRICT,
            "actual": strict,
        })

    force_pushes_level = rule.get("allow_force_pushes_enforcement_level")
    if force_pushes_level is not None:
        allowed = force_pushes_level != "off"
        if allowed is not EXPECTED_ALLOW_FORCE_PUSHES:
            violations.append({
                "field": "allow_force_pushes_enforcement_level",
                "expected": "off" if not EXPECTED_ALLOW_FORCE_PUSHES else "not off",
                "actual": force_pushes_level,
            })

    deletions_level = rule.get("allow_deletions_enforcement_level")
    if deletions_level is not None:
        allowed = deletions_level != "off"
        if allowed is not EXPECTED_ALLOW_DELETIONS:
            violations.append({
                "field": "allow_deletions_enforcement_level",
                "expected": "off" if not EXPECTED_ALLOW_DELETIONS else "not off",
                "actual": deletions_level,
            })

    return violations


def recovery_command(repo: str, field: str) -> str:
    """Готовая команда `gh api` для конкретного нарушения — газ тормоза
    (правило AGENTS.md «тормоз обязан назвать газ»): владелец копирует и
    выполняет с телефона одним действием, без второго решения "что вводить"."""
    if field == "branch_protection":
        return (
            f"gh api --method PUT repos/{repo}/branches/{WATCHED_BRANCH}/protection "
            "--input - <<'JSON'\n"
            '{"required_status_checks": {"strict": true, "contexts": ["test", "contract"]}, '
            '"enforce_admins": true, "required_pull_request_reviews": null, '
            '"restrictions": null, "allow_force_pushes": false, "allow_deletions": false}\n'
            "JSON"
        )
    if field == "admin_enforced":
        return f"gh api -X POST repos/{repo}/branches/{WATCHED_BRANCH}/protection/enforce_admins"
    if field in ("required_status_checks", "strict_required_status_checks_policy"):
        return (
            f"gh api -X PATCH repos/{repo}/branches/{WATCHED_BRANCH}/protection/required_status_checks "
            "-f strict=true -f 'contexts[]=test' -f 'contexts[]=contract'"
        )
    if field == "allow_force_pushes_enforcement_level":
        return f"gh api -X DELETE repos/{repo}/branches/{WATCHED_BRANCH}/protection/allow_force_pushes"
    if field == "allow_deletions_enforcement_level":
        return f"gh api -X DELETE repos/{repo}/branches/{WATCHED_BRANCH}/protection/allow_deletions"
    raise ValueError(f"нет команды восстановления для поля {field!r}")


def build_alert_text(action: str, violations: list[dict], repo: str, sender: str) -> str:
    """Детерминированный текст сигнала: заголовок с маркером, дальше по одному
    пункту на нарушение — следствие и готовая команда восстановления."""
    lines = [
        f"🚨 edge-harness: {MARKER}",
        f"Событие branch_protection_rule: {action} (инициатор: {sender})",
        "",
    ]
    for violation in violations:
        field = violation["field"]
        consequence = _CONSEQUENCE.get(field, f"поле {field} разошлось с ожидаемым")
        lines.append(f"- {consequence}")
        if field == "required_status_checks" and violation.get("missing"):
            lines.append(f"  Пропали контексты: {', '.join(violation['missing'])}")
        lines.append(f"  Восстановление: `{recovery_command(repo, field)}`")
    return "\n".join(lines)


def branch_protection_check(repo: str, event: dict) -> list[str]:
    """Проводка: один вызов из main(). Возвращает строки отчёта."""
    action = event.get("action")
    rule = event.get("rule") or {}
    rule_name = rule.get("name")
    sender = (event.get("sender") or {}).get("login", "неизвестно")

    if rule_name is not None and rule_name != WATCHED_BRANCH:
        return [f"ℹ️ branch_protection_rule: {action} на правиле {rule_name!r} — не {WATCHED_BRANCH}, пропускаю"]

    violations = evaluate_rule(action, rule)
    if not violations:
        return [f"✅ branch_protection_rule: {action} — состояние {WATCHED_BRANCH} соответствует EXPECTED_*"]

    text = build_alert_text(action, violations, repo, sender)
    delivered = escalate(repo, WATCHDOG_ISSUE, text)
    fields = ", ".join(v["field"] for v in violations)
    return [f"🚨 branch_protection_rule: {action} — нарушено ({fields}) — сигнал в #{WATCHDOG_ISSUE} ({delivered})"]


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    event_path = os.environ["GITHUB_EVENT_PATH"]
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    for line in branch_protection_check(repo, event):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
