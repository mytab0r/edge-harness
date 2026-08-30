import { describe, expect, it } from "vitest";
import { dshEdgeUpdateDecision } from "../src/harness";

// Чистое решение самообновления морды (#73): все ветки логики без сети.
// Проводка (fetch/storage/dispatch) тонкая и зеркалит уже доказанный
// dispatch orchestra; живая петля закрывается первым же npm-релизом.
const NOW = 1_800_000_000_000;
const THROTTLE = 4 * 60 * 60 * 1000;

describe("самообновление морды: решение", () => {
  it("расхождение версий без истории попыток — dispatch", () => {
    expect(dshEdgeUpdateDecision("0.7.0", "0.7.1", undefined, NOW)).toBe("dispatch");
  });
  it("равенство версий — тихо, даже если попытка была давно", () => {
    expect(dshEdgeUpdateDecision("0.7.1", "0.7.1", NOW - THROTTLE - 1, NOW)).toBe("quiet");
  });
  it("расхождение на свежей попытке — throttled (штурма нет)", () => {
    expect(dshEdgeUpdateDecision("0.7.0", "0.7.1", NOW - 1000, NOW)).toBe("throttled");
  });
  it("граница троттла: ровно throttleMs назад — снова dispatch", () => {
    expect(dshEdgeUpdateDecision("0.7.0", "0.7.1", NOW - THROTTLE, NOW)).toBe("dispatch");
  });
  it("даунгрейд тоже чинится: расхождение в любую сторону — dispatch", () => {
    expect(dshEdgeUpdateDecision("0.7.1", "0.7.0", undefined, NOW)).toBe("dispatch");
  });
});
