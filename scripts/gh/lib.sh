#!/usr/bin/env bash
# Общая точка входа в сеть GitHub для bash-скриптов scripts/gh/*.
#
# Грабля #1 инфраструктуры (docs/agents/INFRA-GH.md): прямые запросы к
# api.github.com из этого окружения рвутся без локального SOCKS-прокси.
# Раньше каждый агент городил свой `export HTTPS_PROXY=...` в каждой
# bash-команде — здесь это сведено в одно место: перебор портов, первый
# рабочий держится на весь процесс.
#
# Использование: source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; gh_net_setup || exit 1
GH_NET_PROXY_PORTS=(1084 1083 1085)

gh_net_setup() {
  # Уже задан явно снаружи (например владельцем/другим слоем) — доверяем и не трогаем.
  if [ -n "${HTTPS_PROXY:-}" ]; then
    return 0
  fi
  local port
  for port in "${GH_NET_PROXY_PORTS[@]}"; do
    if curl -sS --max-time 5 -o /dev/null -x "socks5://127.0.0.1:${port}" \
         https://api.github.com/rate_limit 2>/dev/null; then
      export HTTPS_PROXY="socks5://127.0.0.1:${port}"
      return 0
    fi
  done
  echo "ОШИБКА: ни один из портов SOCKS-прокси (${GH_NET_PROXY_PORTS[*]}) не пропускает api.github.com." >&2
  echo "Проверь, что прокси-процесс запущен на хосте, или передай HTTPS_PROXY явно." >&2
  return 1
}
