# tasks.md — worker-provider-refusal-green

- [x] Контракт кода возврата: провайдерные классы — job зелёный, `prompt_too_long` и неизвестный класс — громкий die (scripts/worker/task.sh, один case на код возврата и маркер)
- [x] Машиночитаемый маркер `[воркер: провайдерный отказ (#1286)]` в комментарии задачи; читатель — scheduler.py::worker_run_was_provider_refusal
- [x] Исход 1-бис в dispatch_ai_review_rework: зелёный прогон с маркером в окне прогона — инфра-путь доводки (повтор, попытка не в счёт, честный текст)
- [x] Поведенческая гвардия исполнения настоящего блока + каталог (#749): scripts/worker/test/provider-exhaustion-exit-code.smoke.sh, scripts/ci/guards/worker-exhaustion-exit-code-guard.sh; доказана исполненными мутациями
- [x] Сценарии dsh-clients.smoke.sh переведены на зелёный контракт (worker-chain-refusal-green, worker-chain-quota-green, worker-rate-limit-quota)
- [x] Тесты света оркестратора: маркер в окне → инфра-путь без эскалации; маркер вне окна → законная эскалация (scripts/orchestra/test_scheduler.py)
