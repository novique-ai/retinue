// Canonical retinue-web test runner. `.github/workflows/retinue.yml` invokes
// this and nothing else, so a suite is covered exactly when this file finds
// it.
//
// Discovery walks BOTH homes: `test/` (suites that mount components) and
// colocated `src/**/*.test.ts(x)` (suites that sit next to the unit they
// cover). Naming entry points in the workflow instead — which is how
// `src/voice/ptt.test.ts` and `src/thinking.test.ts` were wired — means a
// renamed or added suite stops running with nothing going red.
import { createRequire } from "node:module";
import { existsSync, mkdirSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const modules = join(root, "node_modules");
if (!existsSync(modules)) {
  const install = spawnSync(
    "npm",
    ["ci", "--prefer-offline", "--no-audit", "--no-fund"],
    { cwd: root, stdio: "inherit" },
  );
  if (install.status !== 0) {
    throw new Error("UI tests require the offline npm cache to install dependencies");
  }
}

/** Directories that can never hold a first-party suite. */
const SKIP_DIRS = new Set(["node_modules", "dist", "public", ".vite"]);

/** Every `*.test.ts(x)` under `dir`, as paths relative to the package root. */
function findSuites(dir) {
  if (!existsSync(dir)) return [];
  const found = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name)) continue;
      found.push(...findSuites(join(dir, entry.name)));
    } else if (/\.test\.(ts|tsx)$/.test(entry.name)) {
      found.push(relative(root, join(dir, entry.name)));
    }
  }
  return found;
}

const require = createRequire(import.meta.url);
const esbuild = require(join(modules, "esbuild"));
const outputDir = join(tmpdir(), `retinue-ui-${process.pid}`);
mkdirSync(outputDir, { recursive: true });
const entries = [
  ...findSuites(join(root, "test")),
  ...findSuites(join(root, "src")),
].sort();
// An empty scan is a broken runner, not a clean run — the whole point of #180
// was a suite that silently never executed.
if (entries.length === 0) {
  throw new Error("no UI tests found under test/ or src/ (*.test.ts(x))");
}
console.log(`▶ ${entries.length} UI suite(s): ${entries.join(", ")}`);
try {
  let failed = false;
  for (const entry of entries) {
    const output = join(outputDir, `${entry.replace(/[\\/]/g, "__")}.mjs`);
    esbuild.buildSync({
      entryPoints: [entry],
      bundle: true,
      format: "esm",
      platform: "node",
      jsx: "automatic",
      absWorkingDir: root,
      nodePaths: [modules],
      outfile: output,
    });
    const test = spawnSync(process.execPath, ["--test", output], { stdio: "inherit" });
    if ((test.status ?? 1) !== 0) failed = true;
  }
  process.exitCode = failed ? 1 : 0;
} finally {
  rmSync(outputDir, { recursive: true, force: true });
}
