#!/usr/bin/env python3
"""Gera os ícones do app: chat cyberpunk em neon sobre grid escuro.

Saídas em app/src/main/res/mipmap-*:
  - ic_launcher.png / ic_launcher_round.png (ícones legados)
  - ic_launcher_background.png / _foreground.png / _monochrome.png (adaptive)

Uso: python tools/generate_icons.py
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "app" / "src" / "main" / "res"

LOGICAL = 108.0
SS = 4  # superamostragem para anti-aliasing
DENSITIES = {
    "mdpi": 1.0,
    "hdpi": 1.5,
    "xhdpi": 2.0,
    "xxhdpi": 3.0,
    "xxxhdpi": 4.0,
}

CYAN = (0, 229, 255)
MAGENTA = (255, 43, 214)
DARK = (7, 11, 28)
WHITE = (255, 255, 255)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


class Image:
    """Buffer RGBA pré-multiplicado (necessário para redimensionamento limpo)."""

    def __init__(self, width: int, height: int) -> None:
        self.w = width
        self.h = height
        self.px = bytearray(width * height * 4)

    def copy(self) -> "Image":
        other = Image(self.w, self.h)
        other.px = bytearray(self.px)
        return other

    def set(self, x: int, y: int, color: tuple[int, int, int], alpha: float) -> None:
        if x < 0 or y < 0 or x >= self.w or y >= self.h:
            return
        sa = clamp(alpha)
        if sa <= 0.0:
            return
        idx = (y * self.w + x) * 4
        inv = 1.0 - sa
        r, g, b = color
        self.px[idx] = min(255, int(r * sa + self.px[idx] * inv + 0.5))
        self.px[idx + 1] = min(255, int(g * sa + self.px[idx + 1] * inv + 0.5))
        self.px[idx + 2] = min(255, int(b * sa + self.px[idx + 2] * inv + 0.5))
        self.px[idx + 3] = min(255, int(255 * sa + self.px[idx + 3] * inv + 0.5))

    def add_light(self, x: int, y: int, color: tuple[int, int, int], amount: float) -> None:
        idx = (y * self.w + x) * 4
        r, g, b = color
        self.px[idx] = min(255, int(self.px[idx] + r * amount))
        self.px[idx + 1] = min(255, int(self.px[idx + 1] + g * amount))
        self.px[idx + 2] = min(255, int(self.px[idx + 2] + b * amount))

    def composite(self, top: "Image") -> None:
        for idx in range(0, len(self.px), 4):
            sa = top.px[idx + 3] / 255.0
            if sa <= 0.0:
                continue
            inv = 1.0 - sa
            for channel in range(3):
                self.px[idx + channel] = min(
                    255,
                    int(top.px[idx + channel] + self.px[idx + channel] * inv + 0.5),
                )
            self.px[idx + 3] = min(
                255, int(top.px[idx + 3] + self.px[idx + 3] * inv + 0.5)
            )

    def save_png(self, path: Path, target_w: int, target_h: int) -> None:
        sx = self.w / target_w
        sy = self.h / target_h
        raw = bytearray()
        for oy in range(target_h):
            raw.append(0)  # filtro PNG "None"
            y0 = int(oy * sy)
            y1 = max(y0 + 1, min(self.h, math.ceil((oy + 1) * sy)))
            for ox in range(target_w):
                x0 = int(ox * sx)
                x1 = max(x0 + 1, min(self.w, math.ceil((ox + 1) * sx)))
                sr = sg = sb = sa = 0
                for yy in range(y0, y1):
                    base = yy * self.w * 4
                    for xx in range(x0, x1):
                        idx = base + xx * 4
                        sr += self.px[idx]
                        sg += self.px[idx + 1]
                        sb += self.px[idx + 2]
                        sa += self.px[idx + 3]
                count = (y1 - y0) * (x1 - x0)
                alpha = sa / count
                if sa > 0.5:
                    r = min(255, int(sr / sa * 255 + 0.5))
                    g = min(255, int(sg / sa * 255 + 0.5))
                    b = min(255, int(sb / sa * 255 + 0.5))
                else:
                    r = g = b = 0
                raw.extend((r, g, b, int(alpha + 0.5)))

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        header = struct.pack(">IIBBBBB", target_w, target_h, 8, 6, 0, 0, 0)
        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b"")
        )
        path.write_bytes(png)


def rounded_rect(
    img: Image,
    s: float,
    x: float,
    y: float,
    w: float,
    h: float,
    radius: float,
    fill: tuple[int, int, int] | None = None,
    stroke: float | None = None,
    color: tuple[int, int, int] | None = None,
    alpha: float = 1.0,
) -> None:
    stroke_color = color or fill or CYAN
    cx, cy = x + w / 2.0, y + h / 2.0
    hw, hh = w / 2.0, h / 2.0
    inner_w, inner_h = hw - radius, hh - radius
    x0 = max(0, int((x - 8) * s))
    y0 = max(0, int((y - 8) * s))
    x1 = min(img.w, math.ceil((x + w + 8) * s))
    y1 = min(img.h, math.ceil((y + h + 8) * s))
    for yy in range(y0, y1):
        py = (yy + 0.5) / s
        for xx in range(x0, x1):
            px = (xx + 0.5) / s
            qx = abs(px - cx) - inner_w
            qy = abs(py - cy) - inner_h
            distance = (
                math.hypot(max(qx, 0.0), max(qy, 0.0))
                + min(max(qx, qy), 0.0)
                - radius
            )
            if fill is not None and distance <= 0.0:
                img.set(xx, yy, fill, alpha)
            if stroke is not None and abs(distance) <= stroke / 2.0:
                img.set(xx, yy, stroke_color, alpha)


def circle(
    img: Image,
    s: float,
    cx: float,
    cy: float,
    radius: float,
    fill: tuple[int, int, int] | None = None,
    stroke: float | None = None,
    alpha: float = 1.0,
) -> None:
    x0 = max(0, int((cx - radius - 3) * s))
    y0 = max(0, int((cy - radius - 3) * s))
    x1 = min(img.w, math.ceil((cx + radius + 3) * s))
    y1 = min(img.h, math.ceil((cy + radius + 3) * s))
    for yy in range(y0, y1):
        py = (yy + 0.5) / s
        for xx in range(x0, x1):
            px = (xx + 0.5) / s
            distance = math.hypot(px - cx, py - cy) - radius
            if fill is not None and distance <= 0.0:
                img.set(xx, yy, fill, alpha)
            if stroke is not None and abs(distance) <= stroke / 2.0:
                img.set(xx, yy, fill or CYAN, alpha)


def line(
    img: Image,
    s: float,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    width: float,
    color: tuple[int, int, int],
    alpha: float = 1.0,
) -> None:
    min_x = max(0, int((min(x0, x1) - width) * s))
    max_x = min(img.w, math.ceil((max(x0, x1) + width) * s))
    min_y = max(0, int((min(y0, y1) - width) * s))
    max_y = min(img.h, math.ceil((max(y0, y1) + width) * s))
    dx, dy = x1 - x0, y1 - y0
    length_sq = dx * dx + dy * dy
    half = width / 2.0
    for yy in range(min_y, max_y):
        py = (yy + 0.5) / s
        for xx in range(min_x, max_x):
            px = (xx + 0.5) / s
            t = 0.0 if length_sq == 0 else clamp(((px - x0) * dx + (py - y0) * dy) / length_sq)
            qx, qy = x0 + t * dx - px, y0 + t * dy - py
            if math.hypot(qx, qy) <= half:
                img.set(xx, yy, color, alpha)


def polygon(
    img: Image,
    s: float,
    points: list[tuple[float, float]],
    color: tuple[int, int, int],
    alpha: float = 1.0,
) -> None:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0 = max(0, int((min(xs) - 1) * s))
    y0 = max(0, int((min(ys) - 1) * s))
    x1 = min(img.w, math.ceil((max(xs) + 1) * s))
    y1 = min(img.h, math.ceil((max(ys) + 1) * s))
    for yy in range(y0, y1):
        py = (yy + 0.5) / s
        for xx in range(x0, x1):
            px = (xx + 0.5) / s
            inside = False
            j = len(points) - 1
            for i in range(len(points)):
                xi, yi = points[i]
                xj, yj = points[j]
                if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                    inside = not inside
                j = i
            if inside:
                img.set(xx, yy, color, alpha)


def draw_background(img: Image, s: float) -> None:
    for yy in range(img.h):
        v = (yy + 0.5) / img.h
        base = (
            lerp(11, 21, v),
            lerp(11, 15, v),
            lerp(18, 52, v),
        )
        for xx in range(img.w):
            u = (xx + 0.5) / img.w
            cyan_glow = max(0.0, 1.0 - math.hypot(u - 0.84, v - 0.16) / 0.55) ** 2 * 0.55
            magenta_glow = max(0.0, 1.0 - math.hypot(u - 0.14, v - 0.86) / 0.60) ** 2 * 0.50
            r = min(255, int(base[0] + MAGENTA[0] * magenta_glow * 0.35))
            g = min(
                255,
                int(base[1] + CYAN[1] * cyan_glow * 0.35 + MAGENTA[1] * magenta_glow * 0.12),
            )
            b = min(
                255,
                int(base[2] + CYAN[2] * cyan_glow * 0.45 + MAGENTA[2] * magenta_glow * 0.30),
            )
            img.set(xx, yy, (r, g, b), 1.0)

    # Grade sutil de terminal.
    for i in range(0, 109, 12):
        alpha = 0.18 if i % 36 == 0 else 0.09
        line(img, s, i, 0, i, 108, 0.7, CYAN, alpha)
        line(img, s, 0, i, 108, i, 0.7, CYAN, alpha)

    # Traços de circuito nas bordas, fora da área segura do foreground.
    traces = [
        ([(0, 24), (20, 24), (28, 16), (52, 16)], CYAN, 0.32, (52, 16)),
        ([(108, 84), (88, 84), (80, 92), (56, 92)], MAGENTA, 0.32, (56, 92)),
        ([(0, 72), (10, 72), (16, 78), (16, 108)], MAGENTA, 0.24, (10, 72)),
        ([(108, 40), (98, 40), (92, 34), (92, 0)], CYAN, 0.24, (98, 40)),
    ]
    for points, color, alpha, node in traces:
        for a, b in zip(points, points[1:]):
            line(img, s, a[0], a[1], b[0], b[1], 1.3, color, alpha)
        circle(img, s, node[0], node[1], 2.0, fill=color, alpha=0.75)


def draw_foreground(img: Image, s: float, monochrome: bool = False) -> None:
    main = WHITE if monochrome else CYAN
    accent = WHITE if monochrome else MAGENTA

    if not monochrome:
        # Deslocamento magenta ("glitch") atrás do balão ciano.
        rounded_rect(img, s, 26, 26, 60, 38, 12, stroke=4.0, color=accent, alpha=0.85)
        polygon(img, s, [(40, 62), (36, 76), (54, 66)], accent, 0.85)
        # Interior escuro para manter o prompt legível sobre o background.
        rounded_rect(img, s, 22, 30, 60, 40, 12, fill=DARK, alpha=0.92)

    # Cauda do balão e contorno com halo neon.
    polygon(img, s, [(36, 66), (32, 80), (50, 70)], main, 1.0)
    rounded_rect(img, s, 22, 30, 60, 40, 12, stroke=12.0, color=main, alpha=0.12)
    rounded_rect(img, s, 22, 30, 60, 40, 12, stroke=4.5, color=main, alpha=1.0)
    line(img, s, 34, 30, 70, 30, 1.5, WHITE, 0.55 if not monochrome else 0.75)

    # Prompt ">_" dentro do balão.
    line(img, s, 38, 41, 47, 50, 4.2, accent, 1.0)
    line(img, s, 47, 50, 38, 59, 4.2, accent, 1.0)
    line(img, s, 54, 58, 66, 58, 4.0, main, 1.0)

    # Barras de varredura digitais.
    line(img, s, 22, 36, 46, 36, 2.0, accent, 0.85)
    line(img, s, 64, 64, 84, 64, 2.0, main, 0.85)

    circle(img, s, 22, 50, 1.8, fill=main, alpha=1.0)
    circle(img, s, 82, 50, 1.8, fill=main, alpha=1.0)


def mask_circle(img: Image, s: float, cx: float, cy: float, radius: float) -> None:
    for yy in range(img.h):
        py = (yy + 0.5) / s
        for xx in range(img.w):
            px = (xx + 0.5) / s
            coverage = clamp((radius - math.hypot(px - cx, py - cy)) * s + 0.5)
            if coverage < 1.0:
                idx = (yy * img.w + xx) * 4
                for channel in range(4):
                    img.px[idx + channel] = int(img.px[idx + channel] * coverage + 0.5)


def generate() -> None:
    for density, factor in DENSITIES.items():
        out_dir = RES / f"mipmap-{density}"
        out_dir.mkdir(parents=True, exist_ok=True)
        scale = factor * SS
        size = int(LOGICAL * factor * SS)

        background = Image(size, size)
        draw_background(background, scale)
        foreground = Image(size, size)
        draw_foreground(foreground, scale)
        monochrome = Image(size, size)
        draw_foreground(monochrome, scale, monochrome=True)

        legacy = background.copy()
        legacy.composite(foreground)
        legacy.save_png(out_dir / "ic_launcher.png", int(48 * factor), int(48 * factor))

        round_icon = legacy.copy()
        circle(round_icon, scale, 54, 54, 50.5, fill=CYAN, stroke=1.2, alpha=0.75)
        mask_circle(round_icon, scale, 54, 54, 52)
        round_icon.save_png(
            out_dir / "ic_launcher_round.png", int(48 * factor), int(48 * factor)
        )

        layer_size = int(LOGICAL * factor)
        background.save_png(
            out_dir / "ic_launcher_background.png", layer_size, layer_size
        )
        foreground.save_png(
            out_dir / "ic_launcher_foreground.png", layer_size, layer_size
        )
        monochrome.save_png(
            out_dir / "ic_launcher_monochrome.png", layer_size, layer_size
        )

    adaptive = RES / "mipmap-anydpi-v26"
    adaptive.mkdir(parents=True, exist_ok=True)
    xml = """<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@mipmap/ic_launcher_background" />
    <foreground android:drawable="@mipmap/ic_launcher_foreground" />
    <monochrome android:drawable="@mipmap/ic_launcher_monochrome" />
</adaptive-icon>
"""
    (adaptive / "ic_launcher.xml").write_text(xml, encoding="utf-8")
    (adaptive / "ic_launcher_round.xml").write_text(xml, encoding="utf-8")
    print("Ícones gerados em", RES)


if __name__ == "__main__":
    generate()
