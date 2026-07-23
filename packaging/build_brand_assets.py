"""Render Reclaim's brand assets (Windows .ico, Inno Setup wizard bitmaps, OG preview, README
logo lockup) from the same flat-rectangle geometry as `src/reclaim/api/static/logo.svg`.

Deliberately redraws the mark with Pillow's `ImageDraw` primitives instead of adding an
SVG-rasterizer dependency (cairosvg/resvg) — the mark is three rectangles plus a rounded-corner
clip, cheap to reproduce exactly, so a new dependency would buy nothing. If the mark ever grows
curves or gradients, that trade-off should be revisited.

Run with: `uv run python packaging/build_brand_assets.py`
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Mirrors logo.svg / favicon.svg's light-mode literals, which in turn mirror tokens.css's
# --rc-cat-dev-artifacts / --rc-brand / --rc-bg / --rc-text / --rc-text-muted (see those files'
# comments) — kept as literals here too since these are standalone raster assets, not part of
# the app's CSS cascade.
CLAY = "#c1512b"
GREEN = "#2f6b52"
SAND = "#faf6f0"
TEXT = "#2a2420"
TEXT_MUTED = "#6b5d52"

# The mark's own 32x32 viewBox from logo.svg, expressed as fractions so it can be re-rendered at
# any pixel size without redrawing the geometry by hand each time.
_VIEWBOX = 32.0
_CORNER_RADIUS_FRACTION = 7.0 / _VIEWBOX
_HOLE_INSET_FRACTION = 11.0 / _VIEWBOX
_LIFT_BOX_FRACTION = (15.0 / _VIEWBOX, 1.0 / _VIEWBOX, 29.0 / _VIEWBOX, 13.0 / _VIEWBOX)
_LIFT_STROKE_FRACTION = 2.0 / _VIEWBOX

# Windows ships Segoe UI at this path on every supported install — matches tokens.css's
# --rc-font-sans stack, whose first entry is "Segoe UI". This script only ever runs on the
# Windows dev machine that builds the installer, so no cross-platform fallback path is needed
# beyond the PIL bitmap font safety net in `_font()`.
_FONT_DIR = Path("C:/Windows/Fonts")


def render_mark(size: int) -> Image.Image:
    """Render the Reclaim mark (clay square, green reveal, lifted clay block) as an RGBA image.

    Reproduces `logo.svg`'s geometry at an arbitrary pixel size: a clay square clipped to a
    rounded rect, a green rectangle cut into its bottom-right corner (the already-cleared
    space), and a smaller clay square floating top-right with a sand-colored outline (the piece
    still mid-lift).
    """
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1],
        radius=max(1, round(size * _CORNER_RADIUS_FRACTION)),
        fill=255,
    )

    content = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(content)
    draw.rectangle([0, 0, size, size], fill=CLAY)

    hole = round(size * _HOLE_INSET_FRACTION)
    draw.rectangle([hole, hole, size, size], fill=GREEN)

    lift_x0, lift_y0, lift_x1, lift_y1 = (round(size * f) for f in _LIFT_BOX_FRACTION)
    stroke_width = max(1, round(size * _LIFT_STROKE_FRACTION))
    draw.rectangle(
        [lift_x0, lift_y0, lift_x1, lift_y1], fill=CLAY, outline=SAND, width=stroke_width
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
    """Write Inno Setup's WizardSmallImageFile — 55x58, sand background, centered mark."""
    width, height = 55, 58
    canvas = Image.new("RGB", (width, height), SAND)
    mark = render_mark(40)
    canvas.paste(mark, ((width - 40) // 2, (height - 40) // 2), mark)
    canvas.save(path, format="BMP")


def build_wizard_large(path: Path) -> None:
    """Write Inno Setup's WizardImageFile — 164x314, sand background, mark + wordmark."""
    width, height = 164, 314
    canvas = Image.new("RGB", (width, height), SAND)
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
    canvas = Image.new("RGB", (width, height), SAND)
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
    """Write the README header lockup — ~600x160, mark + wordmark, sand background."""
    width, height = 600, 160
    canvas = Image.new("RGB", (width, height), SAND)
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
