// Модель автоматизации (#116): {триггер → задача агенту → отчёт}.
// Единственное место правды формы конфига: валидацию проходит PUT /api/automations,
// ту же форму читает раннер (scripts/automations/run.sh) и секция UI.
// Здесь только чистые функции (валидация, решение «пора ли», период прогона) —
// проводка (SQL, dispatch, webhook) тонкая и живёт в harness.ts, по образцу
// dshEdgeUpdateDecision/handsAreAlive.

import { AUTOMATIONS, LIMITS } from "./config";

/** Триггер: расписание (интервал в часах, 168 = еженедельно), внешний webhook,
 *  событие журнала (kind события; события самих автоматизаций исключены —
 *  гвардия петли по префиксу task_id, см. AUTOMATIONS.runTaskPrefix). */
export type AutomationTrigger =
  | { type: "schedule"; intervalHours: number }
  | { type: "webhook" }
  | { type: "journal"; kind: string };

/** Задача: встроенный сборщик дайджеста, прямая работа раннера по шаблону
 *  текста (DSH headless) или задача в пул (issue с меткой `task`; воркера
 *  поднимает существующий пульс orchestra). */
export type AutomationTask =
  | { kind: "digest" }
  | { kind: "hands"; text: string }
  | { kind: "pool"; title: string; body: string };

/** Канал отчёта. Читает раннер: slack — Web API chat.postMessage в target,
 *  telegram — sendMessage в TELEGRAM_CHAT_ID (секреты каналов — в секретах
 *  репозитория, значений в конфиге нет и быть не может). */
export type ReportChannel = { type: "slack"; target: string } | { type: "telegram" };

export interface AutomationConfig {
  enabled: boolean;
  trigger: AutomationTrigger;
  task: AutomationTask;
  report: { channels: ReportChannel[] };
}

export const AUTOMATION_ID_PATTERN = "^[a-z0-9][a-z0-9-]{0,47}$";

/** Именованный доступ эмиттеров к служебному kind журнала автоматизаций
 *  (список — AUTOMATIONS.reservedJournalKinds в config.ts, единственное место
 *  правды). Параметр типизирован самим списком: опечатка в kind или новый вид
 *  события, не внесённый в список, — ошибка компиляции, а не тихое расхождение
 *  «список валидации знает, эмиттер пишет другое» (находка AI-ревью #241). */
export function automationServiceKind(name: (typeof AUTOMATIONS.reservedJournalKinds)[number]): string {
  return name;
}

export type ConfigParseResult =
  | { ok: true; config: AutomationConfig }
  | { ok: false; error: string };

/** Жёсткая валидация: неизвестные поля и опечатки отклоняются, а не игнорируются
 *  (fail loud — сохранённый «почти правильный» конфиг молча не работал бы). */
export function parseAutomationConfig(raw: unknown): ConfigParseResult {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return { ok: false, error: "конфиг должен быть JSON-объектом" };
  }
  const input = raw as Record<string, unknown>;
  if (JSON.stringify(input).length > LIMITS.automationConfigMaxChars) {
    return { ok: false, error: `конфиг больше ${LIMITS.automationConfigMaxChars} символов` };
  }
  for (const key of ["enabled", "trigger", "task", "report"]) {
    if (!(key in input)) return { ok: false, error: `нет обязательного поля ${key}` };
  }
  for (const key of Object.keys(input)) {
    if (!["enabled", "trigger", "task", "report"].includes(key)) {
      return { ok: false, error: `неизвестное поле ${key}` };
    }
  }
  if (typeof input.enabled !== "boolean") {
    return { ok: false, error: "enabled должен быть boolean" };
  }

  const triggerInput = input.trigger as Record<string, unknown> | null;
  if (triggerInput === null || typeof triggerInput !== "object" || Array.isArray(triggerInput)) {
    return { ok: false, error: "trigger должен быть объектом" };
  }
  let trigger: AutomationTrigger;
  if (triggerInput.type === "schedule") {
    const hours = triggerInput.intervalHours;
    if (typeof hours !== "number" || !Number.isInteger(hours) || hours < 1 || hours > 24 * 366) {
      return { ok: false, error: "trigger.intervalHours: целое 1..8784" };
    }
    if (Object.keys(triggerInput).length !== 2) {
      return { ok: false, error: "trigger: неизвестные поля для type=schedule" };
    }
    trigger = { type: "schedule", intervalHours: hours };
  } else if (triggerInput.type === "webhook") {
    if (Object.keys(triggerInput).length !== 1) {
      return { ok: false, error: "trigger: неизвестные поля для type=webhook" };
    }
    trigger = { type: "webhook" };
  } else if (triggerInput.type === "journal") {
    const kind = triggerInput.kind;
    if (typeof kind !== "string" || !kind || kind.length > 128) {
      return { ok: false, error: "trigger.kind: непустая строка до 128 символов" };
    }
    // Служебные kind'ы системных событий самой автоматизации всегда живут под
    // task_id с префиксом AUTOMATIONS.runTaskPrefix — гвардия петли исключает
    // их из кандидатов #fireJournalTriggers ДО сравнения kind, так что триггер
    // с таким kind никогда не сработает. Отклоняем явно, а не молча сохраняем
    // мёртвый конфиг (находка AI-ревью PR #241).
    if ((AUTOMATIONS.reservedJournalKinds as readonly string[]).includes(kind)) {
      return {
        ok: false,
        error: `trigger.kind: "${kind}" — служебный kind самой автоматизации, `
          + "триггером быть не может (никогда не сработает)",
      };
    }
    if (Object.keys(triggerInput).length !== 2) {
      return { ok: false, error: "trigger: неизвестные поля для type=journal" };
    }
    trigger = { type: "journal", kind };
  } else {
    return { ok: false, error: "trigger.type: schedule | webhook | journal" };
  }

  const taskInput = input.task as Record<string, unknown> | null;
  if (taskInput === null || typeof taskInput !== "object" || Array.isArray(taskInput)) {
    return { ok: false, error: "task должен быть объектом" };
  }
  let task: AutomationTask;
  if (taskInput.kind === "digest") {
    if (Object.keys(taskInput).length !== 1) {
      return { ok: false, error: "task: неизвестные поля для kind=digest" };
    }
    task = { kind: "digest" };
  } else if (taskInput.kind === "hands") {
    const text = taskInput.text;
    if (typeof text !== "string" || !text.trim() || text.length > 8000) {
      return { ok: false, error: "task.text: непустая строка до 8000 символов" };
    }
    if (Object.keys(taskInput).length !== 2) {
      return { ok: false, error: "task: неизвестные поля для kind=hands" };
    }
    task = { kind: "hands", text };
  } else if (taskInput.kind === "pool") {
    const title = taskInput.title;
    const body = taskInput.body;
    if (typeof title !== "string" || !title.trim() || title.length > 200) {
      return { ok: false, error: "task.title: непустая строка до 200 символов" };
    }
    if (body !== undefined && (typeof body !== "string" || body.length > 8000)) {
      return { ok: false, error: "task.body: строка до 8000 символов" };
    }
    if (Object.keys(taskInput).filter((key) => key !== "body").length !== 2) {
      return { ok: false, error: "task: неизвестные поля для kind=pool" };
    }
    task = { kind: "pool", title, body: typeof body === "string" ? body : "" };
  } else {
    return { ok: false, error: "task.kind: digest | hands | pool" };
  }

  const reportInput = input.report as Record<string, unknown> | null;
  if (reportInput === null || typeof reportInput !== "object" || Array.isArray(reportInput)) {
    return { ok: false, error: "report должен быть объектом" };
  }
  if (Object.keys(reportInput).length !== 1 || !Array.isArray(reportInput.channels)) {
    return { ok: false, error: "report: нужно единственное поле channels (массив)" };
  }
  const channels: ReportChannel[] = [];
  for (const rawChannel of reportInput.channels) {
    const channel = rawChannel as Record<string, unknown> | null;
    if (channel === null || typeof channel !== "object" || Array.isArray(channel)) {
      return { ok: false, error: "report.channels: каждый канал — объект" };
    }
    if (channel.type === "slack") {
      const target = channel.target;
      if (typeof target !== "string" || !target || target.length > 128) {
        return { ok: false, error: "report.channels[slack].target: непустая строка до 128 символов (#канал или id)" };
      }
      if (Object.keys(channel).length !== 2) return { ok: false, error: "report.channels[slack]: неизвестные поля" };
      channels.push({ type: "slack", target });
    } else if (channel.type === "telegram") {
      if (Object.keys(channel).length !== 1) return { ok: false, error: "report.channels[telegram]: неизвестные поля" };
      channels.push({ type: "telegram" });
    } else {
      return { ok: false, error: "report.channels[].type: slack | telegram" };
    }
  }
  if (channels.length > 10) return { ok: false, error: "report.channels: не больше 10 каналов" };
  // Дайджест без каналов — собранный текст, уехавший в никуда: отклоняем на входе,
  // а не обнаруживаем на раннере (см. scripts/automations/run.sh).
  if (task.kind === "digest" && channels.length === 0) {
    return { ok: false, error: "task=digest требует хотя бы один канал в report.channels" };
  }

  return { ok: true, config: { enabled: input.enabled, trigger, task, report: { channels } } };
}

export function isValidAutomationId(id: unknown): id is string {
  return typeof id === "string" && new RegExp(AUTOMATION_ID_PATTERN).test(id);
}

/** Решение «пора ли запускать расписание». Пульс DO тикает каждые 15 минут,
 *  поэтому реальная точность — интервал плюс до 15 минут: фаза anchored
 *  первым запуском. null (ещё не запускалась) — пора всегда. */
export function scheduleDue(intervalHours: number, lastFiredTs: number | null, now: number): boolean {
  if (lastFiredTs === null) return true;
  return now - lastFiredTs >= intervalHours * 3_600_000;
}

/** Решение «пора ли journal-триггеру» — кулдаун от прошлого запуска
 *  (AUTOMATIONS.journalCooldownMs). Работа прогона может порождать события
 *  журнала с чужими task_id (kind=pool → job_end воркера под issue-N):
 *  префикс-гвардия их не отличает, кулдаун рвёт цикл, сводя частоту к каденсу
 *  пульса (ревью #116, minor 3). */
export function journalTriggerDue(lastFiredTs: number | null, now: number, cooldownMs: number): boolean {
  if (lastFiredTs === null) return true;
  return now - lastFiredTs >= cooldownMs;
}

/** Период для сборщика дайджеста: от прошлого запуска (или интервала назад при
 *  первом) до сейчас. Раннер получает его в payload диспатча — дайджест собирает
 *  ровно прошедший интервал, а не «последние N дней» от догадок. */
export function digestPeriod(
  lastFiredTs: number | null,
  intervalHours: number,
  now: number,
): { since_ts: number; until_ts: number } {
  return { since_ts: lastFiredTs ?? now - intervalHours * 3_600_000, until_ts: now };
}

/** task_id прогона в очереди/журнале: `automation:<id>:<ts>-<nonce>`
 *  (префикс — гвардия петли journal-триггеров). ts в base36, nonce добирает
 *  уникальность внутри миллисекунды: два почти одновременных webhook-выстрела
 *  иначе столкнулись бы на PRIMARY KEY tasks (ревью #116, minor 6). Повторная
 *  доставка webhook'а — новый прогон: семантика at-least-once. */
export function runTaskId(automationId: string, now: number, nonce: string): string {
  if (!/^[0-9a-f]{4,32}$/.test(nonce)) {
    throw new Error("runTaskId: nonce — hex-строка 4..32 символов");
  }
  return `${AUTOMATIONS.runTaskPrefix}${automationId}:${now.toString(36)}-${nonce}`;
}
