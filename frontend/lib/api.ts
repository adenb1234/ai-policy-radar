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
  return apiFetch<EntitySummary[]>("/entities", { query: opts });
}

export async function getEntity(id: string): Promise<EntityDetail> {
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
  return apiFetch<ActivityWithEnrichment[]>("/activities", { query: opts });
}

export async function getActivity(
  id: string,
): Promise<ActivityWithEnrichment> {
  return apiFetch<ActivityWithEnrichment>(
    `/activities/${encodeURIComponent(id)}`,
  );
}

export async function listProfiles(): Promise<ProfileSummary[]> {
  return apiFetch<ProfileSummary[]>("/profiles");
}

export async function getProfile(id: string): Promise<Profile> {
  return apiFetch<Profile>(`/profiles/${encodeURIComponent(id)}`);
}

export async function createProfile(body: {
  name: string;
  nl_description: string;
  structured_overrides?: Record<string, unknown>;
}): Promise<Profile> {
  return apiFetch<Profile>("/profiles", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function getDashboard(
  profile_id: string,
  opts?: { since?: string; top_k?: number },
): Promise<Dashboard> {
  return apiFetch<Dashboard>(
    `/dashboard/${encodeURIComponent(profile_id)}`,
    { query: opts },
  );
}

export async function refreshDashboard(
  profile_id: string,
  opts?: { since?: string; top_k?: number },
): Promise<Dashboard> {
  return apiFetch<Dashboard>(
    `/awareness/refresh/${encodeURIComponent(profile_id)}`,
    { method: "POST", query: opts },
  );
}
