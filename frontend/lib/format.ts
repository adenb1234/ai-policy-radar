/** Small formatting utilities used across the dashboard pages. */

export { cn } from "@/lib/utils";

const MONTHS_SHORT = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

function safeDate(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d;
}

/** "Apr 12" or empty string if unparseable. */
export function formatDate(iso: string | null | undefined): string {
  const d = safeDate(iso);
  if (!d) return "";
  return `${MONTHS_SHORT[d.getUTCMonth()]} ${d.getUTCDate()}`;
}

/** "Apr 12, 2026 14:32 UTC" */
export function formatDateTime(iso: string | null | undefined): string {
  const d = safeDate(iso);
  if (!d) return "";
  const m = MONTHS_SHORT[d.getUTCMonth()];
  const day = d.getUTCDate();
  const year = d.getUTCFullYear();
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${m} ${day}, ${year} ${hh}:${mm} UTC`;
}

/**
 * Returns an ISO date (YYYY-MM-DD) for `daysAgo` days before today (UTC).
 * Used for the dashboard "since" filter.
 */
export function isoDaysAgo(daysAgo: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - daysAgo);
  return d.toISOString().slice(0, 10);
}

/**
 * Maps a 0–10 relevance score to Tailwind utility classes for a badge.
 *  [0,3] muted, [4,6] blue, [7,8] amber, [9,10] red.
 */
export function relevanceColor(score: number): string {
  const s = Math.max(0, Math.min(10, Math.round(score)));
  if (s <= 3) return "bg-muted text-muted-foreground border border-border";
  if (s <= 6)
    return "bg-blue-100 text-blue-900 border border-blue-300 dark:bg-blue-950/40 dark:text-blue-200 dark:border-blue-900";
  if (s <= 8)
    return "bg-amber-100 text-amber-900 border border-amber-300 dark:bg-amber-950/40 dark:text-amber-200 dark:border-amber-900";
  return "bg-red-100 text-red-900 border border-red-300 dark:bg-red-950/40 dark:text-red-200 dark:border-red-900";
}

/** Truncate a string to `n` chars with an ellipsis. */
export function truncate(s: string, n: number): string {
  if (!s) return "";
  if (s.length <= n) return s;
  return s.slice(0, n - 1).trimEnd() + "…";
}

/** Title-case a snake_case or kebab-case label. */
export function humanizeLabel(s: string | null | undefined): string {
  if (!s) return "";
  return s
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
