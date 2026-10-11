## Tasks

- [x] Убрать запись `provider-registry` из `dsh-edge/plugins.json`
- [x] Удалить `plugins-src/provider-registry`, снять шаг юнит-тестов в `repo-ci.yml`
- [x] Пересобрать и перевыпустить `plugin-manager` 0.1.13 и `agents-tasks` 0.1.4 с ростером из 5
- [x] Перевести `registry-integration/check.mjs` и вызов в деплое на родной механизм
- [x] Перевести прод-канарейку деплоя
- [x] Обновить `docs/research/11-dsh-edge.md`
- [ ] Зелёный полный прогон `deploy-dsh-edge.yml` после мержа, `/api/health` отдаёт 0.26.0
