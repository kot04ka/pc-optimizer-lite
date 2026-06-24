"""Generate a small ICO file for PC Optimizer Lite."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    output = root / "assets" / "pc_optimizer_lite.ico"
    output.parent.mkdir(parents=True, exist_ok=True)

    sizes = [16, 24, 32, 48, 64, 128, 256]
    images: list[Image.Image] = []
    for size in sizes:
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        scale = size / 64
        accent = (96, 165, 250, 255)
        good = (52, 211, 153, 255)
        white = (255, 255, 255, 255)
        shadow = (15, 23, 42, 80)
        radius = max(3, int(12 * scale))
        draw.rounded_rectangle(
            [int(6 * scale), int(6 * scale), int(58 * scale), int(58 * scale)],
            radius=radius,
            fill=accent,
        )
        draw.rounded_rectangle(
            [int(10 * scale), int(12 * scale), int(54 * scale), int(50 * scale)],
            radius=max(2, int(7 * scale)),
            outline=white,
            width=max(1, int(4 * scale)),
        )
        draw.line(
            [
                (int(18 * scale), int(33 * scale)),
                (int(26 * scale), int(41 * scale)),
                (int(43 * scale), int(23 * scale)),
            ],
            fill=white,
            width=max(2, int(5 * scale)),
            joint="curve",
        )
        draw.ellipse(
            [int(43 * scale), int(43 * scale), int(59 * scale), int(59 * scale)],
            fill=shadow,
        )
        draw.ellipse(
            [int(41 * scale), int(41 * scale), int(57 * scale), int(57 * scale)],
            fill=good,
            outline=white,
            width=max(1, int(3 * scale)),
        )
        images.append(image)

    images[-1].save(output, sizes=[(size, size) for size in sizes])
    print(output)


if __name__ == "__main__":
    main()
