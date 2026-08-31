# Задачи: ai-review-gate

Исполнение — задача #18 (ветка agent/18-ai).

## Реализовано в PR

- [x] `scripts/lib/review_labels.py` — одно место правды: имена меток обоих
      гейтов, `merge_label_gate` (review:ok И ai:ok), `ai_verdicts_to_drop`.
- [x] `scripts/review/ai_review.py` — доверенный транспорт gather/verdict:
      дифф-пак + задача пула + промпт; разбор контракта (последняя строка
      «ВЕРДИКТ: …», неоднозначность = error), маскирование через dsh-ci.sh,
      канонический комментарий (шапка-факты + фенсы задач), метка, проверка
      head перед применением вердикта.
- [x] `scripts/review/ai_prompt.md` — промпт ревьюера (адаптация живого
      паттерна владельца из Harness: контракт вердикта в последней строке,
      блоки ЗАДАЧА/КОНЕЦ ЗАДАЧИ).
- [x] `scripts/review/ai_dsh.sh` — DSH-транспорт без GitHub-токена (пины и
      GLM-патч из общей lib, rc≠0 не роняет шаг — судьбу решает verdict).
- [x] `scripts/review/file_tasks.py` — заведение задач из ревью одной
      командой, идемпотентно по заголовку открытой issue, --dry-run.
- [x] `.github/workflows/ai-review.yml` — триггер labeled:review:ok +
      workflow_dispatch; trust-зона по шагам; concurrency на PR с отменой
      летящего ревью.
- [x] `scripts/review/check_pr.py` — снимает ai:* на каждом запуске
      (вердикт AI привязан к head); метки импортированы из lib.
- [x] `scripts/orchestra/scheduler.py` — гейт слияния через
      review_labels.merge_label_gate (оба гейта); убран задвоенный continue.
- [x] `scripts/review/test_ai_review.py` — 24 теста: контракт вердикта,
      блоки задач, шапка-факты/фенсы, roundtrip комментария, гейт меток
      (обе прод-формы), маскирование. Мутационно доказано: снятие ai-гейта,
      сканирование фактов по всему телу и «вердикт не последний» красят тесты.
- [x] Проводка в repo-ci.yml: pytest-шаг, bash -n для scripts/review/*.sh,
      gh()-гвардия keyword body= расширена на scripts/review/, smoke
      bash-клиентов дополнен клиентом ai_dsh.sh (ответ DSH обязан попасть
      в answer.txt).
- [x] ADR 0007 + дельта-спека + PROTOCOL/PLAYBOOK/INDEX.

## Пост-мерж (проверка живым конвейером)

- [ ] Первый PR после мержа получает содержательный ревью-комментарий и
      метку ai:ok/ai:changes-requested (улики — в задаче #18).
- [ ] Оркестратор НЕ сливает PR с review:ok без ai:ok (видно в отчёте
      merge_queue: «нет вердикта ai:ok»).
- [ ] `python scripts/review/file_tasks.py --pr <N>` заводит предложенные
      ревью задачи с меткой task; повтор — без дублей.
- [ ] Живой прогон контракта вердикта на GLM-5 (ADR 0007, «не подтверждено»):
      если модель регулярно ломает последнюю строку — ужесточить промпт.
