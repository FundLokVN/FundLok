// Export every template to static HTML for Python to fill in with Jinja.
//
//   npm run export
//
// Writes app/email_templates/ from scratch (the React Email exporter deletes
// the folder first), so nothing hand-written may live there. Commit the result:
// the API image has no Node, it only reads these files.
import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const outDir = path.resolve(root, "../app/email_templates");

execFileSync(
  "npx",
  ["email", "export", "--dir", "src/templates", "--outDir", outDir, "--silent"],
  { cwd: root, stdio: "inherit" },
);

// The exporter also copies src/templates/static (the dev-preview logo). The
// real emails attach app/assets/email-logo.png instead, so drop the copy.
rmSync(path.join(outDir, "static"), { recursive: true, force: true });

const exported = existsSync(outDir)
  ? readdirSync(outDir).filter((name) => name.endsWith(".html"))
  : [];
if (exported.length === 0) {
  console.error(`No templates were exported to ${outDir}`);
  process.exit(1);
}

writeFileSync(
  path.join(outDir, "README.md"),
  [
    "# Generated: do not edit",
    "",
    "These files are exported from the React Email templates in",
    "`emails/src/templates/`. Edit those, then run `npm run export` in `emails/`",
    "and commit this folder. The export rewrites it from scratch.",
    "",
    "`{{ … }}` and `{% … %}` are Jinja, filled in by `app/utils/email_templates.py`.",
    "",
  ].join("\n"),
);

console.log(`Exported ${exported.length} templates to ${path.relative(process.cwd(), outDir)}`);
