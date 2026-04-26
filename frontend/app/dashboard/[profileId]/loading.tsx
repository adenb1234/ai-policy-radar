import { Skeleton } from "@/components/ui/skeleton";

export default function Loading() {
  return (
    <div>
      <div className="border-b bg-muted/30 px-6 py-4">
        <Skeleton className="mb-2 h-6 w-64" />
        <Skeleton className="h-4 w-full max-w-3xl" />
      </div>
      <div className="mx-auto grid w-full max-w-screen-2xl grid-cols-1 gap-6 px-6 py-6 lg:grid-cols-[260px_1fr]">
        <Skeleton className="h-72 w-full" />
        <div className="flex flex-col gap-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-32 w-full" />
          ))}
        </div>
      </div>
    </div>
  );
}
