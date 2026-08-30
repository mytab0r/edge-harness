// Чистые части dsh-hands-streamer: allowlist, caps, форма NDJSON-строки спула.
// Единственное место правды по форме спула ({v, session_id, seq, time, type, data})
// и по потолкам payload. Никакого I/O и никакой сети — тесты кормятся этими
// функциями напрямую, листенеры в index.js только вызывают их и пишут файл.
//
// Словарь событий — SessionEventMap (@deepseek-ai/dsh-session 0.1.1-rc.2,
// lib/types/types.d.ts); конверт {type, seq, time, data} переносится дословно,
// кроме полей, попавших под caps: усечение заявлено (truncated: true + исходный
// размер), не замаскировано.

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

function cutText(text, budget) {
  return typeof text === 'string' && text.length > budget ? text.slice(0, budget) : text;
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
 * остаётся формой DSH — добавляются только объявляющие усечение поля
 * truncated/original_size (коллизий со словарём SessionEventMap нет: там нет
 * ни truncated, ни original_size). Поля появляются только при фактической
 * обрезке: неурезанное событие уходит в спул без них.
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
    out.truncated = true;
    out.original_size = originalSize;
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
