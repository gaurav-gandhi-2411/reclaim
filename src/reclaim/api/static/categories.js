// Fixed categorical order — must mirror the order in tokens.css and the palette used to
// generate it (see Stage 6 report for the validator invocations). Never reorder or reassign
// these; a stable order is part of what makes a categorical palette legible across views.
export const CATEGORY_ORDER = [
  "dev_artifacts",
  "package_caches",
  "temp_and_browser_caches",
  "crash_dumps",
  "old_installers",
  "archive_pairs",
  "large_logs",
  "duplicates",
];

const CATEGORY_VARS = {
  dev_artifacts: "--rc-cat-dev-artifacts",
  package_caches: "--rc-cat-package-caches",
  temp_and_browser_caches: "--rc-cat-temp-browser-caches",
  crash_dumps: "--rc-cat-crash-dumps",
  old_installers: "--rc-cat-old-installers",
  archive_pairs: "--rc-cat-archive-pairs",
  large_logs: "--rc-cat-large-logs",
  duplicates: "--rc-cat-duplicates",
};

/** CSS `var(--rc-cat-...)` reference for a category_group id; falls back to the "other" /
 * uncategorized swatch for anything not in the fixed detector-category set. */
export function categoryColorVar(categoryGroup) {
  const name = CATEGORY_VARS[categoryGroup] ?? "--rc-cat-other";
  return `var(${name})`;
}
