// `bun run db:copy` launcher: finds a Python 3 on macOS, Linux or Windows and
// runs scripts/db_migrate.py with the same arguments and the same terminal.
// (A plain "python3 …" script breaks on Windows, where `python3` is usually a
// Microsoft Store stub and the real interpreter is `py` or `python`.)
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

const root = join(import.meta.dir, "..");
const script = join(import.meta.dir, "db_migrate.py");
const args = process.argv.slice(2);
const isWindows = process.platform === "win32";

// The project venv first (known-good Python), then whatever the OS provides.
const venvPython = isWindows ? join(root, ".venv", "Scripts", "python.exe") : join(root, ".venv", "bin", "python");
const candidates: string[][] = [
  ...(existsSync(venvPython) ? [[venvPython]] : []),
  ...(isWindows ? [["py", "-3"], ["python"], ["python3"]] : [["python3"], ["python"]]),
];

const WINDOWS_STORE_STUB_EXIT = 9009; // "Python was not found; run without arguments to install from the Microsoft Store"

for (const [command, ...prefix] of candidates) {
  const result = spawnSync(command, [...prefix, script, ...args], { stdio: "inherit" });
  const missing = (result.error as NodeJS.ErrnoException | undefined)?.code === "ENOENT";
  if (missing || (isWindows && result.status === WINDOWS_STORE_STUB_EXIT)) continue;
  process.exit(result.status ?? 1);
}

console.error(
  "[db-migrate] ERROR: Python 3 not found. Install it from https://www.python.org/downloads/ and run `bun run db:copy` again.",
);
process.exit(1);
