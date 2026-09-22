#!/usr/bin/env bash
# Категория сигнала владельцу — bash-копия реестра scripts/lib/alert_category.py
# (#1461). Здесь именно КОПИЯ, а не чтение: task.sh шлёт отчёт из job'а, где
# поднимать Python ради одной строки дороже, чем держать четыре пары значений.
#
# Копия допустима ровно потому, что её расхождение с двумя другими языками
# красит CI: scripts/lib/test_alert_category_sync.py читает ВСЕ ТРИ исходника
# (этот, .py и cf-worker/src/config.ts) и требует совпадения id и префиксов.
# Тот же приём, которым уже держится формат callback_data решения владельца
# (test_telegram_callback_format_sync.py, #254) — не изобретение, а повтор.
#
# Использование: alert_prefix <категория>  → «⚙️ Конвейер»
#                неизвестная категория     → код 1 и текст причины в stderr

alert_prefix() {
  case "$1" in
    decision) printf '🙋 Решение владельца' ;;
    breakage) printf '🔴 Поломка' ;;
    pipeline) printf '⚙️ Конвейер' ;;
    infra)    printf '🏗 Инфраструктура' ;;
    *)
      echo "alert_prefix: категория '$1' не объявлена (см. scripts/lib/alert_category.py)" >&2
      return 1
      ;;
  esac
}
