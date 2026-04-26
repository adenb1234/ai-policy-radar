import Link from "next/link";
import { notFound } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { ApiError, getEntity } from "@/lib/api";
import { formatDate, humanizeLabel } from "@/lib/format";

export const dynamic = "force-dynamic";

type Params = { id: string };

export default async function EntityDetailPage({
  params,
}: {
  params: Promise<Params>;
}) {
  const { id } = await params;

  let detail: Awaited<ReturnType<typeof getEntity>>;
  try {
    detail = await getEntity(id);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      notFound();
    }
    return (
      <div className="mx-auto w-full max-w-3xl px-6 py-10 text-sm text-destructive">
        Failed to load entity: {e instanceof Error ? e.message : "unknown"}
      </div>
    );
  }

  const { entity, stats } = detail;

  return (
    <div className="mx-auto w-full max-w-screen-xl px-6 py-8">
      <div className="mb-3 text-xs text-muted-foreground">
        <Link href="/entities" className="hover:text-foreground">
          ← entity directory
        </Link>
      </div>

      <header className="mb-6 flex flex-col gap-2">
        <h1 className="font-heading text-2xl font-semibold tracking-tight">
          {entity.name}
        </h1>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Badge variant="outline">{humanizeLabel(entity.entity_type)}</Badge>
          {entity.subcategory ? (
            <Badge variant="secondary">
              {humanizeLabel(entity.subcategory)}
            </Badge>
          ) : null}
          {entity.jurisdiction ? (
            <Badge variant="outline">{entity.jurisdiction}</Badge>
          ) : null}
          <span className="text-muted-foreground">
            {stats.activity_count} activit
            {stats.activity_count === 1 ? "y" : "ies"} on record
          </span>
        </div>
        {entity.aliases.length > 0 ? (
          <div className="text-xs text-muted-foreground">
            also known as: {entity.aliases.join(", ")}
          </div>
        ) : null}
        {entity.description ? (
          <p className="max-w-3xl text-sm text-foreground/90">
            {entity.description}
          </p>
        ) : null}
      </header>

      <Tabs defaultValue="recent">
        <TabsList>
          <TabsTrigger value="recent">Recent activities</TabsTrigger>
          <TabsTrigger value="stats">Stats</TabsTrigger>
        </TabsList>

        <TabsContent value="recent" className="pt-3">
          {stats.recent_activities.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No activities ingested for this entity yet.
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              {stats.recent_activities.map((a) => {
                const act = a.activity;
                return (
                  <Link
                    key={act.id}
                    href={`/activity/${act.id}`}
                    className="group block"
                  >
                    <Card size="sm" className="py-3 transition-colors group-hover:bg-muted/40">
                      <CardContent className="flex flex-col gap-1 px-4">
                        <div className="flex items-center gap-2 text-xs text-muted-foreground">
                          <span>{formatDate(act.occurred_at)}</span>
                          <Badge variant="outline" className="font-normal">
                            {humanizeLabel(act.activity_type)}
                          </Badge>
                          {a.enrichment?.stance ? (
                            <Badge variant="secondary" className="font-normal">
                              {humanizeLabel(a.enrichment.stance)}
                            </Badge>
                          ) : null}
                        </div>
                        <div className="text-sm font-medium group-hover:underline">
                          {act.title || "(untitled)"}
                        </div>
                        {a.enrichment?.summary ? (
                          <p className="line-clamp-2 text-xs text-muted-foreground">
                            {a.enrichment.summary}
                          </p>
                        ) : null}
                      </CardContent>
                    </Card>
                  </Link>
                );
              })}
            </div>
          )}
        </TabsContent>

        <TabsContent value="stats" className="pt-3">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Card size="sm">
              <CardContent className="px-4">
                <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                  Top topics
                </h3>
                {stats.top_topics.length === 0 ? (
                  <p className="text-xs text-muted-foreground">
                    No enriched activities to derive topics from yet.
                  </p>
                ) : (
                  <ul className="flex flex-col gap-1.5 text-sm">
                    {stats.top_topics.map((t) => (
                      <li
                        key={t.topic_id}
                        className="flex items-center justify-between gap-2"
                      >
                        <span>{humanizeLabel(t.topic_id)}</span>
                        <span className="flex items-center gap-2 text-xs text-muted-foreground">
                          {t.dominant_stance ? (
                            <Badge variant="outline" className="font-normal">
                              {humanizeLabel(t.dominant_stance)}
                            </Badge>
                          ) : null}
                          <span className="font-mono tabular-nums">
                            ×{t.count}
                          </span>
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>

            <Card size="sm">
              <CardContent className="px-4">
                <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                  Activity volume
                </h3>
                <p className="font-mono text-2xl tabular-nums">
                  {stats.activity_count}
                </p>
                <p className="text-xs text-muted-foreground">
                  total activities ingested
                </p>
              </CardContent>
            </Card>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
