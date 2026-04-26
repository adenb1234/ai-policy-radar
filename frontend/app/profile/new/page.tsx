"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { createProfile, ApiError } from "@/lib/api";

// NOTE: Structured-overrides UI is deferred. The MVP relies on the NL
// description being passed through the backend ProfileBuilder, which
// extracts a structured representation server-side.

const PLACEHOLDER = `Describe your organization and what you care about.

E.g., "I'm the policy lead at a frontier AI lab. I care about export controls, compute thresholds, the EU AI Act, and any state-level pre-emption fights. I want to know about anything that could change our training-cluster siting decisions or our model-release calculus."`;

export default function NewProfilePage() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [nlDescription, setNlDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    if (!name.trim() || !nlDescription.trim()) {
      setError("Both name and description are required.");
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      const profile = await createProfile({
        name: name.trim(),
        nl_description: nlDescription.trim(),
      });
      router.push(`/dashboard/${profile.profile_id}`);
    } catch (e) {
      if (e instanceof ApiError) {
        setError(`API ${e.status}: ${e.body.slice(0, 300)}`);
      } else if (e instanceof Error) {
        setError(e.message);
      } else {
        setError("Unknown error creating profile.");
      }
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-3xl px-6 py-10">
      <div className="mb-6 flex items-center gap-2 text-xs text-muted-foreground">
        <Link href="/" className="hover:text-foreground">
          ← back to profiles
        </Link>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>New profile</CardTitle>
          <CardDescription>
            Describe an organization and what they monitor. The backend
            extracts a structured representation (topics, watch entities,
            jurisdictions) and uses it to score every incoming activity.
          </CardDescription>
        </CardHeader>
        <form onSubmit={handleSubmit}>
          <CardContent className="flex flex-col gap-4">
            <label className="flex flex-col gap-1.5">
              <span className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                Profile name
              </span>
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Frontier lab — policy lead"
                disabled={submitting}
                required
                maxLength={120}
              />
            </label>

            <label className="flex flex-col gap-1.5">
              <span className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                Natural-language description
              </span>
              <Textarea
                value={nlDescription}
                onChange={(e) => setNlDescription(e.target.value)}
                placeholder={PLACEHOLDER}
                disabled={submitting}
                required
                rows={10}
                className="min-h-[16rem] font-sans"
              />
              <span className="text-[11px] text-muted-foreground">
                Be specific. Mention jurisdictions, topics, and the kinds of
                actions you&rsquo;d want to take.
              </span>
            </label>

            {error ? (
              <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-xs text-destructive">
                {error}
              </div>
            ) : null}
          </CardContent>
          <CardFooter className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">
              {submitting
                ? "Building structured profile via Claude — this can take ~15s."
                : "Submit to build profile and open the dashboard."}
            </span>
            <Button type="submit" disabled={submitting}>
              {submitting ? "Creating…" : "Create profile"}
            </Button>
          </CardFooter>
        </form>
      </Card>
    </div>
  );
}
