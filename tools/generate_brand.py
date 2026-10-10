"""Draw an original, metadata-free integration icon from geometric primitives."""

from pathlib import Path

from PIL import Image, ImageDraw

folder = Path(__file__).resolve().parents[1] / "custom_components/xiaoai_speaker/brand"
folder.mkdir(exist_ok=True)
for size, name in [(256, "icon.png"), (512, "icon@2x.png")]:
    scale = size / 256
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)

    def coords(values, scale=scale):
        return tuple(round(x * scale) for x in values)

    draw.rounded_rectangle(coords((40, 20, 216, 236)), radius=round(36 * scale), fill="#143c50")
    draw.rounded_rectangle(coords((64, 44, 192, 104)), radius=round(18 * scale), fill="#63d8df")
    draw.line(
        coords((80, 82, 108, 66, 148, 66, 176, 82)),
        fill="#143c50",
        width=round(8 * scale),
        joint="curve",
    )
    draw.ellipse(coords((76, 116, 180, 220)), fill="#0c2532")
    draw.ellipse(coords((98, 138, 158, 198)), fill="#63d8df")
    draw.ellipse(coords((115, 155, 141, 181)), fill="#143c50")
    im.save(folder / name, format="PNG", optimize=True)
