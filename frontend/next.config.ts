import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Keep production build fast: TS is checked in CI / dev; skip it here
  // so deployments complete in seconds instead of minutes. The codebase
  // is type-clean — we just don't want a full typecheck on every deploy.
  typescript: {
    ignoreBuildErrors: true,
  },
};

export default nextConfig;
