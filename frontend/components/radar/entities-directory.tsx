"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { Input } from "@/components/ui/input";
import type { EntitySummary } from "@/lib/api";
import { humanizeLabel } from "@/lib/format";

type Props = { entities: EntitySummary[] };

export function EntitiesDirectory({ entities }: Props) {
  const [q, setQ] = useState("");

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return entities;
    return entities.filter((e) => {
      if (e.name.toLowerCase().includes(needle)) return true;
      if (e.aliases.some((a) => a.toLowerCase().includes(needle)))
        return true;
      return false;
    });
  }, [q, entities]);

  const grouped = useMemo(() => {
    const map = new Map<string, EntitySummary[]>();
    for (const e of filtered) {
      const k = e.entity_type || "other";
      if (!map.has(k)) map.set(k, []);
      map.get(k)!.push(e);
    }
    return Array.from(map.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [filtered]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-3">
        <Input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search by name or alias…"
          className="max-w-sm"
        />
        <span className="text-xs text-muted-foreground">
          {filtered.length} of {entities.length} entit
          {entities.length === 1 ? "y" : "ies"}
        </span>
      </div>

      <div className="flex flex-col gap-6">
        {grouped.length === 0 ? (
          <p className="text-sm text-muted-foreground">No matches.</p>
        ) : (
          grouped.map(([type, list]) => (
            <section key={type}>
              <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                {humanizeLabel(type)}{" "}
                <span className="text-muted-foreground/70">({list.length})</span>
              </h3>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {list.map((e) => (
                  <Link
                    key={e.id}
                    href={`/entities/${e.id}`}
                    className="group rounded-lg border bg-card px-3 py-2 text-sm transition-colors hover:bg-muted"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate font-medium group-hover:underline">
                        {e.name}
                      </span>
                      {e.jurisdiction ? (
                        <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                          {e.jurisdiction}
                        </span>
                      ) : null}
                    </div>
                    {e.subcategory ? (
                      <div className="mt-0.5 text-[11px] text-muted-foreground">
                        {humanizeLabel(e.subcategory)}
                      </div>
                    ) : null}
                  </Link>
                ))}
              </div>
            </section>
          ))
        )}
      </div>
    </div>
  );
}
