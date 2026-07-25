"""Render Reclaim's brand assets (Windows .ico, Inno Setup wizard bitmaps, OG preview, README
logo lockup) from the same flat-rectangle geometry as `src/reclaim/api/static/logo.svg`.

Deliberately redraws the mark with Pillow's `ImageDraw` primitives instead of adding an
SVG-rasterizer dependency (cairosvg/resvg) — the mark is three rectangles plus a rounded-corner
clip, cheap to reproduce exactly, so a new dependency would buy nothing.

v3 (WS-B visual identity refresh, GG's Direction A): indigo (occupied ground) revealing mint
(reclaimed space) on a deep cool near-black scale — see tokens.css's file header for the full
palette rationale and the WCAG contrast table. Size-adaptive rendering: a top-lit vertical
gradient on the two large regions at 48px and above; flat, high-contrast fills below that.
Proven empirically (WS-B preview PR #20) that a gradient's inset highlight on the small "lift"
block degenerates to nothing once rasterized at 16px, and that 2026 icon-design convention
backs flat/high-contrast at genuinely small sizes regardless — real pixel-grid renders, not
assumed. Built with `Image.linear_gradient` + `ImageOps.colorize` (pure PIL, no new dependency
here either).

Run with: `uv run python packaging/build_brand_assets.py`
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

# Mirrors tokens.css's light-mode --rc-mark-*/--rc-bg/--rc-text/--rc-text-muted literals (see
# that file's header comment) — kept as literals here too since these are standalone raster
# assets, not part of the app's CSS cascade.
GROUND_LIGHT = "#6366f1"  # indigo (occupied ground) -- lighter gradient stop
GROUND_DARK = "#4338ca"  # indigo -- darker gradient stop
RECLAIMED_LIGHT = "#34d399"  # mint (reclaimed space) -- lighter gradient stop
RECLAIMED_DARK = "#047857"  # mint -- darker gradient stop
BG = "#f6f7fb"
TEXT = "#13151d"
TEXT_MUTED = "#585e72"

# Below this pixel size, gradients are skipped in favor of flat fills -- see the module
# docstring for why (an inset highlight on the ~7px "lift" block at 16px degenerates to nothing
# once rasterized, so flat/high-contrast reads better there than a muddy gradient attempt).
_GRADIENT_MIN_SIZE = 48

# The mark's own 32x32 viewBox from logo.svg, expressed as fractions so it can be re-rendered at
# any pixel size without redrawing the geometry by hand each time.
_VIEWBOX = 32.0
_CORNER_RADIUS_FRACTION = 7.0 / _VIEWBOX
_HOLE_INSET_FRACTION = 11.0 / _VIEWBOX
_LIFT_BOX_FRACTION = (15.0 / _VIEWBOX, 1.0 / _VIEWBOX, 29.0 / _VIEWBOX, 13.0 / _VIEWBOX)
_LIFT_STROKE_FRACTION = 2.0 / _VIEWBOX

# Windows ships Segoe UI at this path on every supported install — matches tokens.css's
# --rc-font-sans stack. This script only ever runs on the Windows dev machine that builds the
# installer, so no cross-platform fallback path is needed beyond the PIL bitmap font safety net
# in `_font()`.
_FONT_DIR = Path("C:/Windows/Fonts")


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return (r, g, b)


def _vertical_gradient(size: int, light: str, dark: str) -> Image.Image:
    """Top-lit vertical gradient (light source directly above) -- reads cleanly at any size,
    unlike a diagonal gradient fighting the mark's own diagonal split. Built from
    `Image.linear_gradient` + `ImageOps.colorize`, pure PIL, no new dependency."""
    base = Image.linear_gradient("L").resize((size, size))
    return ImageOps.colorize(base, black=_hex_to_rgb(dark), white=_hex_to_rgb(light)).convert("RGB")


def render_mark(size: int) -> Image.Image:
    """Render the Reclaim mark (indigo ground, mint reveal, indigo lift block) as an RGBA image.

    Reproduces `logo.svg`'s geometry at an arbitrary pixel size: an indigo square clipped to a
    rounded rect, a mint rectangle cut into its bottom-right corner (the already-cleared space),
    and a smaller indigo square floating top-right with a surface-colored outline (the piece
    still mid-lift). Gradient depth at `size >= _GRADIENT_MIN_SIZE`, flat fills below that.
    """
    gradient = size >= _GRADIENT_MIN_SIZE

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1],
        radius=max(1, round(size * _CORNER_RADIUS_FRACTION)),
        fill=255,
    )

    content = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if gradient:
        ground_fill = _vertical_gradient(size, GROUND_LIGHT, GROUND_DARK)
        reclaimed_fill = _vertical_gradient(size, RECLAIMED_LIGHT, RECLAIMED_DARK)
    else:
        ground_fill = Image.new("RGB", (size, size), _hex_to_rgb(GROUND_LIGHT))
        reclaimed_fill = Image.new("RGB", (size, size), _hex_to_rgb(RECLAIMED_LIGHT))
    content.paste(ground_fill, (0, 0))

    hole = round(size * _HOLE_INSET_FRACTION)
    content.paste(reclaimed_fill.crop((hole, hole, size, size)), (hole, hole))

    # Lift block: a SOLID fill (the gradient's lightest stop), not an inset highlight crop -- at
    # 16px the lift box is ~7px net of its own outline, too small for a nested highlight to
    # survive resampling. One flat tone + one outline is legible at every size; a second
    # internal gradient inside a 7px box is not (see module docstring).
    lift_x0, lift_y0, lift_x1, lift_y1 = (round(size * f) for f in _LIFT_BOX_FRACTION)
    stroke_width = max(1, round(size * _LIFT_STROKE_FRACTION))
    draw = ImageDraw.Draw(content)
    draw.rectangle(
        [lift_x0, lift_y0, lift_x1, lift_y1],
        fill=GROUND_LIGHT,
        outline=BG,
        width=stroke_width,
    )

    mark = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mark.paste(content, (0, 0), mask)
    return mark


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load Segoe UI at `size`, falling back to Pillow's bundled bitmap font if unavailable."""
    candidate = _FONT_DIR / ("segoeuib.ttf" if bold else "segoeui.ttf")
    try:
        return ImageFont.truetype(str(candidate), size)
    except OSError:
        return ImageFont.load_default(size=size)


def _centered_text(
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: str,
) -> None:
    """Draw `text` centered on `center`, using the font's own measured bounding box."""
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    width, height = right - left, bottom - top
    origin = (center[0] - width / 2 - left, center[1] - height / 2 - top)
    draw.text(origin, text, font=font, fill=fill)


def build_ico(path: Path) -> None:
    """Write a multi-resolution .ico (16/32/48/256px) for the packaged exe + installer."""
    sizes = (16, 32, 48, 256)
    renders = {size: render_mark(size) for size in sizes}
    largest = renders[max(sizes)]
    largest.save(
        path,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=[renders[s] for s in sizes if s != max(sizes)],
    )


def build_wizard_small(path: Path) -> None:
    """Write Inno Setup's WizardSmallImageFile — 55x58, bg background, centered mark."""
    width, height = 55, 58
    canvas = Image.new("RGB", (width, height), BG)
    mark = render_mark(40)
    canvas.paste(mark, ((width - 40) // 2, (height - 40) // 2), mark)
    canvas.save(path, format="BMP")


def build_wizard_large(path: Path) -> None:
    """Write Inno Setup's WizardImageFile — 164x314, bg background, mark + wordmark."""
    width, height = 164, 314
    canvas = Image.new("RGB", (width, height), BG)
    mark_size = 96
    mark = render_mark(mark_size)
    mark_top = 48
    canvas.paste(mark, ((width - mark_size) // 2, mark_top), mark)

    draw = ImageDraw.Draw(canvas)
    wordmark_center = (width // 2, mark_top + mark_size + 36)
    _centered_text(draw, wordmark_center, "Reclaim", _font(26, bold=True), TEXT)
    canvas.save(path, format="BMP")


def build_og_preview(path: Path) -> None:
    """Write the GitHub social-preview image — 1200x630, mark + wordmark + one-line tagline."""
    width, height = 1200, 630
    canvas = Image.new("RGB", (width, height), BG)
    mark_size = 220
    mark = render_mark(mark_size)
    mark_left = 140
    mark_top = (height - mark_size) // 2 - 40
    canvas.paste(mark, (mark_left, mark_top), mark)

    draw = ImageDraw.Draw(canvas)
    text_left = mark_left + mark_size + 60
    draw.text((text_left, mark_top + 10), "Reclaim", font=_font(96, bold=True), fill=TEXT)
    draw.text(
        (text_left, mark_top + 130),
        "Windows disk cleanup with hard safety gates",
        font=_font(34),
        fill=TEXT_MUTED,
    )
    canvas.save(path, format="PNG")


def build_logo_lockup(path: Path) -> None:
    """Write the README header lockup — ~600x160, mark + wordmark, bg background."""
    width, height = 600, 160
    canvas = Image.new("RGB", (width, height), BG)
    mark_size = 96
    mark_left = 32
    mark = render_mark(mark_size)
    canvas.paste(mark, (mark_left, (height - mark_size) // 2), mark)

    draw = ImageDraw.Draw(canvas)
    text_left = mark_left + mark_size + 28
    _centered_text(draw, (text_left + 100, height // 2), "Reclaim", _font(56, bold=True), TEXT)
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
