/** @type {import('next').NextConfig} */
const backend = process.env.PATCHPILOT_API_URL ?? "http://127.0.0.1:8000";

const nextConfig = {
  reactStrictMode: true,
  // Next gzips proxied responses by default, which buffers them. That is
  // invisible for JSON and fatal for Server-Sent Events: the run timeline
  // arrived in one burst when the run ended instead of live.
  compress: false,
  // Proxy the API in development so the browser talks to one origin and SSE
  // works without CORS preflight on the EventSource connection.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
  // The Benchmarks section became Results; keep old links and bookmarks working.
  async redirects() {
    return [
      { source: "/benchmarks", destination: "/results", permanent: false },
      { source: "/benchmarks/:id", destination: "/results", permanent: false },
    ];
  },
};

export default nextConfig;
