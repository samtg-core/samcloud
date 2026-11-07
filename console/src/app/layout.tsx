import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import UserNav from "@/components/UserNav";
import InboxBadge from "@/components/InboxBadge";
import { ThumbNav } from "@/components/ThumbNav";
import { ScopePill } from "@/components/ScopePill";
import { ThreadPill } from "@/components/ThreadPill";
import { InboxQueuePill } from "@/components/InboxQueuePill";
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
  title: "Samcloud Console",
  description: "Home services dashboard",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="dark">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased min-h-screen bg-zinc-950 text-zinc-100`}
      >
        <div className="flex min-h-screen flex-col">
          <header className="border-b border-zinc-800 px-4 sm:px-6 py-3 sm:py-4 overflow-hidden">
            <div className="flex items-center gap-4">
              <h1 className="text-xl font-semibold tracking-tight shrink-0">
                <span className="text-zinc-400">sam</span>cloud
              </h1>
              <nav className="flex items-center gap-3 sm:gap-6 text-sm overflow-x-auto ml-auto scrollbar-none">
                <ScopePill />
                <ThreadPill />
                <a href="/" className="text-zinc-100 hover:text-white whitespace-nowrap">Dashboard</a>
                <InboxBadge />
                <a href="/admin" className="text-zinc-400 hover:text-white whitespace-nowrap">Admin</a>
                <a href="/tickets" className="text-zinc-400 hover:text-white whitespace-nowrap">Tickets</a>
                <a href="/docs" className="text-zinc-400 hover:text-white whitespace-nowrap">Docs</a>
                <a href="/projects" className="text-zinc-400 hover:text-white whitespace-nowrap">Projects</a>
                <UserNav />
              </nav>
            </div>
          </header>
          <main className="flex-1 p-4 sm:p-6">
            {children}
          </main>
        </div>
        <ThumbNav />
        <InboxQueuePill />
      </body>
    </html>
  );
}
