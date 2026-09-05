import { LOCALE } from "./config";

// Сообщения API. Ключ = стабильный код ошибки (его видят клиенты и тесты),
// текст — локализованный. Новая локаль = новый ключ верхнего уровня в MESSAGES.

const MESSAGES = {
  ru: {
    unauthorized: "Нужна авторизация: сессионная кука истекла или отсутствует, либо заголовок Authorization: Bearer <HANDS_TOKEN>",
    query_token_removed: "Токен в query (?token=) больше не принимается: браузер входит через POST /api/session (обмен на куку), job — через заголовок Authorization: Bearer",
    session_secret_missing: "SESSION_SECRET не задан: обмен токена на сессионную куку невозможен",
    need_websocket_upgrade: "Этот маршрут — WebSocket: нужен заголовок Upgrade: websocket",
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
    // #320: тот же 500, что и internal, но с именем причины — квота DO SQLite
    // (rows_read/rows_written) на бесплатном тарифе, а не безымянная поломка.
    storage_quota_exceeded: "Хранилище DO вернуло похожую на квоту ошибку: {detail}",
    need_text: "Нужно поле text",
    message_not_found: "Сообщение {message_id} не найдено",
    message_too_large: "Сообщение больше {limit} символов",
    need_source_msg_id: "Нужен идентификатор сообщения: source_msg_id, либо update_id (Telegram update), либо message.message_id — без него идемпотентность невозможна",
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
