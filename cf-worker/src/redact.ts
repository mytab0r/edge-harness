// Маскирование секретов в наружных текстах морды (инбокс → публичный issue —
// первый путь произвольного фриформ-текста наружу). Канонический источник
// паттернов — scripts/lib/dsh-ci.sh::redact: тот же класс паттернов и те же
// фикстуры в тестах; новая форма секрета добавляется в оба места ОДНИМ классом
// правки, гвардия фикстур в test/harness.spec.ts красит расхождение.
const REDACT_PATTERNS: [RegExp, string][] = [
  [/nvapi-[A-Za-z0-9_-]{4,}/g, "nvapi-[REDACTED]"],
  [/(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}/g, "$1sk-[REDACTED]"],
  [/(^|[^A-Za-z0-9_])ghp_[A-Za-z0-9]{20,}/g, "$1ghp_[REDACTED]"],
  [/(^|[^A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}/g, "$1github_pat_[REDACTED]"],
];

/** Заменяет секреты на <префикс>[REDACTED]. Возвращает текст и факт замены. */
export function redact(text: string): { text: string; redacted: boolean } {
  let out = text;
  for (const [pattern, replacement] of REDACT_PATTERNS) {
    out = out.replace(pattern, replacement);
  }
  return { text: out, redacted: out !== text };
}
