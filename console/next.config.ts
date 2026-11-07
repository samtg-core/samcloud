import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  serverExternalPackages: ["better-sqlite3"],
  // Uploads (#378): with middleware matching /api/files, Next silently
  // truncates request bodies at its 10MB default ("Request body exceeded
  // 10MB" in prod logs) — the proxy then waits forever for bytes that never
  // arrive. Lift the cap above the registry's own limit; the registry stays
  // the real enforcer, and a cap it cannot reach is a cap that lies.
  //
  // 2 GB, against a registry default of 1 GB (#889): the headroom is so that
  // raising REGISTRY_FILE_MAX_MB — an env change, no deploy — does not
  // silently make the console the limiter again, which is what 900 MB would
  // have done the moment the registry cap passed it.
  experimental: {
    middlewareClientMaxBodySize: 2048 * 1024 * 1024,
  },
  // Singular-path aliases (#327): agents kept writing /ticket/{id} style links
  // in comments (producer, at least), which 404'd for every reader. Serving
  // both spellings costs nothing and un-breaks the historical links already
  // stored in ticket/doc bodies — those can't be rewritten.
  async redirects() {
    return [
      { source: "/ticket/:id", destination: "/tickets/:id", permanent: true },
      { source: "/task/:id", destination: "/tasks/:id", permanent: true },
      { source: "/doc/:id", destination: "/docs/:id", permanent: true },
    ];
  },
};

export default nextConfig;
