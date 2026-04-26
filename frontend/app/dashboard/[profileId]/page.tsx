import Link from "next/link";
import { notFound } from "next/navigation";
import { DashboardView } from "@/components/radar/dashboard-view";
import { ProfileSummary } from "@/components/radar/profile-summary";
import { ApiError, getDashboard, getProfile } from "@/lib/api";
import { isoDaysAgo } from "@/lib/format";

export const dynamic = "force-dynamic";

type Params = { profileId: string };

export default async function DashboardPage({
  params,
}: {
  params: Promise<Params>;
}) {
  const { profileId } = await params;

  const initialDays = 30;
  const since = isoDaysAgo(initialDays);

  // Fetch profile + dashboard in parallel.
  const [profileRes, dashRes] = await Promise.allSettled([
    getProfile(profileId),
    getDashboard(profileId, { since, top_k: 15 }),
  ]);

  if (profileRes.status === "rejected") {
    const e = profileRes.reason;
    if (e instanceof ApiError && e.status === 404) {
      notFound();
    }
    return (
      <div className="mx-auto w-full max-w-screen-2xl px-6 py-10">
        <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive">
          Failed to load profile:{" "}
          {e instanceof Error ? e.message : "unknown error"}
        </div>
        <div className="mt-4 text-xs">
          <Link href="/" className="text-primary hover:underline">
            ← back to profiles
          </Link>
        </div>
      </div>
    );
  }

  const profile = profileRes.value;
  const items = dashRes.status === "fulfilled" ? dashRes.value.items : [];
  const generatedAt =
    dashRes.status === "fulfilled" ? dashRes.value.generated_at : "";
  const dashError =
    dashRes.status === "rejected"
      ? dashRes.reason instanceof Error
        ? dashRes.reason.message
        : "failed to load dashboard"
      : null;

  return (
    <div>
      <ProfileSummary
        name={profile.name}
        description={profile.nl_description}
        topics={Object.keys(profile.structured.topics_weighted ?? {})}
        jurisdictions={profile.structured.jurisdictions ?? []}
        recencyDays={profile.structured.recency_days}
        riskTolerance={profile.structured.risk_tolerance}
      />

      <div className="mx-auto w-full max-w-screen-2xl px-6 py-6">
        <div className="mb-4 flex items-center gap-3 text-xs text-muted-foreground">
          <Link href="/" className="hover:text-foreground">
            ← profiles
          </Link>
          <span>·</span>
          <span>profile id: {profile.profile_id.slice(0, 8)}…</span>
        </div>

        {dashError ? (
          <div className="mb-4 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-xs text-destructive">
            Awareness engine error: {dashError}
          </div>
        ) : null}

        <DashboardView
          items={items}
          generatedAt={generatedAt}
          initialDays={initialDays}
        />
      </div>
    </div>
  );
}
