import { LOCALE } from "./config";

// Сообщения API. Ключ = стабильный код ошибки (его видят клиенты и тесты),
// текст — локализованный. Новая локаль = новый ключ верхнего уровня в MESSAGES.

const MESSAGES = {
  ru: {
    unauthorized: "Неверный или отсутствующий HANDS_TOKEN",
    not_found: "Нет маршрута {method} {path}",
    bad_json: "Некорректный JSON: {detail}",
    body_not_object: "тело должно быть JSON-объектом",
    too_large: "Тело больше {limit} байт",
    batch_too_many: "Батч больше MAX_BATCH_EVENTS={limit}",
    need_task_id: "Нужно поле task_id",
    need_events_array: "Нужно непустое поле events — массив",
    need_positive_int_seq: "Каждое событие требует целого seq > 0",
    need_nonempty_kind: "Каждое событие требует непустой kind",
    need_job_id: "Нужно поле job_id",
    bad_int_param: "Параметр {name} должен быть целым >= 0",
    task_not_found: "Задача {task_id} не найдена",
    payload_too_large: "payload больше {limit} символов",
    dispatch_not_configured: "GH_DISPATCH_TOKEN или GH_REPO не заданы; задача лежит в очереди",
    dispatch_network_failed: "GitHub API недоступен: {detail}",
    dispatch_rejected: "GitHub ответил {status} на dispatch (ожидался 204)",
    internal: "{detail}",
  },
} as const;

export type MessageCode = keyof (typeof MESSAGES)[typeof LOCALE];

type Params = Record<string, string | number>;

export function msg(code: MessageCode, params: Params = {}): string {
  const template: string = MESSAGES[LOCALE][code];
  return template.replace(/\{(\w+)\}/g, (_, key: string) =>
    key in params ? String(params[key]) : `{${key}}`,
  );
}
