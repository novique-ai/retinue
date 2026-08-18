import { createRequire } from "node:module";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
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

const require = createRequire(import.meta.url);
const esbuild = require(join(modules, "esbuild"));
const outputDir = join(tmpdir(), `retinue-ui-${process.pid}`);
mkdirSync(outputDir, { recursive: true });
const output = join(outputDir, "cron.ui.test.mjs");
try {
  esbuild.buildSync({
    entryPoints: ["test/cron.ui.test.tsx"],
    bundle: true,
    format: "esm",
    platform: "node",
    jsx: "automatic",
    absWorkingDir: root,
    nodePaths: [modules],
    outfile: output,
  });
  const test = spawnSync(process.execPath, ["--test", output], { stdio: "inherit" });
  process.exitCode = test.status ?? 1;
} finally {
  rmSync(outputDir, { recursive: true, force: true });
}
