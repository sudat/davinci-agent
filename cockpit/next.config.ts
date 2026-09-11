import type { NextConfig } from "next";

const cockpitApi = process.env.COCKPIT_API ?? "http://127.0.0.1:8765";

const nextConfig: NextConfig = {
  // Dev-only: floating "N" overlay overlaps primary controls at 375px in
  // screenshots. Production unaffected.
  devIndicators: false,
  async rewrites() {
    return [
      {
        source: "/cockpit-api/:path*",
        destination: `${cockpitApi}/:path*`,
      },
    ];
  },
};

export default nextConfig;
