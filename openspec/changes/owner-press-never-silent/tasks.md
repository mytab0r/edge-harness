# tasks.md — owner-press-never-silent

- [x] Отказ несёт машинный код причины и газ: `DecisionRefused(reason, message, gas)`, четыре различимых кода (scripts/orchestra/apply_owner_decision.py); критерий — тест `test_every_refusal_names_its_gas`
- [x] Два адресата отказа: Telegram владельцу и комментарий-след в задаче, дедуп по паре причина+вариант (`notify_refusal`, `refusal_marker`); критерий — `test_notify_refusal_answers_both_addressees_with_reason_and_gas`, `test_notify_refusal_does_not_repeat_the_trace_but_still_answers_the_press`
- [x] Провал доставки называется вслух по каждому каналу отдельно; критерий — `test_notify_refusal_says_loudly_when_a_channel_did_not_deliver`
- [x] Текст отказа про пустую подпись называет факт и не предлагает гипотез (AGENTS.md, «алерт не гадает»); критерий — `test_signature_missing_alert_states_a_fact_and_does_not_offer_guesses`
- [x] Страховка: текст отказа не может нести маркер решения, иначе комментарий не уходит вовсе; критерий — `test_assert_no_decision_marker_actually_refuses_such_a_text`, `test_notify_refusal_posts_nothing_if_the_text_would_apply_the_decision`
- [x] Сквозной прогон настоящей точки входа процессом (gh вынут из PATH, Telegram-переменных нет): код возврата 1, причина, газ и честное «не доставлено» в логе; критерий — `test_entrypoint_refusal_is_loud_names_the_gas_and_admits_undelivered`
- [x] Секреты Telegram проброшены в job `owner-decision`, число секретов названо синхронно в workflow и в докстринге модуля
- [x] Доказано исполненными мутациями (три штуки, числа — в теле PR #1401)
