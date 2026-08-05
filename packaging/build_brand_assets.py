"""Render Reclaim's brand assets (Windows .ico, Inno Setup wizard bitmaps, OG preview, README
logo lockup) from the same isometric "lifted platter" geometry as
`src/reclaim/api/static/logo.svg` / `favicon.svg`.

Deliberately redraws the mark with Pillow's `ImageDraw` primitives instead of adding an
SVG-rasterizer dependency (cairosvg/resvg) -- each platter is one ellipse (the disk face) plus
one band (the disk's visible front rim: a flat-sided rectangle capped by a downward-bulging
half-ellipse, exactly mirroring the reference asset set's own
`M(cx-rx,cy) L(cx-rx,cy+ry) A(rx,ry) L(cx+rx,cy) A(rx,ry) Z` path shape), cheap to reproduce
exactly in pure PIL, so a new dependency would buy nothing.

v4 (visual identity PIVOT, newly approved "lifted platter" design): isometric stack of disk
platters, the top one lifted free in amber -- "reclaimed space" made literal instead of the v3
indigo/mint flat-rectangle "occupied ground" metaphor. Palette: deep #1B6FA8 / mid #2E9BD6 /
amber #F2A93B / dark ground #0F172A, decomposed directly from `reclaim_lifted_platter_asset_set
.svg`'s own concrete ellipse/path coordinates and hex fills (repo root) -- see that file for the
5 reference renderings this script's geometry ratios were measured from.

Size-adaptive rendering, per the design owner's explicit spec: 3 platters at 32px and above;
simplified to 2 platters (amber lifted platter + one combined blue platter beneath) below that,
matching the reference SVG's own 16px panel exactly -- verified by rendering both real sizes to
PNG and inspecting the pixel output (see PR description / task report), not assumed. The
per-platter aspect ratio (ry/rx ~= 0.36) and inter-platter gap ratios (stacked-pair gap ~= 1.75x
ry, lifted-pair gap ~= 2.5x ry for the 3-platter form; ~= 3.0x ry for the 2-platter form) were
measured directly off the reference SVG's 4 non-mono panels (app icon / installer tile / 32px /
16px), which agree with each other to within rounding -- not invented proportions.

The "installer tile" concept from the reference SVG (mark composited onto a dark #0F172A rounded
-square, distinct from the plain/transparent "app icon" form) is used here specifically for the
Inno Setup wizard images, since that is the one packaging context the reference explicitly shows
on dark ground. Every other raster asset (.ico, OG preview, README/docs lockup) uses the plain
transparent-background mark, matching the reference's "app icon" / "32px" / "16px" panels, none
of which draw an enclosing background shape.

Run with: `uv run python packaging/build_brand_assets.py`
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Palette, decomposed from reclaim_lifted_platter_asset_set.svg (repo root) -- see that file's
# own <ellipse>/<path> fill attributes. Cap = the disk's top face (the ellipse); band = the
# disk's visible front rim (the path). All four "palette" hexes named in the reference SVG's own
# caption text are represented; the two darker band-only shades (#124C74, #C2801F) are the
# reference's own band fills for the bottom and amber platters respectively, not invented.
DEEP = "#1B6FA8"  # bottom platter cap / middle platter band ("deep" in the design brief)
MID = "#2E9BD6"  # middle platter cap
AMBER = "#F2A93B"  # top (lifted) platter cap
DARK_GROUND = "#0F172A"  # installer-tile backdrop; also doubles as the wordmark's "re" ink
BAND_DARKEST = "#124C74"  # bottom platter band (3-platter form only)
BAND_AMBER = "#C2801F"  # amber platter band

# Standalone-raster canvas tokens -- literals here too (not read from tokens.css) since these are
# generated once at build time, not part of the app's live CSS cascade. Mirrors tokens.css's
# light-mode --rc-bg/--rc-text-muted.
BG = "#f6f7fb"
TEXT_MUTED = "#585e72"

# Below this pixel size, the mark simplifies from 3 platters to 2 (amber lifted platter + one
# combined blue platter beneath) -- explicit design-owner spec, verified by rendering both real
# sizes and inspecting the pixel output (see module docstring / task report), matching the
# reference SVG's own dedicated "16px" simplified panel rather than just shrinking the 3-platter
# form, which loses legibility once the gaps between platters approach 1px.
_TWO_PLATTER_MAX_SIZE = 24

# Per-platter proportions, measured off the reference SVG's app-icon / installer-tile / 32px / 16px
# panels (all agree to within rounding -- see module docstring). Expressed as ratios so the mark
# can be re-rendered at any pixel size without hand-tuning per-size numbers.
_RY_OVER_RX = 0.36
_BAND_HEIGHT_OVER_RY = 1.0  # band height == ry in every reference panel except the largest, which
# is off by ~2px relative to its own ry=16 (likely a small authoring inconsistency in the
# reference, not a deliberate ratio -- the other three panels agree exactly at band_height=ry).
_GAP_STACKED_OVER_RY = 1.75  # gap between two adjacent "still stacked" platter centers
_GAP_LIFTED_OVER_RY = 2.5  # gap between the middle platter and the lifted amber platter (3-platter)
_GAP_TWO_PLATTER_OVER_RY = 3.0  # gap in the simplified 2-platter form (matches reference exactly)

# Fraction of the canvas height the platter stack's own bounding box should fill, leaving margin
# on every side -- same role as an icon's safe-area padding.
_CONTENT_FILL_FRACTION = 0.84

# Windows ships Segoe UI at this path on every supported install -- matches tokens.css's
# --rc-font-sans stack. This script only ever runs on the Windows dev machine that builds the
# installer, so no cross-platform fallback path is needed beyond the PIL bitmap font safety net
# in `_font()`.
_FONT_DIR = Path("C:/Windows/Fonts")


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    """Convert a `#rrggbb` string to an `(r, g, b)` int tuple."""
    value = value.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return (r, g, b)


def _platter_layers(two_platter: bool) -> list[tuple[str, str]]:
    """Return `(band_color, cap_color)` pairs bottom-to-top for the requested platter count.

    The 2-platter form reuses the 3-platter form's middle+top layers exactly (drops only the
    darkest bottom platter) -- matches the reference SVG's own 16px panel, which pairs
    `#1B6FA8` band / `#2E9BD6` cap for its single blue platter (the same colors as the 3-platter
    form's middle platter) with the same `#C2801F` band / `#F2A93B` cap amber platter on top.
    """
    if two_platter:
        return [(DEEP, MID), (BAND_AMBER, AMBER)]
    return [(BAND_DARKEST, DEEP), (DEEP, MID), (BAND_AMBER, AMBER)]


def render_mark(size: int) -> Image.Image:
    """Render the Reclaim lifted-platter mark as a transparent RGBA image, `size` x `size`.

    Reproduces the reference asset set's geometry: each platter is a band (a flat-sided
    rectangle capped by a downward-bulging half-ellipse, giving the disk's visible front rim)
    drawn first, then a cap ellipse (the disk's top face) painted over it -- same paint order as
    the reference SVG's own path-then-ellipse pairs. 3 platters at `size > _TWO_PLATTER_MAX_SIZE`;
    simplified to 2 at or below that, per the explicit size-behavior spec (see module docstring).
    """
    two_platter = size <= _TWO_PLATTER_MAX_SIZE
    layers = _platter_layers(two_platter)
    n = len(layers)

    # Content-space geometry (units of rx = 1), then scaled to fit _CONTENT_FILL_FRACTION of the
    # canvas height -- the limiting dimension, since the stack is taller than it is wide.
    ry_u = _RY_OVER_RX
    band_h_u = ry_u * _BAND_HEIGHT_OVER_RY
    if n == 3:
        total_height_u = (_GAP_STACKED_OVER_RY + _GAP_LIFTED_OVER_RY) * ry_u + 3 * ry_u
    else:
        total_height_u = _GAP_TWO_PLATTER_OVER_RY * ry_u + 3 * ry_u

    target_content_height = size * _CONTENT_FILL_FRACTION
    scale = target_content_height / total_height_u
    rx = scale
    ry = ry_u * scale
    band_h = band_h_u * scale
    cx = size / 2.0

    # cy for each platter, top (amber, index n-1) to bottom (index 0), starting from the topmost
    # point of the stack (amber cap's top edge) sitting at the top margin.
    top_margin = (size - target_content_height) / 2.0
    cy_top = top_margin + ry
    cys = [0.0] * n
    cys[n - 1] = cy_top
    if n == 3:
        cys[1] = cy_top + _GAP_LIFTED_OVER_RY * ry
        cys[0] = cys[1] + _GAP_STACKED_OVER_RY * ry
    else:
        cys[0] = cy_top + _GAP_TWO_PLATTER_OVER_RY * ry

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    for (band_color, cap_color), cy in zip(layers, cys, strict=True):
        band_bottom = cy + band_h
        # Straight sides + flat top of the band (the part that will mostly be hidden under the
        # cap ellipse painted next).
        draw.rectangle([cx - rx, cy, cx + rx, band_bottom], fill=_hex_to_rgb(band_color))
        # Downward-bulging half-ellipse at the band's bottom edge -- the visible disk rim. Drawn
        # as a full ellipse (its top half harmlessly overlaps the rectangle above); centered
        # exactly at band_bottom to match the reference path's `A rx ry ...` arc, whose two
        # endpoints share that y (see module docstring for the geometric derivation).
        draw.ellipse(
            [cx - rx, band_bottom - ry, cx + rx, band_bottom + ry],
            fill=_hex_to_rgb(band_color),
        )
        # Cap: the disk's top face, painted last so it covers the band down to y = cy + ry,
        # leaving only the band's outer wings/rim visible below it.
        draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=_hex_to_rgb(cap_color))

    return canvas


def render_tile(size: int, *, corner_radius_fraction: float = 24.0 / 112.0) -> Image.Image:
    """Render the mark on a dark #0F172A rounded-square tile, `size` x `size`.

    Matches the reference asset set's "installer tile" panel exactly (a 112x112, rx=24 rounded
    square in the reference -- expressed here as a fraction so it scales), used for the Inno
    Setup wizard images since that is the one packaging context the reference explicitly shows on
    dark ground (see module docstring).
    """
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        [0, 0, size - 1, size - 1],
        radius=max(1, round(size * corner_radius_fraction)),
        fill=(*_hex_to_rgb(DARK_GROUND), 255),
    )
    inner_size = round(size * 0.56)
    mark = render_mark(inner_size)
    tile.paste(mark, ((size - inner_size) // 2, (size - inner_size) // 2), mark)
    return tile


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load Segoe UI at `size`, falling back to Pillow's bundled bitmap font if unavailable."""
    candidate = _FONT_DIR / ("segoeuib.ttf" if bold else "segoeui.ttf")
    try:
        return ImageFont.truetype(str(candidate), size)
    except OSError:
        return ImageFont.load_default(size=size)


def _centered_wordmark(
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    """Draw the lowercase "reclaim" wordmark centered on `center` -- "re" in dark-ground ink,
    "claim" in amber, matching the reference asset set's split-color wordmark exactly."""
    part_a = "re"
    bbox_a = draw.textbbox((0, 0), part_a, font=font)
    bbox_full = draw.textbbox((0, 0), "reclaim", font=font)
    width_a = bbox_a[2] - bbox_a[0]
    total_width = bbox_full[2] - bbox_full[0]
    height = bbox_full[3] - bbox_full[1]
    start_x = center[0] - total_width / 2 - bbox_full[0]
    y = center[1] - height / 2 - bbox_full[1]
    draw.text((start_x, y), part_a, font=font, fill=DARK_GROUND)
    draw.text((start_x + width_a, y), "claim", font=font, fill=AMBER)


def _left_anchored_wordmark(
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    """Draw the "reclaim" wordmark left-anchored at `origin` (top-left of its own bounding box)
    -- same split-color scheme as `_centered_wordmark`, for layouts that already position the
    mark + wordmark as a left-to-right pair rather than centering the wordmark independently."""
    part_a = "re"
    bbox_a = draw.textbbox((0, 0), part_a, font=font)
    width_a = bbox_a[2] - bbox_a[0]
    draw.text(origin, part_a, font=font, fill=DARK_GROUND)
    draw.text((origin[0] + width_a, origin[1]), "claim", font=font, fill=AMBER)


def build_ico(path: Path) -> None:
    """Write a multi-resolution .ico (16/24/32/48/64/128/256px) for the packaged exe + installer."""
    sizes = (16, 24, 32, 48, 64, 128, 256)
    renders = {size: render_mark(size) for size in sizes}
    largest = renders[max(sizes)]
    largest.save(
        path,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=[renders[s] for s in sizes if s != max(sizes)],
    )


def build_wizard_small(path: Path) -> None:
    """Write Inno Setup's WizardSmallImageFile -- 55x58, bg background, dark installer tile."""
    width, height = 55, 58
    canvas = Image.new("RGB", (width, height), BG)
    tile_size = 44
    tile = render_tile(tile_size)
    canvas.paste(tile, ((width - tile_size) // 2, (height - tile_size) // 2), tile)
    canvas.save(path, format="BMP")


def build_wizard_large(path: Path) -> None:
    """Write Inno Setup's WizardImageFile -- 164x314, bg background, dark tile + wordmark."""
    width, height = 164, 314
    canvas = Image.new("RGB", (width, height), BG)
    tile_size = 100
    tile = render_tile(tile_size)
    tile_top = 48
    canvas.paste(tile, ((width - tile_size) // 2, tile_top), tile)

    draw = ImageDraw.Draw(canvas)
    wordmark_center = (width // 2, tile_top + tile_size + 36)
    _centered_wordmark(draw, wordmark_center, _font(26, bold=True))
    canvas.save(path, format="BMP")


def build_og_preview(path: Path) -> None:
    """Write the GitHub social-preview image -- 1200x630, mark + wordmark + one-line tagline."""
    width, height = 1200, 630
    canvas = Image.new("RGB", (width, height), BG)
    mark_size = 220
    mark = render_mark(mark_size)
    mark_left = 140
    mark_top = (height - mark_size) // 2 - 40
    canvas.paste(mark, (mark_left, mark_top), mark)

    draw = ImageDraw.Draw(canvas)
    text_left = mark_left + mark_size + 60
    _left_anchored_wordmark(draw, (text_left, mark_top + 10), _font(96, bold=True))
    draw.text(
        (text_left, mark_top + 130),
        "Windows disk cleanup with hard safety gates",
        font=_font(34),
        fill=TEXT_MUTED,
    )
    canvas.save(path, format="PNG")


def build_logo_lockup(path: Path) -> None:
    """Write the README header lockup -- ~600x160, mark + wordmark, bg background."""
    width, height = 600, 160
    canvas = Image.new("RGB", (width, height), BG)
    mark_size = 96
    mark_left = 32
    mark = render_mark(mark_size)
    canvas.paste(mark, (mark_left, (height - mark_size) // 2), mark)

    draw = ImageDraw.Draw(canvas)
    text_left = mark_left + mark_size + 28
    _centered_wordmark(draw, (text_left + 100, height // 2), _font(56, bold=True))
    canvas.save(path, format="PNG")


def main() -> None:
    """Generate every brand asset the packaging pipeline and docs reference."""
    packaging_dir = Path(__file__).parent
    docs_assets_dir = packaging_dir.parent / "docs" / "assets"
    docs_assets_dir.mkdir(parents=True, exist_ok=True)

    build_ico(packaging_dir / "reclaim.ico")
    build_wizard_small(packaging_dir / "wizard_small.bmp")
    build_wizard_large(packaging_dir / "wizard_large.bmp")
    build_og_preview(docs_assets_dir / "og-preview.png")
    build_logo_lockup(docs_assets_dir / "logo-lockup.png")


if __name__ == "__main__":
    main()
