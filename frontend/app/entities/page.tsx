import Link from "next/link";
import { EntitiesDirectory } from "@/components/radar/entities-directory";
import { listEntities } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function EntitiesPage() {
  let entities: Awaited<ReturnType<typeof listEntities>> = [];
  let error: string | null = null;
  try {
    entities = await listEntities();
  } catch (e) {
    error = e instanceof Error ? e.message : "failed to load entities";
  }

  return (
    <div className="mx-auto w-full max-w-screen-2xl px-6 py-8">
      <div className="mb-6">
        <div className="mb-2 text-xs text-muted-foreground">
          <Link href="/" className="hover:text-foreground">
            ← profiles
          </Link>
        </div>
        <h1 className="font-heading text-2xl font-semibold tracking-tight">
          Entity directory
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {entities.length} entit{entities.length === 1 ? "y" : "ies"} tracked
          across the AI-policy ecosystem.
        </p>
      </div>

      {error ? (
        <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          {error}
        </div>
      ) : (
        <EntitiesDirectory entities={entities} />
      )}
    </div>
  );
}
