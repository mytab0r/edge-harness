#!/usr/bin/env bash
# Общий дайджест граблей инфраструктуры GitHub/Cloudflare — печатается агенту
# в момент входа в agent-ветку.
#
# Раньше текст жил только в scripts/git/task-branch, а у входа в задачу два
# пути: новая задача (task-branch) и доводка уже открытого PR
# (scripts/worker/task.sh, CONTINUE_PR_NUMBER делает `git checkout -B`
# напрямую, task-branch не вызывается). Второй путь — вердикт AI-ревью на
# #326 — оставался без дайджеста, хотя именно там scripts/gh/* нужны раньше
# всего (доводка по замечаниям ai:changes-requested). Вынесено сюда одним
# местом правды, зовётся из ОБОИХ входов.
#
# Использование: source "$(dirname "${BASH_SOURCE[0]}")/../gh/infra_digest.sh"; print_infra_digest
print_infra_digest() {
  cat >&2 <<DIGEST

── Инфраструктура: граблям не удивляться (полностью — docs/agents/INFRA-*.md) ──
GitHub: gh issue/pr view/create — GraphQL, падает чаще gh api. heredoc в bash —
        блокируется, многострочный текст через Write в файл.
        scripts/gh/queue.py и pr_blockers.py <N> — что мешает PR слиться.
Cloudflare: см. docs/agents/INFRA-CF.md (лимиты DO/Workers, если документ уже есть).
─────────────────────────────────────────────────────────────────────────────
DIGEST
}
