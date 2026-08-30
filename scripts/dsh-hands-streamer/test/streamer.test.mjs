// Юнит-проверки контракта спула dsh-hands-streamer.
// Фикстура — настоящий Session.append над @deepseek-ai/dsh-session (tarball с
// пином целостности), не пересказ событий: правило «тест кормит прод-форму».
// Запуск: node --test scripts/dsh-hands-streamer/test/
// Store-уровень шва (доставка session/event листенеру) доказывается живым
// прогоном dsh headless с плагином, не этими тестами.
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { before, describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { apply } from '../lib/index.js';
import { ALLOWED_EVENT_TYPES, CAPS, MAX_EVENTS_PER_SESSION, parseSpool, projectEventData } from '../lib/core.js';

// Пин фикстуры: dist.integrity из metadata реестра; сверяется с фактически
// скачанным tarball'ом — тот же supply-chain паттерн, что в dsh_task.sh.
const FIXTURE_VERSION = '0.1.1-rc.2';
const FIXTURE_INTEGRITY =
  'sha512-4/cv6X9HPhm47eyRhCu/WZwzrtJKegk5J+0xaxcZ9i8S0smdxP57tqy8a0jkSshLQn7BzMFxneQrlYExrLrDhQ==';

// Фикстура ставится один раз и кэшируется во временном каталоге ОС по пину.
const fixtureRoot = join(
  tmpdir(),
  `dsh-hands-streamer-fixture-${FIXTURE_VERSION}-${createHash('sha256').update(FIXTURE_INTEGRITY).digest('hex').slice(0, 8)}`,
);

before(async () => {
  const installed = join(fixtureRoot, 'node_modules', '@deepseek-ai', 'dsh-session', 'package.json');
  if (existsSync(installed)) return;
  rmSync(fixtureRoot, { recursive: true, force: true });
  mkdirSync(fixtureRoot, { recursive: true });
  const pack = spawnSync('npm', ['pack', `@deepseek-ai/dsh-session@${FIXTURE_VERSION}`], {
    cwd: fixtureRoot,
    encoding: 'utf8',
    shell: process.platform === 'win32',
  });
  if (pack.status !== 0) throw new Error(`npm pack фикстуры упал: ${pack.stderr}`);
  const tgz = readdirSync(fixtureRoot).find((f) => f.endsWith('.tgz'));
  if (!tgz) throw new Error('npm pack не оставил tarball');
  const actual =
    'sha512-' + createHash('sha512').update(readFileSync(join(fixtureRoot, tgz))).digest('base64');
  assert.equal(actual, FIXTURE_INTEGRITY, 'integrity mismatch: tarball фикстуры не совпал с пином');
  const install = spawnSync('npm', ['install', '--no-audit', '--no-fund', `./${tgz}`], {
    cwd: fixtureRoot,
    encoding: 'utf8',
    shell: process.platform === 'win32',
  });
  if (install.status !== 0) throw new Error(`npm install фикстуры упал: ${install.stderr}`);
});

const { Session, SessionId } = await import(
  pathToFileURL(join(fixtureRoot, 'node_modules', '@deepseek-ai', 'dsh-session', 'lib', 'index.js')).href
);

// ── Фикстурные события — то, что реально отдаёт Session.append ───────────────────

const surfaceAppend = { surfaceOp: 'append' };

function appendUser(session, text) {
  return session.append(
    'user/message',
    { id: 'msg-1', role: 'user', content: [{ type: 'text', text }], source: { kind: 'user' } },
    surfaceAppend,
  );
}

function appendAssistant(session, text, turn = 1, step = 1) {
  return session.append(
    'assistant/message',
    {
      turn,
      step,
      message: {
        id: 'msg-a',
        role: 'assistant',
        content: [{ type: 'text', text }],
        source: { kind: 'model', provider: 'test', model: 'test' },
      },
    },
    surfaceAppend,
  );
}

function appendToolCall(session, name, args, turn = 1, step = 1) {
  return session.append('tool/call', { turn, step, callId: 'call-1', name, arguments: args });
}

function appendToolResult(session, text, turn = 1, step = 1) {
  return session.append(
    'tool/result',
    {
      turn,
      step,
      message: {
        id: 'msg-t',
        role: 'user',
        content: [{ type: 'tool-result', toolCallId: 'call-1', content: [{ type: 'text', text }] }],
        source: { kind: 'tool', callId: 'call-1' },
      },
    },
    surfaceAppend,
  );
}

/** apply() над фейковым ctx, который собирает подписки; env подменяется на время apply. */
function captureCtx(env) {
  const saved = { ...process.env };
  for (const key of Object.keys(env)) process.env[key] = env[key];
  const listeners = new Map();
  const logger = {
    warnings: [],
    warn: (message) => logger.warnings.push(message),
  };
  const ctx = {
    logger,
    on: (name, fn) => {
      if (!listeners.has(name)) listeners.set(name, []);
      listeners.get(name).push(fn);
    },
  };
  apply(ctx);
  process.env = saved;
  return { listeners, logger };
}

function readSpool(path) {
  return parseSpool(readFileSync(path, 'utf8'));
}

function readStats(path) {
  return JSON.parse(readFileSync(`${path}.stats.json`, 'utf8'));
}

describe('монтаж', () => {
  it('без HANDS_SPOOL apply — no-op: ни одной подписки', () => {
    const { listeners } = captureCtx({});
    assert.equal(listeners.size, 0);
  });

  it('с HANDS_SPOOL подписки по канону персистенции: created/event/flush/disposed', () => {
    const { listeners } = captureCtx({ HANDS_SPOOL: join(mkdtempSync(join(tmpdir(), 'hands-streamer-')), 'spool.ndjson') });
    for (const name of ['session/created', 'session/event', 'session/flush', 'session/disposed']) {
      assert.ok(listeners.has(name), `нет подписки ${name}`);
    }
  });
});

describe('allowlist и форма спула', () => {
  it('реальная сессия: разрешённые типы в спуле дословно, чужие — в статистике отброшенного', () => {
    const dir = mkdtempSync(join(tmpdir(), 'hands-streamer-'));
    const spool = join(dir, 'spool.ndjson');
    const { listeners } = captureCtx({ HANDS_SPOOL: spool });
    const onEvent = listeners.get('session/event')[0];
    const onCreate = listeners.get('session/created')[0];

    const session = new Session(SessionId('session-fixture-1'));
    onCreate(session);
    appendUser(session, 'задача');
    session.append('turn/start', { turn: 1 });
    session.append('step/start', { turn: 1, step: 1 });
    appendToolCall(session, 'bash', '{"cmd":"ls"}');
    appendToolResult(session, 'file.txt');
    appendAssistant(session, 'готово');
    session.append('step/end', { turn: 1, step: 1 });
    session.append('turn/end', { turn: 1, reason: { kind: 'completed' } });
    session.append('assistant/chunk', { turn: 1, step: 1, chunk: { type: 'text', delta: 'x' } });
    session.append('todo/write', { todos: [] });
    for (const event of session.events) onEvent(session, event);

    const { records, tornTail } = readSpool(spool);
    assert.equal(tornTail, false);
    assert.ok(records.every((r) => ALLOWED_EVENT_TYPES.includes(r.type)), 'в спуле только allowlist');
    // Форма строки: {v, session_id, seq, time, type, data}; seq/time — из конверта DSH.
    for (const record of records) {
      assert.equal(record.v, 1);
      assert.equal(record.session_id, 'session-fixture-1');
      assert.equal(typeof record.seq, 'number');
      assert.equal(typeof record.time, 'number');
      assert.equal(typeof record.type, 'string');
      assert.ok('data' in record);
    }
    assert.deepEqual(
      records.map((r) => [r.seq, r.type]),
      [
        [0, 'user/message'],
        [1, 'turn/start'],
        [2, 'step/start'],
        [3, 'tool/call'],
        [4, 'tool/result'],
        [5, 'assistant/message'],
        [6, 'step/end'],
        [7, 'turn/end'],
      ],
    );
    // data переносится дословно (caps не сработали — всё короткое).
    assert.equal(records[3].data.name, 'bash');
    assert.equal(records[3].data.arguments, '{"cmd":"ls"}');
    assert.equal(records[5].data.message.content[0].text, 'готово');
    assert.equal(records[7].data.reason.kind, 'completed');
    // Гвардия ложного усечения: короткие события НЕ помечаются truncated —
    // флаг в журнале обязан означать реальную обрезку, а не наличие текста.
    assert.equal(records[3].data.truncated, undefined);
    assert.equal(records[4].data.truncated, undefined, 'tool/result без обрезки не помечается');
    assert.equal(records[5].data.truncated, undefined, 'assistant/message без обрезки не помечается');
    assert.equal(records[4].data.original_size, undefined);
    assert.equal(records[5].data.original_size, undefined);
    // Статистика рядом со спулом: 8 принято, потоковое и todo — отброшенное по типам.
    const stats = readStats(spool);
    assert.equal(stats.accepted, 8);
    assert.equal(stats.dropped['assistant/chunk'], 1);
    assert.equal(stats.dropped['todo/write'], 1);
    assert.equal(stats.capped, false);
  });

  it('листенер не бросает даже на чуждом объекте события', () => {
    const dir = mkdtempSync(join(tmpdir(), 'hands-streamer-'));
    const { listeners } = captureCtx({ HANDS_SPOOL: join(dir, 'spool.ndjson') });
    const onEvent = listeners.get('session/event')[0];
    assert.doesNotThrow(() => onEvent({ id: 'session-x' }, null));
    assert.doesNotThrow(() => onEvent({ id: 'session-x' }, { type: 'turn/start' }));
  });
});

describe('caps payload', () => {
  it('assistant/message: текст усечён до 48000, truncated + исходный размер', () => {
    const dir = mkdtempSync(join(tmpdir(), 'hands-streamer-'));
    const spool = join(dir, 'spool.ndjson');
    const { listeners } = captureCtx({ HANDS_SPOOL: spool });
    const onEvent = listeners.get('session/event')[0];
    const session = new Session(SessionId('session-caps-a'));
    onEvent(session, appendAssistant(session, 'ж'.repeat(CAPS.assistantText + 500)));

    const { records } = readSpool(spool);
    assert.equal(records.length, 1);
    const data = records[0].data;
    assert.equal(data.truncated, true);
    assert.equal(data.original_size, CAPS.assistantText + 500);
    assert.equal(data.message.content[0].text.length, CAPS.assistantText);
  });

  it('tool/call: arguments усечены до 16000; короткие не трогаются', () => {
    const dir = mkdtempSync(join(tmpdir(), 'hands-streamer-'));
    const spool = join(dir, 'spool.ndjson');
    const { listeners } = captureCtx({ HANDS_SPOOL: spool });
    const onEvent = listeners.get('session/event')[0];
    const session = new Session(SessionId('session-caps-b'));
    const big = JSON.stringify({ cmd: 'x'.repeat(20000) });
    onEvent(session, appendToolCall(session, 'bash', big));
    onEvent(session, appendToolCall(session, 'ls', '{}', 1, 2));

    const { records } = readSpool(spool);
    assert.equal(records[0].data.arguments.length, CAPS.toolArguments);
    assert.equal(records[0].data.truncated, true);
    assert.equal(records[0].data.original_size, big.length);
    assert.equal(records[1].data.arguments, '{}');
    assert.equal(records[1].data.truncated, undefined);
  });

  it('tool/result: текст result-блока усечён до 16000', () => {
    const dir = mkdtempSync(join(tmpdir(), 'hands-streamer-'));
    const spool = join(dir, 'spool.ndjson');
    const { listeners } = captureCtx({ HANDS_SPOOL: spool });
    const onEvent = listeners.get('session/event')[0];
    const session = new Session(SessionId('session-caps-c'));
    onEvent(session, appendToolResult(session, 'y'.repeat(CAPS.toolResultText + 10)));

    const { records } = readSpool(spool);
    const text = records[0].data.message.content[0].content[0].text;
    assert.equal(text.length, CAPS.toolResultText);
    assert.equal(records[0].data.truncated, true);
    assert.equal(records[0].data.original_size, CAPS.toolResultText + 10);
  });

  it('короткие assistant/message и tool/result не помечаются усечёнными', () => {
    // Регрессия ложного truncated:true: флаг ставился по наличию текста
    // (originalSize > 0), а не по факту обрезки — журнал молча врал про
    // усечение на каждом коротком сообщении.
    const shortAssistant = projectEventData(
      'assistant/message',
      structuredClone({ message: { content: [{ type: 'text', text: 'готово' }] } }),
    );
    assert.equal(shortAssistant.data.truncated, undefined);
    assert.equal(shortAssistant.data.original_size, undefined);
    assert.equal(shortAssistant.data.message.content[0].text, 'готово');
    const shortResult = projectEventData(
      'tool/result',
      structuredClone({ message: { content: [{ type: 'tool-result', content: [{ type: 'text', text: 'ok' }] }] } }),
    );
    assert.equal(shortResult.data.truncated, undefined);
    assert.equal(shortResult.data.original_size, undefined);
    assert.equal(shortResult.data.message.content[0].content[0].text, 'ok');
    // Граница: ровно в потолок — ещё не усечение.
    const exact = projectEventData(
      'tool/call',
      structuredClone({ name: 'x', arguments: 'a'.repeat(CAPS.toolArguments) }),
    );
    assert.equal(exact.data.truncated, undefined);
    assert.equal(exact.data.arguments.length, CAPS.toolArguments);
  });

  it('deep-frozen событие не мутируется caps — клон пишется в спул, оригинал нетронут', () => {
    const session = new Session(SessionId('session-frozen'));
    const event = appendAssistant(session, 'не изменится');
    assert.ok(Object.isFrozen(event));
    const { data } = projectEventData('assistant/message', event.data);
    data.message.content[0].text = 'изменённый клон';
    assert.equal(event.data.message.content[0].text, 'не изменится');
  });
});

describe('аварийный клапан MAX_EVENTS', () => {
  it('превышение останавливает запись и ставит capped: true', () => {
    const dir = mkdtempSync(join(tmpdir(), 'hands-streamer-'));
    const spool = join(dir, 'spool.ndjson');
    const { listeners, logger } = captureCtx({ HANDS_SPOOL: spool });
    const onEvent = listeners.get('session/event')[0];
    const session = new Session(SessionId('session-cap-valve'));
    for (let i = 0; i < MAX_EVENTS_PER_SESSION + 10; i++) {
      onEvent(session, session.append('turn/start', { turn: i + 1 }));
    }
    const { records } = readSpool(spool);
    assert.equal(records.length, MAX_EVENTS_PER_SESSION);
    const stats = readStats(spool);
    assert.equal(stats.capped, true);
    assert.equal(stats.accepted, MAX_EVENTS_PER_SESSION);
    assert.ok(logger.warnings.some((w) => w.includes('MAX_EVENTS')), 'клапан обязан шуметь в warn');
  });
});

describe('torn tail', () => {
  it('последняя строка без \\n не читается и помечается как обрыв', () => {
    const dir = mkdtempSync(join(tmpdir(), 'hands-streamer-'));
    const spool = join(dir, 'spool.ndjson');
    writeFileSync(
      spool,
      '{"v":1,"session_id":"s","seq":0,"time":1,"type":"turn/start","data":{"turn":1}}\n' +
        '{"v":1,"session_id":"s","seq":1,"time":2,"type":"turn/en',
    );
    const { records, tornTail } = parseSpool(readFileSync(spool, 'utf8'));
    assert.equal(tornTail, true);
    assert.equal(records.length, 1);
    assert.equal(records[0].type, 'turn/start');
  });

  it('каждая строка спула плагина завершается \\n — при живой записи torn tail не возникает', () => {
    const dir = mkdtempSync(join(tmpdir(), 'hands-streamer-'));
    const spool = join(dir, 'spool.ndjson');
    const { listeners } = captureCtx({ HANDS_SPOOL: spool });
    const onEvent = listeners.get('session/event')[0];
    const session = new Session(SessionId('session-lines'));
    onEvent(session, session.append('turn/start', { turn: 1 }));
    onEvent(session, session.append('turn/end', { turn: 1, reason: { kind: 'completed' } }));
    const raw = readFileSync(spool, 'utf8');
    assert.ok(raw.endsWith('\n'));
    const parsed = parseSpool(raw);
    assert.equal(parsed.tornTail, false);
    assert.equal(parsed.records.length, 2);
  });
});
