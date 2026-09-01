#88

## Что сделано
Playbook автономного воркера (`docs/agents/WORKER-PLAYBOOK.md`) уже находится в `main` (добавлен в #87, коммит 6e25dc7) и используется транспортным скриптом воркера (`scripts/worker/task.sh`) как вход в работу: скрипт читает playbook и включает его в промпт к DSH headless.

Дополнительных изменений кода не требуется — задача выполняет критерий готовности: «Playbook в main, используется воркером как вход в работу».

## Чем доказано (видимый результат)
1. Файл `docs/agents/WORKER-PLAYBOOK.md` присутствует в `main` (и в этой ветке, идентичной `main`).
2. `scripts/worker/task.sh:173-174` — проверка наличия playbook перед запуском: `[ -f "$PLAYBOOK_FILE" ] || die "Нет docs/agents/WORKER-PLAYBOOK.md — воркер без playbook не работает"`.
3. `scripts/worker/task.sh:213-214` — playbook читается и встраивается в промпт агента: `cat "$PLAYBOOK_FILE"`.
4. Workflow `.github/workflows/worker.yml` запускает `scripts/worker/task.sh` с необходимыми секретами и переменными (DSH провайдер, Telegram для эскалации, heartbeat).
5. Правила playbook соответствуют живой практике: нет вопросов владельцу, единственная эскалация — `blocked` + Telegram, конвейер через `gh issue` + `scripts/git/task-branch` + PR без `Closes/Fixes/Resolves`, два гейта ревью (`review:ok` + `ai:ok`), DSH механика (tarball-only установка, профиль headless, патч `cordis.patch.yml` для модели).

## Пост-мерж проверка
Не требуется — ветка идентична `main`, слияние не меняет состояние репозитория. Playbook уже работает в продакшн-воркере.

## Чек-лист
- [x] Playbook в `main`
- [x] Playbook читается `scripts/worker/task.sh` и встраивается в промпт DSH
- [x] Правила соответствуют PROTOCOL.md и AGENTS.md
- [x] Эскалация через `blocked` + Telegram реализована в `task.sh:351-367`
- [x] Конвейер задачи: назначение → ветка через `scripts/git/task-branch` → PR
- [x] DSH механика: tarball-only, профиль headless, патч модели через `cordis.patch.yml`
- [x] Нет `Closes/Fixes/Resolves` в теле PR
- [x] Первая строка тела PR — `#88`