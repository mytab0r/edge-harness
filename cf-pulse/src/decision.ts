// Чистое решение самообновления морды (#73): проводка (fetch/storage/dispatch)
// остаётся тонкой в Pulse#alarm, а все ветки логики крутятся тестами.
// Порт из cf-worker/src/harness.ts (задача #86) — поведение не менялось.

export type UpdateDecision = "dispatch" | "throttled" | "quiet";

import { DSH_EDGE_UPDATE } from "./config.ts";

export function dshEdgeUpdateDecision(
  deployed: string,
  latest: string,
  lastAttemptTs: number | undefined,
  now: number,
): UpdateDecision {
  if (lastAttemptTs !== undefined && now - lastAttemptTs < DSH_EDGE_UPDATE.throttleMs) {
    return "throttled";
  }
  return deployed === latest ? "quiet" : "dispatch";
}
