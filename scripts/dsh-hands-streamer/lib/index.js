// dsh-hands-streamer — живой стрим событий сессии DSH в журнал морды edge-harness.
// Шов подписки — канон PersistenceCoordinator.installWritePath()
// (dsh-session-persistence/lib/index.js:1132-1163): четыре подписки
// session/created, session/event, session/flush, session/disposed.
//
// Свойства шва, на которых держится дизайн (dsh-session/lib/index.js):
// - session/event — observe-only firehose: «the hot path never blocks on I/O»,
//   сбои листенера изолированы per-listener и гасятся в ctx.logger.warn —
//   сломанный стример не может уронить агента;
// - session/flush попадает в барьер sessions.flush() dsh-headless, который
//   бросает первую ошибку листенера: поэтому здесь НИКОГДА нет сети и ничего,
//   что умеет падать от журнала, — write-through уже обеспечен синхронным
//   append'ом каждой строки (appendFileSync), буфера у плагина нет.
//
// Сеть в плагине отсутствует по построению: транспорт (drain, journal-seq,
// ретраи, redact) ведёт bash-клиент dsh_task.sh. Плагин только пишет
// NDJSON-спул и файл статистики рядом.
import { appendFileSync, mkdirSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import {
  ALLOWED_EVENT_TYPES,
  MAX_EVENTS_PER_SESSION,
  isAllowedType,
  projectEventData,
  serializeSpoolLine,
  toSpoolLine,
} from './core.js';

/** Stable Cordis plugin name — строка `- id:` в cordis.patch.yml. */
const name = 'hands-streamer';

/** Предупреждение без возможности уронить дерево: logger + stderr, оба best-effort. */
function warn(ctx, message) {
  try {
    ctx.logger?.warn?.(`hands-streamer: ${message}`);
  } catch {
    /* logger не обязан быть */
  }
  try {
    process.stderr.write(`dsh-hands-streamer: ${message}\n`);
  } catch {
    /* stderr может быть закрыт */
  }
}

/**
 * Монтирует стример. Без HANDS_SPOOL — no-op с warn: чужой ручной прогон
 * headless не должен ломаться (клиент рук dsh_task.sh всегда задаёт
 * HANDS_SPOOL, так что «нет переменной» для наших рук — поломка монтажа,
 * которую ловит dsh_task.sh отдельной громкой проверкой dump-config).
 * @param ctx - cordis-контекст.
 */
function apply(ctx) {
  const spoolPath = process.env.HANDS_SPOOL;
  if (!spoolPath) {
    warn(ctx, 'HANDS_SPOOL не задан — плагин no-op (ручной прогон без клиента рук)');
    return;
  }
  const statsPath = `${spoolPath}.stats.json`;
  mkdirSync(dirname(spoolPath), { recursive: true });

  // Статистика: счётчики отброшенного по типам + аварийный клапан. Пишется на
  // каждое событие (temp + rename — атомарная подмена, kill -9 посреди прогона
  // не оставит поломанный JSON) — bash один раз в конце публикует её как
  // stream_note; видимость без строк на каждое событие в журнале.
  const stats = { accepted: 0, dropped: {}, truncated: 0, capped: false, max_events: MAX_EVENTS_PER_SESSION };
  const written = new Map(); // session_id -> записано строк (для клапана MAX_EVENTS)

  const persistStats = () => {
    try {
      const tmp = `${statsPath}.tmp`;
      writeFileSync(tmp, `${JSON.stringify(stats, null, 2)}\n`);
      renameSync(tmp, statsPath);
    } catch (error) {
      warn(ctx, `статистика не записана: ${error instanceof Error ? error.message : String(error)}`);
    }
  };

  const noteDrop = (type) => {
    stats.dropped[type] = (stats.dropped[type] ?? 0) + 1;
  };

  ctx.on('session/created', (session) => {
    written.set(session.id, 0);
    persistStats();
  });

  ctx.on('session/event', (session, event) => {
    // Контейнинг дублирован намеренно: upstream изолирует сбой листенера, но
    // наш собственный инвариант — сбой стримера никогда не влияет на append.
    try {
      if (!isAllowedType(event.type)) {
        noteDrop(event.type);
        return;
      }
      const count = written.get(session.id) ?? 0;
      if (count >= MAX_EVENTS_PER_SESSION) {
        if (!stats.capped) {
          stats.capped = true;
          warn(ctx, `MAX_EVENTS=${MAX_EVENTS_PER_SESSION} превышен для ${session.id} — запись остановлена`);
        }
        noteDrop(event.type);
        return;
      }
      const { data } = projectEventData(event.type, event.data);
      appendFileSync(spoolPath, serializeSpoolLine(toSpoolLine(session.id, event, data)));
      written.set(session.id, count + 1);
      stats.accepted += 1;
    } catch (error) {
      warn(ctx, `событие ${event?.type} не записано: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      persistStats();
    }
  });

  ctx.on('session/flush', () => {
    // Write-through уже обеспечен синхронным append'ом каждой строки —
    // сливать нечего. Листенер обязан существовать (канон) и обязан уметь
    // падать только от диска, никогда от сети: он внутри барьера
    // sessions.flush() dsh-headless.
  });

  ctx.on('session/disposed', () => {
    persistStats();
  });
}

export { ALLOWED_EVENT_TYPES, apply, name };
