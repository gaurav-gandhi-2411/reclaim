// Self-contained squarified treemap (Bruls, Huizing & van Wijk, 2000) — no charting library
// dependency (none is in the project's deps and the spec doesn't call for one).
import { categoryColorVar } from "./categories.js";

const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * Lays out `items` (each `{ value, ...arbitrary payload }`, `value > 0`) inside the rectangle
 * `{x, y, w, h}` using the squarify algorithm, which greedily grows each "row" (a strip laid
 * out along the shorter side of the remaining rectangle) as long as adding the next item keeps
 * the row's worst aspect ratio from getting worse — this is what keeps individual cells close
 * to square instead of degenerating into thin slivers.
 *
 * Returns an array of `{ item, x, y, w, h }` in the same units as the input rectangle.
 */
export function squarify(items, x, y, w, h) {
  const total = items.reduce((sum, item) => sum + item.value, 0);
  if (total <= 0 || items.length === 0 || w <= 0 || h <= 0) return [];

  const scale = (w * h) / total;
  let remaining = items.map((item) => ({ item, area: item.value * scale }));
  const rects = [];
  let rx = x;
  let ry = y;
  let rw = w;
  let rh = h;

  const worstRatio = (row, length) => {
    const rowSum = row.reduce((sum, entry) => sum + entry.area, 0);
    if (rowSum === 0 || length === 0) return Infinity;
    const rmax = Math.max(...row.map((entry) => entry.area));
    const rmin = Math.min(...row.map((entry) => entry.area));
    const s2 = rowSum * rowSum;
    const l2 = length * length;
    return Math.max((l2 * rmax) / s2, s2 / (l2 * rmin));
  };

  const layoutRow = (row, horizontal, length) => {
    const rowSum = row.reduce((sum, entry) => sum + entry.area, 0);
    const thickness = length > 0 ? rowSum / length : 0;
    let offset = 0;
    for (const entry of row) {
      const entryLength = thickness > 0 ? entry.area / thickness : 0;
      if (horizontal) {
        rects.push({ item: entry.item, x: rx + offset, y: ry, w: entryLength, h: thickness });
      } else {
        rects.push({ item: entry.item, x: rx, y: ry + offset, w: thickness, h: entryLength });
      }
      offset += entryLength;
    }
    return thickness;
  };

  while (remaining.length > 0) {
    const horizontal = rw >= rh;
    const length = horizontal ? rh : rw;
    let row = [remaining[0]];
    let rest = remaining.slice(1);
    let currentWorst = worstRatio(row, length);

    while (rest.length > 0) {
      const candidateRow = [...row, rest[0]];
      const candidateWorst = worstRatio(candidateRow, length);
      if (candidateWorst <= currentWorst) {
        row = candidateRow;
        rest = rest.slice(1);
        currentWorst = candidateWorst;
      } else {
        break;
      }
    }

    const thickness = layoutRow(row, horizontal, length);
    if (horizontal) {
      ry += thickness;
      rh -= thickness;
    } else {
      rx += thickness;
      rw -= thickness;
    }
    remaining = rest;
    if (rw <= 0 || rh <= 0) break;
  }

  return rects;
}

function el(name, attrs) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

/**
 * Renders `nodes` (treemap API response nodes: `{ path, label, size_bytes, size_human,
 * category_group, category_label, is_dir, is_candidate, is_inaccessible, explanation }`) into
 * `svgEl`, wiring up `tooltipEl` (hover/focus) to show the real path and formatted byte size —
 * never a rounded figure standing in for the exact one, since `size_human` already carries the
 * honest formatting from the API and the tooltip never recomputes its own.
 *
 * P0-5 treemap follow-up: a node with `is_inaccessible: true` (the single synthetic
 * "inaccessible" bucket `build_treemap` appends) gets the `rc-treemap-node--inaccessible` class
 * — a hatched fill plus dashed stroke (see styles.css) so it reads as informational/estimated
 * rather than a real, selectable directory even before a user hovers it — and its tooltip
 * appends `node.explanation`, the one-line reason string the API provides for exactly this node
 * (`null` for every ordinary node). That node is deliberately NOT fed into `squarify` alongside
 * the real, size-proportional nodes: its best-effort byte estimate can be many orders of
 * magnitude smaller than a real scan's biggest directories (or, per `InaccessibleSummary`,
 * entirely absent — every inaccessible path can have an unknown size, `known_bytes == 0`), and
 * squarify's row-packing can degenerate under an extreme size disparity (a single dominant item
 * can compute a row "thickness" taller than the whole remaining rectangle, which zeroes out the
 * rectangle and drops every remaining item — reproduced directly against a 500MB-vs-12KB pair
 * in tests/frontend/treemap-inaccessible.test.mjs before this fix). Reserved instead as a fixed-
 * height strip along the bottom of the canvas — always visible regardless of its relative size,
 * which nothing here claims is proportionally accurate anyway (see its explanation string).
 */
export function renderTreemap(svgEl, tooltipEl, nodes) {
  svgEl.innerHTML = "";
  if (nodes.length === 0) return;

  const bbox = svgEl.viewBox.baseVal;
  const width = bbox.width || svgEl.clientWidth || 800;
  const height = bbox.height || svgEl.clientHeight || 520;
  svgEl.setAttribute("viewBox", `0 0 ${width} ${height}`);

  const inaccessibleNode = nodes.find((node) => node.is_inaccessible);
  const ordinaryNodes = nodes.filter((node) => !node.is_inaccessible);

  const stripHeight = inaccessibleNode ? Math.min(32, height * 0.15) : 0;
  const mapHeight = height - stripHeight;

  const items = ordinaryNodes.map((node) => ({ value: Math.max(node.size_bytes, 1), node }));
  const rects = squarify(items, 0, 0, width, mapHeight);

  for (const rect of rects) {
    drawNode(svgEl, tooltipEl, rect.item.node, rect);
  }

  if (inaccessibleNode) {
    drawNode(svgEl, tooltipEl, inaccessibleNode, { x: 0, y: mapHeight, w: width, h: stripHeight });
  }
}

function drawNode(svgEl, tooltipEl, node, rect) {
  const groupClass = node.is_inaccessible
    ? "rc-treemap-node rc-treemap-node--inaccessible"
    : "rc-treemap-node";
  const ariaLabel = node.is_inaccessible
    ? `${node.label}, ${node.category_label}, ${node.size_human} (estimated). ${node.explanation}`
    : `${node.label}, ${node.category_label}, ${node.size_human}`;
  const group = el("g", {
    class: groupClass,
    tabindex: "0",
    role: "img",
    "aria-label": ariaLabel,
  });
  group.appendChild(
    el("rect", {
      x: rect.x.toFixed(2),
      y: rect.y.toFixed(2),
      width: Math.max(rect.w, 0).toFixed(2),
      height: Math.max(rect.h, 0).toFixed(2),
      fill: categoryColorVar(node.category_group),
    })
  );
  if (rect.w > 46 && rect.h > 18) {
    const text = el("text", { x: (rect.x + 6).toFixed(2), y: (rect.y + 16).toFixed(2) });
    text.textContent = node.label;
    group.appendChild(text);
  }

  const show = () => showTooltip(tooltipEl, node, rect, svgEl);
  const hide = () => {
    tooltipEl.style.visibility = "hidden";
  };
  group.addEventListener("mouseenter", show);
  group.addEventListener("mousemove", show);
  group.addEventListener("mouseleave", hide);
  group.addEventListener("focus", show);
  group.addEventListener("blur", hide);
  svgEl.appendChild(group);
}

function showTooltip(tooltipEl, node, rect, svgEl) {
  tooltipEl.textContent = node.is_inaccessible
    ? `${node.category_label} — ${node.size_human} (estimated). ${node.explanation}`
    : `${node.path} — ${node.size_human} (${node.category_label})`;
  const svgRect = svgEl.getBoundingClientRect();
  const scaleX = svgRect.width / (svgEl.viewBox.baseVal.width || svgRect.width);
  const scaleY = svgRect.height / (svgEl.viewBox.baseVal.height || svgRect.height);
  tooltipEl.style.left = `${rect.x * scaleX + 8}px`;
  tooltipEl.style.top = `${rect.y * scaleY + 8}px`;
  tooltipEl.style.visibility = "visible";
}
