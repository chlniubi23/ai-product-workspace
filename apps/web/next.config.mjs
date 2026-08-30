import os from "node:os";
import path from "node:path";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  experimental: {
    typedRoutes: false,
  },
  webpack: (config, { dev }) => {
    // This repo lives under OneDrive, whose sync scanner holds handles on
    // .next/cache/webpack/*.pack.gz while webpack renames them, producing
    // "ENOENT: rename ... 0.pack.gz_ -> 0.pack.gz" warnings and cold rebuilds.
    // Keeping the dev cache outside the synced tree avoids the race. Production
    // builds keep the default location so CI behaviour is unchanged.
    if (dev && config.cache && config.cache.type === "filesystem") {
      config.cache.cacheDirectory = path.join(os.tmpdir(), "apw-next-cache");
    }
    return config;
  },
};

export default nextConfig;
