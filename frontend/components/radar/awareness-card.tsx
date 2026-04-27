import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import type { AwarenessItem } from "@/lib/api";
import { formatDate, humanizeLabel, relevanceColor } from "@/lib/format";

type Props = { item: AwarenessItem };

export function AwarenessCard({ item }: Props) {
  const { activity, enrichment, source_entity, awareness } = item;
  const score = awareness.relevance_score;
  const stance = enrichment?.stance ?? null;
  const stanceQuote = enrichment?.stance_quote ?? null;
  const topics = enrichment?.topics ?? [];
  const allActions = awareness.recommended_actions ?? [];
  const actions = allActions.slice(0, 3);
  const citations = awareness.citations ?? [];

  return (
    <Card size="sm" className="gap-2 py-3">
      <CardContent className="flex flex-col gap-2 px-4">
        {/* Top row: score, source entity, date, activity_type */}
        <div className="flex items-center gap-2 text-xs">
          <span
            className={
              "inline-flex h-6 min-w-7 items-center justify-center rounded-md px-1.5 font-mono text-[11px] font-semibold tabular-nums " +
              relevanceColor(score)
            }
            title={`Relevance: ${score.toFixed(1)}/10`}
          >
            {score.toFixed(1)}
          </span>
          {source_entity ? (
            <Link
              href={`/entities/${source_entity.id}`}
              className="inline-flex items-center gap-1 rounded-md border border-border bg-muted/40 px-1.5 py-0.5 text-xs font-medium hover:bg-muted"
            >
              <span className="truncate max-w-[14rem]">
                {source_entity.name}
              </span>
              <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                {humanizeLabel(source_entity.entity_type)}
              </span>
            </Link>
          ) : null}
          <span className="text-muted-foreground">
            {formatDate(activity.occurred_at)}
          </span>
          {activity.activity_type ? (
            <span className="ml-auto text-[10px] uppercase tracking-wider text-muted-foreground">
              {humanizeLabel(activity.activity_type)}
            </span>
          ) : null}
        </div>

        {/* Title */}
        <Link
          href={`/activity/${activity.id}`}
          className="text-[15px] font-semibold leading-snug hover:underline"
        >
          {activity.title || "(untitled activity)"}
        </Link>

        {/* Reasoning */}
        {awareness.reasoning ? (
          <p className="text-sm leading-snug text-foreground/90">
            {awareness.reasoning}
          </p>
        ) : null}

        {/* Stance + quote */}
        {stance ? (
          <div className="flex flex-col gap-1 rounded-md border border-border/60 bg-muted/30 p-2">
            <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-muted-foreground">
              <span>Stance</span>
              <Badge variant="outline" className="font-medium">
                {humanizeLabel(stance)}
              </Badge>
            </div>
            {stanceQuote ? (
              <blockquote className="border-l-2 border-border pl-2 text-xs italic text-muted-foreground">
                &ldquo;{stanceQuote}&rdquo;
              </blockquote>
            ) : null}
          </div>
        ) : null}

        {/* Topics */}
        {topics.length > 0 ? (
          <div className="flex flex-wrap items-center gap-1">
            {topics.map((t) => (
              <Badge key={t} variant="secondary" className="font-normal">
                {humanizeLabel(t)}
              </Badge>
            ))}
          </div>
        ) : null}

        {/* Recommended actions */}
        {actions.length > 0 ? (
          <div className="rounded-md border-l-2 border-primary/60 bg-primary/5 px-3 py-2">
            <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-primary">
              <span aria-hidden>→</span>
              <span>Recommended actions</span>
              <span className="font-mono text-[10px] font-normal opacity-70">
                ({allActions.length})
              </span>
            </div>
            <ul className="ml-4 list-disc space-y-1 text-sm">
              {actions.map((a, i) => (
                <li key={i} className="leading-snug">
                  {a}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {/* Citations */}
        {citations.length > 0 ? (
          <div className="flex flex-wrap items-center gap-1 text-[11px] text-muted-foreground">
            <span className="font-medium">drawn from:</span>
            {citations.map((c, i) => (
              <span
                key={i}
                className="rounded border border-border bg-muted/30 px-1.5 py-0.5 font-mono"
              >
                {c}
              </span>
            ))}
          </div>
        ) : null}

        <Separator className="my-1" />

        {/* Footer links */}
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          {activity.source_url ? (
            <a
              href={activity.source_url}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-foreground hover:underline"
            >
              view source ↗
            </a>
          ) : null}
          <Link
            href={`/activity/${activity.id}`}
            className="hover:text-foreground hover:underline"
          >
            view full activity →
          </Link>
        </div>
      </CardContent>
    </Card>
  );
}
