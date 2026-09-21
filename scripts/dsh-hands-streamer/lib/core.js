// Чистые части dsh-hands-streamer: allowlist, caps, форма NDJSON-строки спула.
// Единственное место правды по форме спула ({v, session_id, seq, time, type, data})
// и по потолкам payload. Никакого I/O и никакой сети — тесты кормятся этими
// функциями напрямую, листенеры в index.js только вызывают их и пишут файл.
//
// Словарь событий — SessionEventMap (@deepseek-ai/dsh-session 0.1.1-rc.2,
// lib/types/types.d.ts); конверт {type, seq, time, data} переносится дословно,
// кроме полей, попавших под caps: усечение ЗАЯВЛЕНО, не замаскировано — но
// заявлено ВНУТРИ усечённого текста, а не лишними членами data (issue #1404,
// см. projectEventData).

/** Версия формы спула; при несовместимой смене формы растёт. */
export const SPOOL_VERSION = 1;

/**
 * Allowlist-фильтр стрима (design dsh-streaming, «Фильтр — allowlist, не
 * denylist»): незнакомый тип проходит мимо по построению, бюджет строк
 * журнала (100k/сутки) защищён от любого нового потокового типа upstream.
 * assistant/chunk (токен-стрим) не стримится намеренно.
 */
export const ALLOWED_EVENT_TYPES = Object.freeze([
  'turn/start',
  'turn/end',
  'step/start',
  'step/end',
  'user/message',
  'assistant/message',
  'tool/call',
  'tool/result',
]);

export function isAllowedType(type) {
  return ALLOWED_EVENT_TYPES.includes(type);
}

/**
 * Потолки payload — константы в одном месте (design dsh-streaming, «Caps на
 * payload»). assistantText покрывает текстовые блоки ассистентского сообщения
 * (text и reasoning); toolArguments — сырую JSON-строку tool/call;
 * toolResultText — текст result-блока tool/result.
 */
export const CAPS = Object.freeze({
  assistantText: 48_000,
  toolArguments: 16_000,
  toolResultText: 16_000,
});

/**
 * Аварийный клапан на сессию: превышение — запись прекращается, в статистику
 * пишется capped: true (громко, bash публикует warn), а не тихий рост спула.
 */
export const MAX_EVENTS_PER_SESSION = 5_000;

/**
 * Текст пометки усечения (#1404). Живёт ВНУТРИ усечённого текста, потому что
 * схема v0 у `data` закрытая: любой лишний член — отказ миграции целиком
 * (`assertReleasedEventPayload` → `data has unexpected member`). Пометка
 * добавляет к усечённому блоку константу порядка 50 символов СВЕРХ потолка —
 * сознательно: потолок нужен, чтобы ограничить рост спула, и полсотни байт
 * на событие его не ломают, а молчаливое усечение ломает журнал.
 */
export function truncationNotice(originalSize) {
  return `\n…[обрезано харнесом: было ${originalSize} символов]`;
}

function cutText(text, budget) {
  return typeof text === 'string' && text.length > budget ? text.slice(0, budget) : text;
}

/** Дописывает пометку в ПОСЛЕДНИЙ блок с текстом — тот, на котором бюджет
 * кончился. Блоков без текста не касается. */
function appendNoticeToBlocks(blocks, originalSize) {
  if (!Array.isArray(blocks)) return;
  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    const block = blocks[i];
    if (block !== null && typeof block === 'object' && typeof block.text === 'string') {
      block.text += truncationNotice(originalSize);
      return;
    }
  }
}

/**
 * Применяет потолок к массиву контент-блоков: бюджет расходуется жадно по
 * блокам с полем text (text и reasoning); неизвестные блоки не трогаются.
 * Возвращает суммарный исходный размер текстов и факт РЕАЛЬНОЙ обрезки:
 * флаг truncated ставится только когда хотя бы один блок действительно
 * укорочен (или опустошён). Заявлять усечение, которого не было, — тот же
 * silent-wrong: журнал не должен врать ни про одно поле.
 */
function capContentBlocks(blocks, budget) {
  if (!Array.isArray(blocks)) return { originalSize: 0, cut: false };
  let originalSize = 0;
  let cut = false;
  let remaining = budget;
  for (const block of blocks) {
    if (block === null || typeof block !== 'object') continue;
    if (typeof block.text !== 'string') continue;
    originalSize += block.text.length;
    if (remaining > 0) {
      const trimmed = cutText(block.text, remaining);
      if (trimmed !== block.text) {
        block.text = trimmed;
        cut = true;
      }
      remaining -= block.text.length;
    } else {
      block.text = '';
      cut = true;
    }
  }
  return { originalSize, cut };
}

/**
 * Caps одного события. Событие из Session.append — deep-frozen, поэтому data
 * сначала клонируется (structuredClone), мутируется только клон; форма data
 * остаётся формой DSH ДОСЛОВНО — ни одного лишнего члена.
 *
 * Класс, оплаченный проданным инцидентом (issue #1404, измерено механизмом
 * #1379): прежняя редакция дописывала в `data` поля `truncated`/`original_size`
 * и проверяла коллизии со словарём SessionEventMap — но не с ВАЛИДАТОРОМ
 * формата. Схема v0 у `data` закрытая: `assertReleasedEventPayload`
 * (@deepseek-ai/dsh-session-format-v0-to-v1) знает для tool/result ровно
 * {turn, step, message} + опциональные {error, meta}, для tool/call —
 * {turn, step, callId, name, arguments} БЕЗ опциональных, для
 * assistant/message — {turn, step, message} + {usage, interrupted}. Любой
 * лишний член → `data has unexpected member` → вся сессия не мигрирует.
 * Цена, увиденная в первом же прогоне #1379: 127 карантинных сессий прода из
 * 165 — то есть 77% карантина породили эти две строки.
 *
 * Факт усечения при этом НЕ теряется (fail loud остаётся fail loud), и у него
 * ДВА носителя: пометка `truncationNotice` внутри самого усечённого текста —
 * для человека, читающего журнал, — и возвращаемый флаг `truncated`, который
 * index.js считает в `stats.truncated` и публикует как stream_note — для
 * машины. Второй носитель до #1405 существовал только на бумаге: счётчик был
 * в объекте stats, но не инкрементился никем (находка ai-ревью). Пометка
 * появляется только при фактической обрезке: неурезанное событие уходит в
 * спул дословно.
 */
export function projectEventData(type, data) {
  if (data === null || typeof data !== 'object') return { data, truncated: false, originalSize: 0 };
  const out = structuredClone(data);
  let originalSize = 0;
  let cut = false;
  if (type === 'assistant/message') {
    const content = out.message?.content;
    ({ originalSize, cut } = capContentBlocks(content, CAPS.assistantText));
  } else if (type === 'tool/call') {
    const before = out.arguments;
    if (typeof before === 'string') {
      out.arguments = cutText(before, CAPS.toolArguments);
      if (out.arguments !== before) {
        cut = true;
        originalSize = before.length;
      }
    }
  } else if (type === 'tool/result') {
    // message.content — ровно один ToolResultBlock, текст живёт в его content.
    const block = Array.isArray(out.message?.content) ? out.message.content[0] : undefined;
    if (block !== null && typeof block === 'object') {
      ({ originalSize, cut } = capContentBlocks(block.content, CAPS.toolResultText));
    }
  }
  if (cut) {
    if (type === 'assistant/message') {
      appendNoticeToBlocks(out.message?.content, originalSize);
    } else if (type === 'tool/call') {
      // arguments — JSON-строка, и обрезка её уже сломала: дописать пометку
      // хуже не делает, а причину называет. Молча отдать обрубок JSON —
      // ровно silent-wrong.
      out.arguments += truncationNotice(originalSize);
    } else if (type === 'tool/result') {
      const block = Array.isArray(out.message?.content) ? out.message.content[0] : undefined;
      if (block !== null && typeof block === 'object') appendNoticeToBlocks(block.content, originalSize);
    }
  }
  return { data: out, truncated: cut, originalSize };
}

/** Строка спула: конверт события DSH плюс наши session_id/v; data уже с caps. */
export function toSpoolLine(sessionId, event, cappedData) {
  return {
    v: SPOOL_VERSION,
    session_id: sessionId,
    seq: event.seq,
    time: event.time,
    type: event.type,
    data: cappedData,
  };
}

/** Синхронная форма записи: ровно одна строка с завершающим \n. */
export function serializeSpoolLine(line) {
  return `${JSON.stringify(line)}\n`;
}

/**
 * Парсер спула с семантикой committed-prefix: последняя строка без \n — torn
 * tail (обрыв записи) — не читается и сообщается отдельно. Полная строка с
 * не-JSON содержимым — громкая ошибка: тихо пропускать сломанное нельзя.
 */
export function parseSpool(text) {
  const records = [];
  const parts = text.split('\n');
  const last = parts.pop();
  const tornTail = last !== '';
  for (const part of parts) {
    records.push(JSON.parse(part));
  }
  return { records, tornTail };
}
