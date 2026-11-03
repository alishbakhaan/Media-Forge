// Bundles src/ into dist/ with esbuild. `--watch` rebuilds on change (the backend serves dist/).
import * as esbuild from "esbuild";
import { cpSync, mkdirSync, rmSync } from "node:fs";

const watch = process.argv.includes("--watch");
rmSync("dist", { recursive: true, force: true });
mkdirSync("dist", { recursive: true });
cpSync("public", "dist", { recursive: true });

const options = {
  entryPoints: ["src/main.jsx"],
  bundle: true,
  outdir: "dist/assets",
  entryNames: "app",
  format: "esm",
  jsx: "automatic",
  minify: !watch,
  sourcemap: watch,
  target: ["es2020"],
  loader: { ".css": "css", ".woff2": "file", ".woff": "file" },
  assetNames: "[name]-[hash]",
  define: { "process.env.NODE_ENV": JSON.stringify(watch ? "development" : "production") },
  logLevel: "info",
};

if (watch) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  console.log("watching src/ … (Ctrl+C to stop)");
} else {
  await esbuild.build(options);
}
