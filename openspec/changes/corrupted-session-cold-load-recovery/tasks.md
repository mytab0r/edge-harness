Задача: #809.

# Tasks: corrupted-session-cold-load-recovery

- [x] 1. `scripts/lib/dsh-edge-session.sh::dsh_edge_session_begin` — фоллбэк
      на один новый session_id (`<исходный>-r<GITHUB_RUN_ID>`) при отказе
      `session.create`, содержащем `failed validation` в тексте ошибки;
      любая другая причина — без фоллбэка, как раньше. stdout несёт РЕАЛЬНО
      использованный id.
- [x] 2. `scripts/worker/task.sh`, `scripts/hands/dsh_task.sh` — читают
      возврат `dsh_edge_session_begin`, не подставляют исходный `HARNESS_SID`
      вручную.
- [x] 3. Смоук-сценарий `scripts/lib/test/dsh-clients.smoke.sh`
      (`worker-corrupted-session`) — прод-форма ошибки дословно из живых
      прогонов `worker.yml` 34455120330/34441499974; проверяет два вызова
      `session.create` и что задача доводится до DSH/отчёта.
- [x] 4. Мутационная проверка изолированной логики фоллбэка (сеть vs порча,
      повторная порча фоллбэка) — доказано локально прогоном/откатом фикса
      (репозиторий не даёт CI на Windows-машине разработчика, см. design.md).

## Закрывающая проверка (после слияния, живой прогон)

- [ ] Живой ручной прогон `worker.yml` (`workflow_dispatch`, `task: 716` или
      `140`) проходит шаг «Задача через DSH headless» ЗА точку создания
      сессии (видно продолжение — установка DSH/провайдер), предохранитель
      диспатча (#847/#205) снимается тем же прогоном или следующим циклом.
