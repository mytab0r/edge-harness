// Пульс-воркер конвейера (#86): минимальный инфра-воркер, принявший на себя
// alarm-пульс списываемого воркера edge-harness. Две функции, обе унаследованы
// как есть:
//   1. пульс оркестрации — workflow_dispatch orchestra.yml каждые 15 минут
//      (GitHub cron на репо не тикает/деградирует — замеры #73 и #269);
//   2. самообновление морды (#73) — сверка /api/health морды с npm latest,
//      расхождение при истёкшем троттле → workflow_dispatch deploy-dsh-edge.yml.
// Механизм — alarm-цепочка DO: конструктор закладывает первый тик (его будит
// канарейка деплоя), alarm() ПЕРЕЗАКЛАДЫВАЕТ следующий тик до любой работы —
// падение сети не может оборвать цепочку. Состояние троттла — storage того же
// DO, ни KV, ни cron-конфига не нужно.
import { DurableObject } from "cloudflare:workers";
import { DSH_EDGE_UPDATE, GITHUB, OWNER_OBJECT_NAME, PULSE } from "./config.ts";
import { dshEdgeUpdateDecision } from "./decision.ts";

export class Pulse extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    // Alarm переживает гибернацию; после деплоя объект будит канарейка деплоя
    // (GET /api/health прокидывается внутрь DO — иначе конструктор не стартует).
    void this.ctx.storage
      .getAlarm()
      .then((scheduled) => {
        if (scheduled === null) {
          return this.ctx.storage.setAlarm(Date.now() + PULSE.firstMs);
        }
      })
      .catch(() => {});
  }

  /** Health для канарейки деплоя: доказательство не «200», а ВЗВЕДЁННЫЙ будильник. */
  override async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname !== "/api/health") {
      return new Response(JSON.stringify({ ok: false, error: "not_found" }), {
        status: 404,
        headers: { "content-type": "application/json" },
      });
    }
    const nextAlarmAt = await this.ctx.storage.getAlarm();
    return new Response(
      JSON.stringify({
        ok: true,
        service: "harness-pulse",
        interval_ms: PULSE.intervalMs,
        // null до первого тика не ошибка: конструктор закладывает его на старте,
        // ответ health и есть тот самый первый запрос.
        next_alarm_at: nextAlarmAt,
      }),
      { headers: { "content-type": "application/json" } },
    );
  }

  /**
   * Тик пульса: следующим тиком гарантируем цепочку, потом дёргаем оркестратора
   * и проверяем, не отстала ли морда dsh-edge от npm.
   */
  override async alarm(): Promise<void> {
    await this.ctx.storage.setAlarm(Date.now() + PULSE.intervalMs);
    const token = this.env.GH_DISPATCH_TOKEN;
    const repo = this.env.GH_REPO;
    if (!token || !repo) return; // «возможности нет» — не поломка, тихо ждём конфигурации
    try {
      await dispatchWorkflow(token, repo, GITHUB.orchestraWorkflow);
    } catch (error) {
      // Пульс не роняет объект: ошибка уйдёт в observability, цепочка уже перезаложена.
      console.log(`orchestra dispatch failed: ${error instanceof Error ? error.message : error}`);
    }
    try {
      await this.#checkDshEdgeUpdate(token, repo);
    } catch (error) {
      console.log(`dsh-edge update check failed: ${error instanceof Error ? error.message : error}`);
    }
  }

  /** #73: морда dsh-edge должна быть последней версии. Любой сбой здесь не роняет
   *  пульс: тик уже перезаложен, ошибка уходит в observability. */
  async #checkDshEdgeUpdate(token: string, repo: string): Promise<void> {
    const now = Date.now();
    const last = await this.ctx.storage.get<number>(DSH_EDGE_UPDATE.lastAttemptKey);
    const [healthRes, registryRes] = await Promise.all([
      fetch(DSH_EDGE_UPDATE.healthUrl),
      fetch(DSH_EDGE_UPDATE.registryUrl),
    ]);
    if (!healthRes.ok || !registryRes.ok) {
      throw new Error(`health=${healthRes.status} registry=${registryRes.status}`);
    }
    const health = (await healthRes.json<Record<string, unknown>>()) as { version?: unknown };
    const release = (await registryRes.json<Record<string, unknown>>()) as { version?: unknown };
    if (typeof health.version !== "string" || typeof release.version !== "string") {
      throw new Error("неожиданный формат версий: health/registry не отдали строку version");
    }
    const decision = dshEdgeUpdateDecision(health.version, release.version, last, now);
    if (decision !== "dispatch") return;
    // Пометку попытки ставим ДО диспетча: упавший деплой не должен превратить
    // пульс в штурм упавшего деплоя каждые 15 минут.
    await this.ctx.storage.put(DSH_EDGE_UPDATE.lastAttemptKey, now);
    console.log(`dsh-edge update: deployed ${health.version} != npm ${release.version} — dispatch`);
    await dispatchWorkflow(token, repo, DSH_EDGE_UPDATE.workflow);
  }
}

async function dispatchWorkflow(token: string, repo: string, workflow: string): Promise<void> {
  const res = await fetch(`${GITHUB.apiBase}/repos/${repo}/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "User-Agent": GITHUB.userAgent,
      "X-GitHub-Api-Version": GITHUB.apiVersion,
    },
    body: JSON.stringify({ ref: "main" }),
  });
  if (!res.ok) throw new Error(`dispatch ${workflow} отклонён: ${res.status}`);
}

export default {
  /** Всё прокидывается в единственный DO: сам воркер состояния не держит. */
  async fetch(request, env): Promise<Response> {
    const id = env.PULSE.idFromName(OWNER_OBJECT_NAME);
    return env.PULSE.get(id).fetch(request);
  },
} satisfies ExportedHandler<Env>;
