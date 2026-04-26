"use client";

import { useMemo, useState } from "react";
import { AwarenessCard } from "@/components/radar/awareness-card";
import { Card, CardContent } from "@/components/ui/card";
import type { AwarenessItem } from "@/lib/api";
import { cn, humanizeLabel } from "@/lib/format";

type Props = {
  items: AwarenessItem[];
  generatedAt: string;
  initialDays: number;
};

const DATE_PRESETS: { label: string; days: number }[] = [
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
  { label: "90d", days: 90 },
  { label: "All", days: 9999 },
];

function uniqSorted(values: string[]): string[] {
  return Array.from(new Set(values.filter(Boolean))).sort((a, b) =>
    a.localeCompare(b),
  );
}

function ChipFilter({
  label,
  values,
  selected,
  onToggle,
}: {
  label: string;
  values: string[];
  selected: Set<string>;
  onToggle: (v: string) => void;
}) {
  if (values.length === 0) return null;
  return (
    <div className="flex flex-col gap-1.5">
      <div className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="flex flex-wrap gap-1">
        {values.map((v) => {
          const active = selected.has(v);
          return (
            <button
              key={v}
              type="button"
              onClick={() => onToggle(v)}
              className={cn(
                "rounded-md border px-1.5 py-0.5 text-[11px] transition-colors",
                active
                  ? "border-primary bg-primary text-primary-foreground"
                  : "border-border bg-muted/30 text-foreground hover:bg-muted",
              )}
            >
              {humanizeLabel(v)}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function DashboardView({ items, generatedAt, initialDays }: Props) {
  const [days, setDays] = useState<number>(initialDays);
  const [topicSel, setTopicSel] = useState<Set<string>>(new Set());
  const [entityTypeSel, setEntityTypeSel] = useState<Set<string>>(new Set());
  const [activityTypeSel, setActivityTypeSel] = useState<Set<string>>(
    new Set(),
  );
  const [jurisdictionSel, setJurisdictionSel] = useState<Set<string>>(
    new Set(),
  );

  const facets = useMemo(() => {
    const topics: string[] = [];
    const entityTypes: string[] = [];
    const activityTypes: string[] = [];
    const jurisdictions: string[] = [];
    for (const it of items) {
      if (it.enrichment?.topics) {
        for (const t of it.enrichment.topics) topics.push(t);
      }
      if (it.activity.entity_type) entityTypes.push(it.activity.entity_type);
      if (it.activity.activity_type)
        activityTypes.push(it.activity.activity_type);
      const jur = it.source_entity?.jurisdiction;
      if (jur) jurisdictions.push(jur);
    }
    return {
      topics: uniqSorted(topics),
      entityTypes: uniqSorted(entityTypes),
      activityTypes: uniqSorted(activityTypes),
      jurisdictions: uniqSorted(jurisdictions),
    };
  }, [items]);

  const filtered = useMemo(() => {
    const cutoff =
      days >= 9999 ? null : Date.now() - days * 24 * 60 * 60 * 1000;
    return items.filter((it) => {
      if (cutoff !== null) {
        const d = it.activity.occurred_at
          ? new Date(it.activity.occurred_at).getTime()
          : NaN;
        if (Number.isFinite(d) && d < cutoff) return false;
      }
      if (topicSel.size > 0) {
        const ts = it.enrichment?.topics ?? [];
        if (!ts.some((t) => topicSel.has(t))) return false;
      }
      if (entityTypeSel.size > 0) {
        if (!entityTypeSel.has(it.activity.entity_type)) return false;
      }
      if (activityTypeSel.size > 0) {
        if (!activityTypeSel.has(it.activity.activity_type)) return false;
      }
      if (jurisdictionSel.size > 0) {
        const j = it.source_entity?.jurisdiction;
        if (!j || !jurisdictionSel.has(j)) return false;
      }
      return true;
    });
  }, [items, days, topicSel, entityTypeSel, activityTypeSel, jurisdictionSel]);

  const toggle = (
    setter: (s: Set<string>) => void,
    current: Set<string>,
    v: string,
  ) => {
    const next = new Set(current);
    if (next.has(v)) next.delete(v);
    else next.add(v);
    setter(next);
  };

  const totalActiveFilters =
    topicSel.size +
    entityTypeSel.size +
    activityTypeSel.size +
    jurisdictionSel.size;

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[260px_1fr]">
      <aside className="lg:sticky lg:top-16 lg:self-start">
        <div className="flex flex-col gap-4 rounded-xl border bg-card p-3 text-card-foreground">
          <div className="flex flex-col gap-1.5">
            <div className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              Window
            </div>
            <div className="flex gap-1">
              {DATE_PRESETS.map((p) => (
                <button
                  key={p.label}
                  type="button"
                  onClick={() => setDays(p.days)}
                  className={cn(
                    "flex-1 rounded-md border px-1.5 py-0.5 text-[11px] transition-colors",
                    days === p.days
                      ? "border-primary bg-primary text-primary-foreground"
                      : "border-border bg-muted/30 hover:bg-muted",
                  )}
                >
                  {p.label}
                </button>
              ))}
            </div>
            <span className="text-[10px] text-muted-foreground">
              Filters scoped to items already loaded ({items.length}).
            </span>
          </div>

          <ChipFilter
            label="Topics"
            values={facets.topics}
            selected={topicSel}
            onToggle={(v) => toggle(setTopicSel, topicSel, v)}
          />
          <ChipFilter
            label="Entity types"
            values={facets.entityTypes}
            selected={entityTypeSel}
            onToggle={(v) => toggle(setEntityTypeSel, entityTypeSel, v)}
          />
          <ChipFilter
            label="Activity types"
            values={facets.activityTypes}
            selected={activityTypeSel}
            onToggle={(v) => toggle(setActivityTypeSel, activityTypeSel, v)}
          />
          <ChipFilter
            label="Jurisdictions"
            values={facets.jurisdictions}
            selected={jurisdictionSel}
            onToggle={(v) => toggle(setJurisdictionSel, jurisdictionSel, v)}
          />

          {totalActiveFilters > 0 ? (
            <button
              type="button"
              onClick={() => {
                setTopicSel(new Set());
                setEntityTypeSel(new Set());
                setActivityTypeSel(new Set());
                setJurisdictionSel(new Set());
              }}
              className="text-[11px] text-muted-foreground hover:text-foreground hover:underline"
            >
              Clear {totalActiveFilters} filter
              {totalActiveFilters === 1 ? "" : "s"}
            </button>
          ) : null}
        </div>
      </aside>

      <section className="flex flex-col gap-3">
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>
            Showing {filtered.length} of {items.length} items
          </span>
          <span>
            generated{" "}
            {generatedAt
              ? new Date(generatedAt).toLocaleString(undefined, {
                  hour: "2-digit",
                  minute: "2-digit",
                  month: "short",
                  day: "numeric",
                })
              : ""}
          </span>
        </div>

        {items.length === 0 ? (
          <Card>
            <CardContent className="flex flex-col gap-2 px-4 py-6 text-center">
              <h3 className="text-sm font-semibold">
                No activities matched your profile in this window
              </h3>
              <p className="text-xs text-muted-foreground">
                Try widening the date range or adding more topics to the
                profile description.
              </p>
            </CardContent>
          </Card>
        ) : filtered.length === 0 ? (
          <Card>
            <CardContent className="px-4 py-6 text-center text-xs text-muted-foreground">
              No items match the current filters. Loosen them above.
            </CardContent>
          </Card>
        ) : (
          filtered.map((item) => (
            <AwarenessCard key={item.activity.id} item={item} />
          ))
        )}
      </section>
    </div>
  );
}
