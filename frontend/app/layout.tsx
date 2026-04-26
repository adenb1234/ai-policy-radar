import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "AI Policy Radar",
  description: "Personalized awareness for the AI-policy ecosystem.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col bg-background text-foreground">
        <header className="sticky top-0 z-30 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/70">
          <div className="mx-auto flex h-12 w-full max-w-screen-2xl items-center gap-6 px-4">
            <Link
              href="/"
              className="font-heading text-sm font-semibold tracking-tight"
            >
              AI Policy Radar
            </Link>
            <nav className="flex items-center gap-4 text-xs text-muted-foreground">
              <Link
                href="/"
                className="hover:text-foreground transition-colors"
              >
                Profiles
              </Link>
              <Link
                href="/entities"
                className="hover:text-foreground transition-colors"
              >
                Entities
              </Link>
            </nav>
            <div className="ml-auto text-[10px] uppercase tracking-wider text-muted-foreground">
              analyst preview
            </div>
          </div>
        </header>
        <main className="flex-1 w-full">{children}</main>
      </body>
    </html>
  );
}
