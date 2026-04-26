import Link from "next/link";
import { notFound } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { ApiError, getActivity } from "@/lib/api";
import { formatDate, formatDateTime, humanizeLabel } from "@/lib/format";

export const dynamic = "force-dynamic";

type Params = { id: string };

export default async function ActivityDetailPage({
  params,
}: {
  params: Promise<Params>;
}) {
  const { id } = await params;

  let detail: Awaited<ReturnType<typeof getActivity>>;
  try {
    detail = await getActivity(id);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      notFound();
    }
    return (
      <div className="mx-auto w-full max-w-3xl px-6 py-10 text-sm text-destructive">
        Failed to load activity: {e instanceof Error ? e.message : "unknown"}
      </div>
    );
  }

  const { activity, enrichment, source_entity } = detail;
  const materiality = enrichment?.materiality ?? {};

  return (
    <div className="mx-auto w-full max-w-screen-xl px-6 py-8">
      <div className="mb-3 text-xs text-muted-foreground">
        <Link href="/entities" className="hover:text-foreground">
          ← entities
        </Link>
      </div>

      <header className="mb-6 flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Badge variant="outline">
            {humanizeLabel(activity.activity_type)}
          </Badge>
          <span className="text-muted-foreground">
            {formatDate(activity.occurred_at)}
          </span>
          {source_entity ? (
            <Link
              href={`/entities/${source_entity.id}`}
              className="text-primary hover:underline"
            >
              {source_entity.name}
            </Link>
          ) : null}
          {activity.source_url ? (
            <a
              href={activity.source_url}
              target="_blank"
              rel="noopener noreferrer"
              className="ml-auto text-muted-foreground hover:text-foreground hover:underline"
            >
              view source ↗
            </a>
          ) : null}
        </div>
        <h1 className="font-heading text-xl font-semibold leading-snug tracking-tight">
          {activity.title || "(untitled activity)"}
        </h1>
        <div className="text-[11px] text-muted-foreground">
          ingested {formatDateTime(activity.ingested_at)} ·{" "}
          adapter <span className="font-mono">{activity.source_adapter}</span>
        </div>
      </header>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_360px]">
        <section className="flex flex-col gap-4">
          {enrichment ? (
            <>
              <div>
                <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                  Summary
                </h2>
                <p className="text-sm leading-relaxed">{enrichment.summary}</p>
              </div>

              {enrichment.stance ? (
                <div>
                  <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                    Stance
                  </h2>
                  <div className="flex items-center gap-2">
                    <Badge variant="outline">
                      {humanizeLabel(enrichment.stance)}
                    </Badge>
                  </div>
                  {enrichment.stance_quote ? (
                    <blockquote className="mt-2 border-l-2 border-border pl-3 text-sm italic text-muted-foreground">
                      &ldquo;{enrichment.stance_quote}&rdquo;
                    </blockquote>
                  ) : null}
                </div>
              ) : null}

              {enrichment.topics.length > 0 ? (
                <div>
                  <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                    Topics
                  </h2>
                  <div className="flex flex-wrap gap-1">
                    {enrichment.topics.map((t) => (
                      <Badge key={t} variant="secondary" className="font-normal">
                        {humanizeLabel(t)}
                      </Badge>
                    ))}
                  </div>
                </div>
              ) : null}

              <div>
                <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                  Materiality
                </h2>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  {(["scope", "bindingness", "novelty"] as const).map((k) => (
                    <Card key={k} size="sm">
                      <CardContent className="px-3 py-2">
                        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                          {k}
                        </div>
                        <div className="text-sm font-medium">
                          {humanizeLabel(
                            (materiality[k] as string | undefined) ?? "—",
                          )}
                        </div>
                      </CardContent>
                    </Card>
                  ))}
                  <Card size="sm">
                    <CardContent className="px-3 py-2">
                      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                        confidence
                      </div>
                      <div className="font-mono text-sm tabular-nums">
                        {typeof materiality.confidence === "number"
                          ? materiality.confidence.toFixed(2)
                          : "—"}
                      </div>
                    </CardContent>
                  </Card>
                </div>
              </div>

              {enrichment.mentioned_entities.length > 0 ? (
                <div>
                  <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                    Mentioned entities
                  </h2>
                  <div className="flex flex-wrap gap-1">
                    {enrichment.mentioned_entities.map((m) => (
                      <Link
                        key={m}
                        href={`/entities/${m}`}
                        className="rounded-md border border-border bg-muted/30 px-1.5 py-0.5 text-xs hover:bg-muted"
                      >
                        {m}
                      </Link>
                    ))}
                  </div>
                </div>
              ) : null}

              <Separator />
              <div className="text-[11px] text-muted-foreground">
                enriched {formatDateTime(enrichment.enriched_at)} ·{" "}
                <span className="font-mono">{enrichment.enricher_model}</span>
              </div>
            </>
          ) : (
            <Card>
              <CardContent className="px-4 py-6 text-center text-sm text-muted-foreground">
                This activity has not been enriched yet.
              </CardContent>
            </Card>
          )}

          {activity.raw_text ? (
            <details className="rounded-lg border bg-muted/20 p-3 text-xs">
              <summary className="cursor-pointer font-medium text-muted-foreground">
                Raw source text
              </summary>
              <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-snug text-foreground/90">
                {activity.raw_text}
              </pre>
            </details>
          ) : null}
        </section>

        <aside className="flex flex-col gap-3">
          <h2 className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            Raw payload
          </h2>
          <pre className="max-h-[600px] overflow-auto rounded-lg border bg-muted/30 p-3 font-mono text-[11px] leading-snug">
            {JSON.stringify(activity.payload, null, 2)}
          </pre>
        </aside>
      </div>
    </div>
  );
}
