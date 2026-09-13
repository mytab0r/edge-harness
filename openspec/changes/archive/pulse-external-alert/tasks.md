# Tasks: pulse-external-alert (#1103)

## Backend (cf-worker)

- [x] Новая таблица `pulse_alert` (`cf-worker/src/harness.ts`, SCHEMA) — дедуп-флаг
      живости пульса, отдельная от `pulse` (нет прецедента ALTER TABLE на уже
      развёрнутую строку — новая таблица безопасна при любом прод-состоянии).
- [x] `pulseAlertText(now, lastPulse)` — чистая функция текста алерта: факт
      (конкретный `detail` либо самодостаточный текст ветки `pulseStale`) +
      план (что делает самовосстановление, порог эскалации на ручную проверку).
- [x] `#recordPulseAlertFlag`/`#tickPulseAlert` — дедуп переходов
      здоров↔нездоров, переиспользует `storageReadyAlertDecision` (не вторая
      копия решения), доставка через существующий `#telegramApi`.
- [x] Вызов `#tickPulseAlert` из общего `#dispatchOrchestraTick` — общий код
      для `alarm()` и `scheduledTick()` (Cron Trigger), одно место правды.
- [x] `docs/decisions/0018-pulse-alert-lives-in-do.md` — решение и три
      отклонённые альтернативы (внешний cron, GH Actions watchdog на
      `schedule`, ретрай средствами workflow).

## Тесты

- [x] `cf-worker/test/pulse-alert.spec.ts`:
      - серия из восьми подряд падающих тиков (403, прод-форма живого
        инцидента) → ровно один incident-алерт, восстановление → ровно один
        recovery-алерт, второй здоровый тик подряд — без повторного recovery;
      - страховка Cron Trigger (`scheduledTick()`, alarm «подвис» —
        `pulseStale`) тоже шлёт алерт тем же общим кодом;
      - `TELEGRAM_CHAT_ID` не задан — тик не падает, не звонит («возможности
        нет»);
      - таблица `pulse_alert` недоступна — тик не падает, не спамит, первый
        удавшийся тик догоняет.
- [x] Полный прогон существующего набора (`npx vitest run`) — 172/172, ничего
      не сломано; `npx tsc --noEmit` — чисто; `npm run check`
      (check-frontend-contract.mjs) — OK, контракт фронта не тронут.

### Мутационное доказательство (дословно)

Снята строка `this.#tickPulseAlert(now, {...})` в `#dispatchOrchestraTick`
(закомментирована). `npx vitest run test/pulse-alert.spec.ts`:

```
 Test Files  1 failed (1)
      Tests  3 failed | 1 passed (4)
```

(упали: «цепочка рвётся … один incident-алерт; восстановление …», «alarm
подвис — страховка scheduledTick тоже шлёт алерт», «таблица pulse_alert
недоступна …» — все три ждут `sendMessage`, получают `[]`).

Строка возвращена. `npx vitest run`:

```
 Test Files  5 passed (5)
      Tests  172 passed (172)
```

## Openspec

- [x] `proposal.md`, `specs/journal-tasks-hands/spec.md` этого change'а.
