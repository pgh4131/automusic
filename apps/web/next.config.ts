import type { NextConfig } from "next";
import { loadEnvConfig } from "@next/env";
import path from "node:path";

loadEnvConfig(path.resolve(process.cwd(), "../.."));
const nextConfig: NextConfig = {
  poweredByHeader: false,
};
export default nextConfig;
