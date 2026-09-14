# Tasks: pool-producer-orphan-watch (#1277)

- [x] `scripts/lib/pool_issue.py`: обязательный `producer`, маркер тела
      `<!-- pool-issue-producer: <id> -->`, `producer_marker`/`extract_producer`.
- [x] Обновлены все девять вызовов `create_pool_issue` (`pulse_guard.py`,
      `stall_detector.py`, `dependabot_alert_watch.py`, `upstream_drift.py`,
      `health_audit.py`, `merge_health_watch.py`, `scheduler.py` ×2,
      `file_tasks.py`) — передают `producer=`.
- [x] `scripts/lib/producer_orphan_watch.py`: классификатор (маркер +
      legacy-эвристика), пороги из живого замера, `compute_stats`,
      `evaluate_producer`/`evaluate_all` (три исхода, `check_result.CheckResult`).
- [x] `scripts/orchestra/repo_invariants.py`: инвариант 23
      (`check_pool_producer_orphaned`, `fetch_producer_pool_issues`),
      wiring в `build_report`, `ESCALATING_INVARIANTS`, `run_escalations`.
- [x] Тесты: `scripts/lib/test_pool_issue.py` (маркер), `scripts/lib/
      test_producer_orphan_watch.py` (поведенческие, прод-форма из живого
      `gh api graphql`), `scripts/orchestra/test_repo_invariants.py`
      (интеграция wiring, не ломает существующие 221).
- [x] Доказательство мутацией (класс #1194): снятие условия `violation()`
      в `evaluate_producer` красит 3 теста, возврат — снова зелено. Прогон
      до/после — в теле PR.
- [x] Обратный прогон на исторических данных (тем же классификатором и
      порогами): первое срабатывание — 2026-09-07 (21 задача в окне, 0
      закрыто, старейшая открытая 26.3ч), за 7 суток до находки человеком
      (2026-09-14).
