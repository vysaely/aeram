#!/usr/bin/env python3
"""
Generates minimal achromatic 'ae' icon for AERAM.
Produces:
- assets/aeram.svg (pure standalone vector SVG)
- assets/roblox-account-manager.svg (drop-in compatibility)
- assets/aeram.png (high-resolution PNG)
- assets/aeram.ico (multi-resolution Windows ICO: 16, 24, 32, 48, 64, 128, 256)
"""

import struct
from pathlib import Path
from PyQt6 import QtGui, QtCore, QtSvg

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_icon_assets():
    app = QtGui.QGuiApplication.instance()
    if app is None:
        app = QtGui.QGuiApplication([])

    # 1. Generate precision vector path for 'æ'
    font = QtGui.QFont("Segoe UI Variable Display", 260, QtGui.QFont.Weight.DemiBold)
    path = QtGui.QPainterPath()
    path.addText(QtCore.QPointF(0, 0), font, "æ")

    # Shift path so its visual bounding center is exactly at (256.0, 256.0)
    bounds = path.boundingRect()
    dx = 256.0 - bounds.center().x()
    dy = 256.0 - bounds.center().y()
    path.translate(dx, dy)

    # Serialize QPainterPath to standard SVG path syntax
    d_parts = []
    i = 0
    while i < path.elementCount():
        el = path.elementAt(i)
        if el.isMoveTo():
            d_parts.append(f"M {el.x:.2f},{el.y:.2f}")
            i += 1
        elif el.isLineTo():
            d_parts.append(f"L {el.x:.2f},{el.y:.2f}")
            i += 1
        elif el.isCurveTo():
            cp1 = el
            cp2 = path.elementAt(i + 1)
            ep = path.elementAt(i + 2)
            d_parts.append(f"C {cp1.x:.2f},{cp1.y:.2f} {cp2.x:.2f},{cp2.y:.2f} {ep.x:.2f},{ep.y:.2f}")
            i += 3
        else:
            i += 1

    svg_d = " ".join(d_parts)

    svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <!-- Deep obsidian matte squircle gradient -->
    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#161619"/>
      <stop offset="50%" stop-color="#0b0b0d"/>
      <stop offset="100%" stop-color="#030304"/>
    </linearGradient>

    <!-- Subtle graphite border highlight -->
    <linearGradient id="borderGrad" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#38383e"/>
      <stop offset="100%" stop-color="#18181b"/>
    </linearGradient>

    <!-- Achromatic matte titanium gray glyph gradient -->
    <linearGradient id="glyphGrad" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#d4d4d8"/>
      <stop offset="50%" stop-color="#b4b4bb"/>
      <stop offset="100%" stop-color="#8e8e93"/>
    </linearGradient>

    <!-- Soft ambient inner glow -->
    <radialGradient id="centerGlow" cx="50%" cy="46%" r="60%">
      <stop offset="0%" stop-color="#27272a" stop-opacity="0.35"/>
      <stop offset="100%" stop-color="#000000" stop-opacity="0"/>
    </radialGradient>
  </defs>

  <!-- Background Squircle -->
  <rect x="28" y="28" width="456" height="456" rx="116" fill="url(#bgGrad)" stroke="url(#borderGrad)" stroke-width="6"/>
  <rect x="28" y="28" width="456" height="456" rx="116" fill="url(#centerGlow)"/>

  <!-- Minimal Achromatic 'æ' Symbol -->
  <path d="{svg_d}" fill="url(#glyphGrad)" fill-rule="evenodd"/>
</svg>"""

    assets_dir = PROJECT_ROOT / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    # Save SVG vector files
    (assets_dir / "aeram.svg").write_text(svg_content, encoding="utf-8")
    (assets_dir / "roblox-account-manager.svg").write_text(svg_content, encoding="utf-8")

    # Render multi-resolution images from SVG renderer
    renderer = QtSvg.QSvgRenderer(str(assets_dir / "aeram.svg"))
    if not renderer.isValid():
        raise RuntimeError("Failed to load generated SVG")

    sizes = [16, 24, 32, 48, 64, 128, 256]
    images_data = []

    for s in sizes:
        img = QtGui.QImage(s, s, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(img)
        renderer.render(painter)
        painter.end()

        ba = QtCore.QByteArray()
        buf = QtCore.QBuffer(ba)
        buf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        images_data.append((s, bytes(ba)))

    # Assemble Windows ICO
    header = struct.pack("<HHH", 0, 1, len(images_data))
    offset = 6 + 16 * len(images_data)
    entries = []
    for s, data in images_data:
        w = 0 if s == 256 else s
        h = 0 if s == 256 else s
        entries.append(struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset))
        offset += len(data)

    ico_path = assets_dir / "aeram.ico"
    with open(ico_path, "wb") as f:
        f.write(header)
        for e in entries:
            f.write(e)
        for _, data in images_data:
            f.write(data)

    # Save 512x512 and 256x256 PNGs
    img_512 = QtGui.QImage(512, 512, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    img_512.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(img_512)
    renderer.render(p)
    p.end()
    img_512.save(str(assets_dir / "aeram.png"), "PNG")

    print("[OK] Successfully generated minimal achromatic 'ae' icon assets:")
    print(f"  • {assets_dir / 'aeram.svg'}")
    print(f"  • {assets_dir / 'aeram.ico'}")
    print(f"  • {assets_dir / 'aeram.png'}")


if __name__ == "__main__":
    build_icon_assets()
