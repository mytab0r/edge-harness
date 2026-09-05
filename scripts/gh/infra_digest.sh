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
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

print_infra_digest() {
  local primary="${GH_NET_PROXY_PORTS[0]}"
  local rest=("${GH_NET_PROXY_PORTS[@]:1}")
  local last_idx=$((${#rest[@]} - 1))
  local fallback="" i
  for i in "${!rest[@]}"; do
    if [ "$i" -eq 0 ]; then
      fallback="${rest[$i]}"
    elif [ "$i" -eq "$last_idx" ]; then
      fallback="$fallback, потом ${rest[$i]}"
    else
      fallback="$fallback, ${rest[$i]}"
    fi
  done
  cat >&2 <<DIGEST

── Инфраструктура: граблям не удивляться (полностью — docs/agents/INFRA-*.md) ──
GitHub: прокси ОБЯЗАТЕЛЕН перед gh/git — export HTTPS_PROXY=socks5://127.0.0.1:${primary}
        (не прошло — ${fallback}; scripts/gh/lib.sh перебирает сам).
        gh issue/pr view/create — GraphQL, падает чаще gh api. heredoc в bash —
        блокируется, многострочный текст через Write в файл.
        scripts/gh/queue.py и pr_blockers.py <N> — что мешает PR слиться.
Cloudflare: см. docs/agents/INFRA-CF.md (лимиты DO/Workers, если документ уже есть).
─────────────────────────────────────────────────────────────────────────────
DIGEST
}
