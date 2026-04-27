/**
 * Typed API client for the AI Policy Radar FastAPI backend.
 *
 * All shapes mirror `backend/radar/api/schemas.py` 1:1. The server-side
 * connection is per-request and we always pass `cache: 'no-store'` so
 * Server Components don't accidentally cache stale dashboard data.
 */

export const API_BASE: string =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_BASE) ||
  "http://localhost:8000";

/**
 * DEMO_MODE: when set, the API client returns pre-computed JSON snapshots
 * shipped under /public/snapshots instead of hitting the FastAPI backend.
 * This lets us deploy the read-only analyst tool to a static host (e.g.
 * GitHub Pages, Vercel) without standing up the backend service.
 *
 * Snapshots cover the four demo profiles (Frontier Lab, State AG,
 * Healthcare AI Startup, Datacenter Investor) plus the entities and
 * activities they reference. Profile creation and live refresh are
 * disabled in this mode — callers should check `DEMO_MODE` and surface
 * a banner.
 */
export const DEMO_MODE: boolean =
  typeof process !== "undefined" &&
  process.env.NEXT_PUBLIC_DEMO_MODE === "1";

/**
 * Snapshot loader for DEMO_MODE. Reads pre-computed JSON files from
 * public/snapshots at build/request time. Uses Node's fs on the server
 * (which is where all our server components run) to avoid having to
 * stand up a fetch loop against ourselves.
 */
async function loadSnapshot<T>(relPath: string): Promise<T> {
  const isServer = typeof window === "undefined";
  if (isServer) {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const fs = await import("node:fs/promises");
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const path = await import("node:path");
    const filePath = path.join(
      process.cwd(),
      "public",
      "snapshots",
      relPath.replace(/^\//, ""),
    );
    try {
      const buf = await fs.readFile(filePath, "utf-8");
      return JSON.parse(buf) as T;
    } catch (e) {
      throw new ApiError(404, `snapshot not found: ${filePath}`);
    }
  }
  // Client-side fallback (rare — server components dominate).
  const url = `/snapshots/${relPath.replace(/^\//, "")}`;
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new ApiError(res.status, `snapshot not found: ${url}`);
  return (await res.json()) as T;
}

// ---------- Types ----------

export type EntitySummary = {
  id: string;
  name: string;
  entity_type: string;
  subcategory: string | null;
  jurisdiction: string | null;
  description: string | null;
  aliases: string[];
};

export type Activity = {
  id: string;
  entity_id: string;
  entity_type: string;
  activity_type: string;
  occurred_at: string;
  ingested_at: string;
  source_url: string;
  source_adapter: string;
  title: string;
  raw_text: string | null;
  payload: Record<string, unknown>;
  url_verified_at: string | null;
};

export type Materiality = {
  scope?: string;
  bindingness?: string;
  novelty?: string;
  confidence?: number;
  [k: string]: unknown;
};

export type Enrichment = {
  activity_id: string;
  summary: string;
  topics: string[];
  mentioned_entities: string[];
  stance: string | null;
  stance_quote: string | null;
  materiality: Materiality;
  enriched_at: string;
  enricher_model: string;
};

export type ActivityWithEnrichment = {
  activity: Activity;
  enrichment: Enrichment | null;
  source_entity: EntitySummary | null;
};

export type TopTopicStat = {
  topic_id: string;
  count: number;
  dominant_stance: string | null;
};

export type EntityStats = {
  activity_count: number;
  recent_activities: ActivityWithEnrichment[];
  top_topics: TopTopicStat[];
};

export type EntityDetail = {
  entity: EntitySummary;
  stats: EntityStats;
};

export type StructuredProfile = {
  topics_weighted: Record<string, number>;
  watch_entities: string[];
  jurisdictions: string[];
  entity_types: string[];
  activity_type_filters: string[] | null;
  recency_days: number;
  risk_tolerance: string;
  notes: string | null;
};

export type ProfileSummary = {
  id: string;
  name: string;
  created_at: string | null;
};

export type Profile = {
  profile_id: string;
  name: string;
  nl_description: string;
  structured: StructuredProfile;
};

export type AwarenessBlock = {
  relevance_score: number;
  reasoning: string;
  recommended_actions: string[];
  citations: string[];
};

export type AwarenessItem = {
  activity: Activity;
  enrichment: Enrichment | null;
  source_entity: EntitySummary | null;
  awareness: AwarenessBlock;
};

export type Dashboard = {
  profile_id: string;
  generated_at: string;
  items: AwarenessItem[];
};

// ---------- Errors ----------

export class ApiError extends Error {
  status: number;
  body: string;
  constructor(status: number, body: string, message?: string) {
    super(message || `API ${status}: ${body.slice(0, 200)}`);
    this.status = status;
    this.body = body;
    this.name = "ApiError";
  }
}

// ---------- Internal helpers ----------

function buildQuery(
  params?: Record<
    string,
    string | number | boolean | string[] | undefined | null
  >,
): string {
  if (!params) return "";
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    if (Array.isArray(v)) {
      for (const item of v) usp.append(k, String(item));
    } else {
      usp.append(k, String(v));
    }
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

async function apiFetch<T>(
  path: string,
  init?: RequestInit & { query?: Parameters<typeof buildQuery>[0] },
): Promise<T> {
  const { query, ...rest } = init || {};
  const url = `${API_BASE}${path}${buildQuery(query)}`;
  const res = await fetch(url, {
    cache: "no-store",
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(rest.headers || {}),
    },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, text);
  }
  return (await res.json()) as T;
}

// ---------- Public API ----------

export async function listEntities(opts?: {
  entity_type?: string[];
  jurisdiction?: string[];
  q?: string;
}): Promise<EntitySummary[]> {
  if (DEMO_MODE) {
    const all = await loadSnapshot<EntitySummary[]>("entities.json");
    let out = all;
    if (opts?.entity_type?.length) {
      const set = new Set(opts.entity_type);
      out = out.filter((e) => set.has(e.entity_type));
    }
    if (opts?.jurisdiction?.length) {
      const set = new Set(opts.jurisdiction);
      out = out.filter((e) => e.jurisdiction && set.has(e.jurisdiction));
    }
    if (opts?.q) {
      const q = opts.q.toLowerCase();
      out = out.filter(
        (e) =>
          e.name.toLowerCase().includes(q) ||
          e.aliases.some((a) => a.toLowerCase().includes(q)),
      );
    }
    return out;
  }
  return apiFetch<EntitySummary[]>("/entities", { query: opts });
}

export async function getEntity(id: string): Promise<EntityDetail> {
  if (DEMO_MODE) {
    return loadSnapshot<EntityDetail>(`entities/${id}.json`);
  }
  return apiFetch<EntityDetail>(`/entities/${encodeURIComponent(id)}`);
}

export async function listActivities(opts?: {
  entity_id?: string[];
  entity_type?: string;
  activity_type?: string;
  topic?: string;
  since?: string;
  limit?: number;
}): Promise<ActivityWithEnrichment[]> {
  if (DEMO_MODE) {
    // Best-effort: not snapshot-indexed; return empty list to avoid
    // showing stale or partial data on activity-list pages.
    return [];
  }
  return apiFetch<ActivityWithEnrichment[]>("/activities", { query: opts });
}

export async function getActivity(
  id: string,
): Promise<ActivityWithEnrichment> {
  if (DEMO_MODE) {
    return loadSnapshot<ActivityWithEnrichment>(`activities/${id}.json`);
  }
  return apiFetch<ActivityWithEnrichment>(
    `/activities/${encodeURIComponent(id)}`,
  );
}

export async function listProfiles(): Promise<ProfileSummary[]> {
  if (DEMO_MODE) {
    return loadSnapshot<ProfileSummary[]>("profiles.json");
  }
  return apiFetch<ProfileSummary[]>("/profiles");
}

export async function getProfile(id: string): Promise<Profile> {
  if (DEMO_MODE) {
    return loadSnapshot<Profile>(`profile_${id}.json`);
  }
  return apiFetch<Profile>(`/profiles/${encodeURIComponent(id)}`);
}

export async function createProfile(body: {
  name: string;
  nl_description: string;
  structured_overrides?: Record<string, unknown>;
}): Promise<Profile> {
  if (DEMO_MODE) {
    throw new ApiError(
      503,
      "Profile creation is disabled in the live demo. Run the backend locally to create custom profiles.",
    );
  }
  return apiFetch<Profile>("/profiles", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function getDashboard(
  profile_id: string,
  opts?: { since?: string; top_k?: number },
): Promise<Dashboard> {
  if (DEMO_MODE) {
    return loadSnapshot<Dashboard>(`${profile_id}.json`);
  }
  return apiFetch<Dashboard>(
    `/dashboard/${encodeURIComponent(profile_id)}`,
    { query: opts },
  );
}

export async function refreshDashboard(
  profile_id: string,
  opts?: { since?: string; top_k?: number },
): Promise<Dashboard> {
  if (DEMO_MODE) {
    // Refresh in demo mode just re-reads the same snapshot.
    return loadSnapshot<Dashboard>(`${profile_id}.json`);
  }
  return apiFetch<Dashboard>(
    `/awareness/refresh/${encodeURIComponent(profile_id)}`,
    { method: "POST", query: opts },
  );
}
