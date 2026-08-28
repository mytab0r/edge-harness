import { exports } from "cloudflare:workers";
import { API_SPEC } from "../src/api-spec";
import { describe, expect, it } from "vitest";

// Канарейка контракта: каждый маршрут, заявленный в api-spec.json, обязан быть
// подключён на сервере — отвечать чем угодно, кроме «не найдено»; и наоборот,
// незаявленный путь обязан получить not_found. Спека не может оторваться от кода:
// роутинг сервера построен на этой же таблице, но проверка стоит отдельно —
// на случай, если кто-то обойдёт таблицу.

const AUTH = { Authorization: "Bearer test-token" };

async function call(method: string, path: string, init: RequestInit = {}): Promise<Response> {
  return exports.default.fetch(`https://example.com${path}`, { method, headers: { ...AUTH, ...init.headers }, ...init });
}

describe("канарейка контракта API", () => {
  it("каждый маршрут спеки подключён: без токена — ровно 401, с токеном — не «нет маршрута»", async () => {
    for (const route of API_SPEC) {
      for (const method of route.methods) {
        const unauth = await exports.default.fetch(`https://example.com${route.path}`, { method });
        expect(unauth.status, `без токена ${method} ${route.path}`).toBe(401);

        // Инвариант «маршрут подключён»: любой легитимный ответ (200/400/404 task_not_found…),
        // кроме кода not_found — того, которым сервер отвечает незаявленным путям.
        const res = await call(method, route.path);
        const body = await res.json<{ error?: { code?: string } }>().catch(() => null);
        expect(body, `${method} ${route.path}: ответ — не JSON-конверт API`).toBeTruthy();
        expect(body?.error?.code, `${method} ${route.path}`).not.toBe("not_found");
      }
    }
  });

  it("events.live без Upgrade — внятный 400, а не подвисание", async () => {
    const res = await call("GET", "/api/events.live");
    expect(res.status).toBe(400);
    const body = await res.json<{ error: { code: string } }>();
    expect(body.error.code).toBe("need_websocket_upgrade");
  });

  it("незаявленный путь и незаявленный метод — not_found", async () => {
    const unknownPath = await call("GET", "/api/api/status");
    expect(unknownPath.status).toBe(404);
    const unknownBody = await unknownPath.json<{ error: { code: string } }>();
    expect(unknownBody.error.code).toBe("not_found");

    const unknownMethod = await call("DELETE", "/api/status");
    expect(unknownMethod.status).toBe(404);
  });

  it("спека самосогласованна: имена уникальны, пути начинаются с префикса", () => {
    const names = API_SPEC.map((route) => route.name);
    expect(new Set(names).size).toBe(names.length);
    for (const route of API_SPEC) {
      expect(route.path.startsWith("/api/")).toBe(true);
      expect(route.methods.length).toBeGreaterThan(0);
      expect(route.summary.length).toBeGreaterThan(0);
    }
  });
});
