// Разбор тела ответа fetch при !response.ok (issue #575, вторая половина
// диагноза «зелёный health при мёртвой морде»): владелец видел голый «HTTP
// 500» на журнале/плагинах/дедупе заказов, хотя воркер честно отдаёт причину
// в теле — {"error":{"code","message"}} (конвенция ApiError, cf-worker/src/
// harness.ts, storageErrorResponse + classifyStorageError), которую прокси dsh-edge
// (patches/0005-harness-status-proxy.patch) проносит нетронутым. Три клиентских
// точки (integrations, plugin-manager: журнал и rpcCall) отбрасывали тело ДО
// попытки его прочитать — этот файл их единственная общая правда.
//
// ОДНО место правды на все fetch-точки клиентских плагинов: они собираются в
// НЕЗАВИСИМЫЕ npm-тарболы без общего рантайм-модуля (require() чужого пакета
// без декларации dsh.client.inject бросает при материализации в браузере, а
// заводить ради одной функции ещё один публикуемый пакет — избыточно), поэтому
// шарить код можно только на этапе СБОРКИ: build.mjs каждого пакета читает
// текст этого файла и инлайнит его в бандл тем же приёмом, что уже инлайнит
// MANIFEST/CATALOG (см. build.mjs обоих пакетов) — автор источника один,
// скомпилированных копий может быть несколько.
//
// Фолбэк «HTTP N» — когда тело пустое, не JSON или не по форме ApiError:
// код ответа честнее выдумки, но не должен разово ронять секцию (см. тесты
// client.test.mjs — «отказ журнала», «чужой API не JSON»).
async function describeResponseError(response) {
  let body;
  try {
    body = await response.json();
  } catch {
    return "HTTP " + response.status;
  }
  const message = body !== null && typeof body === "object"
    && body.error !== null && typeof body.error === "object"
    && typeof body.error.message === "string"
    ? body.error.message
    : null;
  return message !== null ? message : "HTTP " + response.status;
}
