# verify-agent-claims — задачи

- [x] Контракт блоков и чистая логика (`mutation_claim.py`): парсинг заявлений, мутационный
  прогон с фазами и вердиктами, класс-закрыт, непроверяемые формулировки. Исполнитель:
  воркер PR #1028. Приёмка: 35 тестов двух файлов зелёные; фикстуры #893 (байт-копии
  508e6899) ловятся как `false_claim`.
- [x] Живая проверка PR (`pr_mutation_claim_check.py`) + регистрация каталогом #749
  (`scripts/ci/guards/mutation-claim-guard.sh`), без правки repo-ci.yml. Исполнитель:
  воркер PR #1028. Приёмка: glue-прогон на реальном событии `pull_request` PR #893 ловит
  контрактное нарушение.
- [x] Находки ai-review PR #1028 (круг 2): заявление исполняется всегда, когда блок в теле;
  аварийное восстановление дерева с `tree_restored`; честный газ тормоза. Исполнитель:
  воркер PR #1028. Приёмка: тесты `test_false_claim_on_non_guard_pr_fails_check`,
  `test_run_mutation_proof_revert_failure_restores_tree`, `test_poisoned_tree_stops_claim_loop`.
- [x] Находки ai-review PR #1028 (круг 3): ADR 0026 (выбор варианта 1, отказ от
  критик-прохода, разведение форматов) + эта дельта-спека; адресная ошибка на
  `MUTATION-PROOF`-форму в секции заявления; параметризованные pytest-id в «Тест:»;
  битый ERE — `::error::` с газом, не трейсбек. Исполнитель: воркер PR #1028. Приёмка:
  тесты `test_parse_mutation_claims_mutation_proof_block_gets_targeted_error`,
  `test_parse_mutation_claims_accepts_parameterized_test_id`,
  `test_broken_ere_is_error_with_gas_not_traceback`; мутация механизма перегнана живьём
  (дословный вывод в ADR 0026).
