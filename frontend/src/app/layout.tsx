import type { Metadata } from "next";

import { Nav } from "@/components/nav";

import "./globals.css";

export const metadata: Metadata = {
  title: "PatchPilot — Autonomous Software Engineering Agent",
  description:
    "An autonomous bug-fixing agent with LLM tool use, iterative test feedback, isolated execution, and official SWE-bench Lite evaluation.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="dark">
      <body className="min-h-screen antialiased">
        <Nav />
        <main className="mx-auto w-full max-w-[1400px] px-5 py-6">{children}</main>
      </body>
    </html>
  );
}
