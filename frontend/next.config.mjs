/** @type {import('next').NextConfig} */
const backend = process.env.PATCHPILOT_API_URL ?? "http://127.0.0.1:8000";

const nextConfig = {
  reactStrictMode: true,
  // Proxy the API in development so the browser talks to one origin and SSE
  // works without CORS preflight on the EventSource connection.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

export default nextConfig;
