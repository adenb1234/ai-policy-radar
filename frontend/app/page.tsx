import Link from "next/link";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { DEMO_MODE, listProfiles } from "@/lib/api";
import { formatDateTime } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function HomePage() {
  let profiles: Awaited<ReturnType<typeof listProfiles>> = [];
  let error: string | null = null;
  try {
    profiles = await listProfiles();
  } catch (e) {
    error = e instanceof Error ? e.message : "failed to load profiles";
  }

  return (
    <div className="mx-auto w-full max-w-screen-2xl px-6 py-10">
      <header className="mb-8">
        <h1 className="font-heading text-2xl font-semibold tracking-tight">
          AI Policy Radar
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Personalized awareness across the AI-policy ecosystem — built for
          analysts, on demand.
        </p>
      </header>

      {error ? (
        <Card className="border-destructive/40">
          <CardHeader>
            <CardTitle className="text-destructive">
              Could not reach the backend
            </CardTitle>
            <CardDescription>{error}</CardDescription>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-muted-foreground">
              Make sure the API is running at{" "}
              <code className="rounded bg-muted px-1 py-0.5">
                http://localhost:8000
              </code>{" "}
              (try{" "}
              <code className="rounded bg-muted px-1 py-0.5">
                make dev-backend
              </code>
              ).
            </p>
          </CardContent>
        </Card>
      ) : profiles.length === 0 ? (
        <Card className="border-dashed">
          <CardHeader>
            <CardTitle>No profiles yet</CardTitle>
            <CardDescription>
              Profiles describe what an analyst cares about. The dashboard is
              built around one.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Link
              href="/profile/new"
              className="inline-flex h-9 items-center rounded-lg bg-primary px-4 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
            >
              Create your first profile →
            </Link>
          </CardContent>
        </Card>
      ) : (
        <div
          className={
            DEMO_MODE
              ? "grid grid-cols-1 gap-6"
              : "grid grid-cols-1 gap-6 lg:grid-cols-[1fr_320px]"
          }
        >
          <section>
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Existing profiles ({profiles.length})
            </h2>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {profiles.map((p) => (
                <Link
                  key={p.id}
                  href={`/dashboard/${p.id}`}
                  className="group block"
                >
                  <Card className="transition-shadow group-hover:shadow-md group-hover:ring-foreground/20">
                    <CardHeader>
                      <CardTitle className="truncate">{p.name}</CardTitle>
                      <CardDescription className="text-xs">
                        Created {formatDateTime(p.created_at)}
                      </CardDescription>
                    </CardHeader>
                    <CardContent className="text-xs text-muted-foreground">
                      <span className="text-primary group-hover:underline">
                        Open dashboard →
                      </span>
                    </CardContent>
                  </Card>
                </Link>
              ))}
            </div>
          </section>

          {DEMO_MODE ? null : (
          <aside>
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              New profile
            </h2>
            <Link href="/profile/new" className="block group">
              <Card className="border-primary/40 bg-primary/5 transition-colors group-hover:bg-primary/10">
                <CardHeader>
                  <CardTitle>Create a profile</CardTitle>
                  <CardDescription>
                    Describe an organization and what they monitor. The radar
                    builds the dashboard around them.
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <span className="text-sm font-medium text-primary group-hover:underline">
                    Start →
                  </span>
                </CardContent>
              </Card>
            </Link>
          </aside>
          )}
        </div>
      )}

      <footer className="mt-10 border-t pt-4 text-xs text-muted-foreground">
        <Link href="/entities" className="hover:text-foreground">
          Entity directory →
        </Link>
      </footer>
    </div>
  );
}
