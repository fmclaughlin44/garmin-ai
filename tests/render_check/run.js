/*
 * Executes the built dashboard's render pass under a minimal DOM and reports
 * whether every mount actually populated.
 *
 * This exists because a syntax check passes on a page that renders nothing. A
 * duplicate element id once made one renderer wipe its own section, and every
 * section after it silently stopped drawing -- no syntax error, no warning, just
 * most of the dashboard missing. Checking that the mounts fill is the only way
 * to catch that from the command line.
 *
 * Usage: node run.js path/to/dashboard.html
 */
const fs = require("fs");
const { DOC, El } = require("./shim.js");

const html = fs.readFileSync(process.argv[2], "utf8");
const markup = html.split("<script>")[0];

// Register ids in document order; the shim keeps the first, as browsers do.
for (const m of markup.matchAll(/id="([^"]+)"/g)) {
  const e = new El("div");
  e.setAttribute("id", m[1]);
}
const page = new El("div");
DOC.querySelector = () => page;

const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

// Mounts that are legitimately empty in some states, so existence is all that
// can be asserted.
// "Coming up next" has nothing to show once the block ends, and the week
// calendar is deliberately emptied in post-race mode in favour of a notice.
const CONDITIONAL = new Set(["cu-list", "wk-cal"]);

const MOUNTS = [
  "jump", "range-group", "wk-cal", "cu-list", "tiles", "r-components",
  "chart-readiness", "chart-form", "chart-load", "chart-rhr", "chart-nocturnal",
  "chart-sleep", "chart-mileage", "block-weeks", "nut-macros", "nut-energy",
  "table-nutrition", "coach-body", "prov-yes", "p-reads", "table-ironman", "im-insights",
  "lift-templates", "plan-rules", "athlete-ref", "perceived-band", "perceived-note", "arc-phases", "arc-note", "nut-phase",
];

const failures = [];
try {
  new Function(script)();
} catch (err) {
  failures.push(`render threw: ${err.message}`);
}

for (const id of MOUNTS) {
  const e = DOC.getElementById(id);
  if (!e) { failures.push(`${id}: element missing`); continue; }
  if (CONDITIONAL.has(id)) continue;
  const filled = e.children.length || (e.innerHTML ? 1 : 0) || (e.textContent ? 1 : 0);
  if (!filled) failures.push(`${id}: rendered empty`);
}

if (failures.length) {
  console.log("FAIL");
  failures.forEach((f) => console.log("  " + f));
  process.exit(1);
}
console.log(`OK — ${MOUNTS.length} mounts populated`);
