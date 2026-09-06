#!/usr/bin/env bash
# Установка PATH-шима `gh` (#594) — единственное место правды, единственный
# вызывающий scripts/worker/task.sh. Область действия — окружение ЭТОГО
# процесса: export PATH меняет только его и дочерние (dsh — обычный
# subprocess, наследует PATH как есть); локальные машины и другие job'ы не
# затронуты, никакой системной/глобальной подмены gh здесь нет.
#
# Подключение: source "$(dirname "${BASH_SOURCE[0]}")/../lib/gh_shim.sh"
# Рассчитано на bash с set -euo pipefail (источник задаёт), как и dsh-ci.sh.

# Каталог этого файла — источник шаблона scripts/gh-shim/gh (та же копия,
# что живёт в дереве репозитория, не переизобретённая инлайн-строка).
GH_SHIM_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

gh_shim_install() { # $1 — рабочий каталог для шима (создаётся, обычно $WORK/gh-shim)
  local shim_dir=$1 real_gh src resolved
  # `type -P` — ПРИНУДИТЕЛЬНЫЙ поиск по PATH, в отличие от `command -v`
  # игнорирует функции/алиасы шелла (bash отдаёт функции приоритет над PATH
  # для голого слова `gh`): scripts/lib/test/dsh-clients.smoke.sh стабит `gh`
  # именно функцией для прямых bash-вызовов клиента (и отдельным файлом в
  # PATH — для python-подпроцессов claim_task.py, которых `export -f` не
  # достаёт); тем же файлом в PATH пользуется и этот шим. `command -v` в той
  # же смок-среде нашёл бы функцию, а не файл, и решил бы, что «настоящего
  # gh нет» — ложный отказ на ровном месте.
  real_gh="$(type -P gh)" || {
    echo "::error::gh-shim: настоящий gh не найден в PATH (type -P) — установка невозможна (без него шим звал бы в никуда)" >&2
    return 1
  }
  src="$GH_SHIM_LIB_DIR/../gh-shim/gh"
  [ -f "$src" ] || {
    echo "::error::gh-shim: исходник $src не найден — установка сломана" >&2
    return 1
  }
  mkdir -p "$shim_dir"
  cp "$src" "$shim_dir/gh"
  chmod +x "$shim_dir/gh"
  export GH_SHIM_REAL_GH="$real_gh"
  export PATH="$shim_dir:$PATH"
  # До этой строки `gh` уже мог резолвиться (issue view/pr list выше по
  # task.sh) — bash кеширует найденный путь (hash table) и без сброса
  # продолжил бы отдавать СТАРЫЙ путь на голое слово `gh`, PATH бы не помог.
  hash -r
  # Доказательство, а не предположение: PATH-резолюция `gh` ПРЯМО СЕЙЧАС
  # обязана указывать на только что установленный шим. Иначе (другой каталог
  # раньше в PATH, гвардия прав на исполнение и т.п.) `gh pr create` агента
  # ушёл бы мимо шима НЕЗАМЕТНО — тот самый обход, который эта установка
  # обязана делать невозможным. Фейл здесь — громкий, не тихая деградация до
  # «шима как бы нет, но никто не узнает».
  resolved="$(type -P gh)" || {
    echo "::error::gh-shim: type -P gh не находит ничего после установки — PATH сломан" >&2
    return 1
  }
  if [ "$resolved" != "$shim_dir/gh" ]; then
    echo "::error::gh-shim: PATH не подхватил шим (type -P gh → $resolved, ожидался $shim_dir/gh) — установка сломана, а не отсутствует: без этой проверки gh pr create агента прошёл бы мимо НЕЗАМЕЧЕННО" >&2
    return 1
  fi
  echo "gh-shim: установлен ($shim_dir/gh → настоящий $real_gh) — gh pr create теперь отклоняется в пользу scripts/git/pr-create"
}
