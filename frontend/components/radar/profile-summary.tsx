"use client";

import { useState } from "react";
import { cn } from "@/lib/format";

type Props = {
  name: string;
  description: string;
  topics?: string[];
  jurisdictions?: string[];
  recencyDays?: number;
  riskTolerance?: string;
};

export function ProfileSummary({
  name,
  description,
  topics = [],
  jurisdictions = [],
  recencyDays,
  riskTolerance,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const truncated = description.length > 200;
  const visible = expanded || !truncated
    ? description
    : description.slice(0, 200).trimEnd() + "…";

  const sortedTopics = [...topics].slice(0, 8);

  return (
    <div className="flex flex-col gap-2 border-b bg-muted/30 px-6 py-4">
      <div className="flex items-center gap-3">
        <h1 className="font-heading text-xl font-semibold tracking-tight">
          {name}
        </h1>
        <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
          dashboard
        </span>
      </div>
      <p
        className={cn(
          "max-w-4xl text-sm text-muted-foreground",
          !expanded && truncated ? "line-clamp-3" : "",
        )}
      >
        {visible}
      </p>
      {truncated ? (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="self-start text-[11px] text-primary hover:underline"
        >
          {expanded ? "show less" : "show more"}
        </button>
      ) : null}
      <div className="flex flex-wrap items-center gap-2 pt-1 text-[11px] text-muted-foreground">
        {recencyDays ? (
          <span className="rounded border border-border bg-background px-1.5 py-0.5">
            window {recencyDays}d
          </span>
        ) : null}
        {riskTolerance ? (
          <span className="rounded border border-border bg-background px-1.5 py-0.5">
            risk: {riskTolerance.replace(/_/g, " ")}
          </span>
        ) : null}
        {jurisdictions.length > 0
          ? jurisdictions.slice(0, 6).map((j) => (
              <span
                key={j}
                className="rounded border border-border bg-background px-1.5 py-0.5"
              >
                {j}
              </span>
            ))
          : null}
        {sortedTopics.map((t) => (
          <span
            key={t}
            className="rounded border border-border bg-background px-1.5 py-0.5"
          >
            {t}
          </span>
        ))}
      </div>
    </div>
  );
}
