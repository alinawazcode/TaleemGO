// Patches whatsapp-web.js 1.34.x so media sends work.
// Known upstream bug: the internal `__x_id` field of mediaOptions is spread into the outgoing
// message and overwrites its id -> "Data passed to getter must include an id property".
// Safe to run many times. Runs automatically after `npm install` (see package.json "postinstall").
const fs = require("fs");
const path = require("path");

const file = process.argv[2] ||
  path.join(__dirname, "node_modules", "whatsapp-web.js", "src", "util", "Injected", "Utils.js");
// Historical identifier preserved for compatibility with already-patched node_modules
const MARK = "/*saathi-patch*/";

if (!fs.existsSync(file)) {
  console.error("[patch] file not found:", file);
  process.exit(1);
}
const src = fs.readFileSync(file, "utf8");
if (src.includes(MARK)) {
  console.log("[patch] already applied");
  process.exit(0);
}

let count = 0;
const out = src.replace(/\.\.\.mediaOptions(\s*[,}])/g, (_m, tail) => {
  count++;
  return `...${MARK}(mediaOptions ? (({ __x_id, ...rest }) => rest)(mediaOptions) : {})${tail}`;
});

if (count === 0) {
  console.error("[patch] pattern '...mediaOptions' not found. Lines mentioning mediaOptions:");
  src.split(/\r?\n/).forEach((l, i) => { if (l.includes("mediaOptions")) console.error(`  ${i + 1}: ${l.trim()}`); });
  process.exit(2);
}
if (!fs.existsSync(file + ".bak")) fs.writeFileSync(file + ".bak", src);
fs.writeFileSync(file, out);
console.log(`[patch] applied (${count} place${count > 1 ? "s" : ""}). Backup: ${file}.bak`);