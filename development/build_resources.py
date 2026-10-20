"""Generate simple authored Qt icons; no original or vendor artwork is used."""
import argparse
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile

# Keep the aliases used by the recovered GUI and both application launchers.
ALIASES = ('window.png', 'alert.png', 'bug.png', 'settings.png', 'folder.png',
           'drive.png', 'local.png', 'signin.png', 'internet.png', 'sponsor.png',
           'synced.png', 'refresh.png', 'options.png', 'loading.gif', 'loading2.gif',
           'plus.png', 'minus.png', 'revise.png', 'mismatch.png', 'nosync.png')


def png(name):
    # QImage/QPainter need no GUI application, fonts, display or SVG plug-in.
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF

    image = QImage(72, 72, QImage.Format_RGBA8888)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.scale(3, 3)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor('#243244'))
    painter.drawRoundedRect(QRectF(1, 1, 22, 22), 5, 5)
    painter.setBrush(Qt.NoBrush)
    color = {'folder.png': '#fbbf24', 'alert.png': '#fbbf24',
             'mismatch.png': '#fbbf24', 'plus.png': '#4ade80',
             'synced.png': '#4ade80', 'minus.png': '#fb7185',
             'nosync.png': '#cbd5e1'}.get(name, '#e2e8f0')
    pen = QPen(QColor(color), 1.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    painter.setPen(pen)

    def line(*points):
        painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in points]))

    def circle(x, y, radius):
        painter.drawEllipse(QPointF(x, y), radius, radius)

    if name in ('window.png', 'sponsor.png'):
        line((12, 4), (19, 8), (19, 16), (12, 20), (5, 16), (5, 8), (12, 4))
        line((5, 8), (12, 12), (19, 8))
        line((12, 12), (12, 20))
    elif name == 'settings.png':
        circle(12, 12, 5.2)
        circle(12, 12, 2)
        for index in range(8):
            angle = index * math.pi / 4
            line((12 + 5.5 * math.cos(angle), 12 + 5.5 * math.sin(angle)),
                 (12 + 8 * math.cos(angle), 12 + 8 * math.sin(angle)))
    elif name == 'folder.png':
        line((4, 7), (10, 7), (12, 9), (20, 9), (20, 18), (4, 18), (4, 7))
        line((4, 11), (20, 11))
    elif name == 'bug.png':
        painter.drawRoundedRect(QRectF(8, 8, 8, 10), 4, 4)
        circle(12, 6, 2)
        line((12, 9), (12, 17))
        for y in (9, 13, 17):
            line((5, y - 1), (8, y))
            line((16, y), (19, y - 1))
    elif name == 'drive.png':
        path = QPainterPath(QPointF(7, 18))
        path.cubicTo(1, 18, 2, 10, 7, 10)
        path.cubicTo(7, 3, 17, 3, 18, 10)
        path.cubicTo(23, 10, 23, 18, 18, 18)
        path.lineTo(7, 18)
        painter.drawPath(path)
    elif name == 'local.png':
        painter.drawRoundedRect(QRectF(5, 5, 14, 11), 1, 1)
        line((3, 19), (21, 19))
        line((10, 16), (10, 19))
        line((14, 16), (14, 19))
    elif name == 'signin.png':
        circle(8, 8, 3)
        painter.drawArc(QRectF(3, 13, 10, 10), 0, 180 * 16)
        line((14, 12), (21, 12))
        line((18, 9), (21, 12), (18, 15))
    elif name == 'internet.png':
        circle(12, 12, 8)
        painter.drawEllipse(QRectF(8, 4, 8, 16))
        line((4, 12), (20, 12))
    elif name == 'synced.png':
        line((6, 12), (10, 16), (18, 7))
    elif name in ('refresh.png', 'revise.png'):
        painter.drawArc(QRectF(5, 5, 14, 14), 45 * 16, 285 * 16)
        line((14, 5), (19, 5), (19, 10))
    elif name == 'options.png':
        for x, y in ((6, 9), (12, 15), (18, 8)):
            line((x, 5), (x, 19))
            painter.setBrush(QColor('#243244'))
            circle(x, y, 2)
            painter.setBrush(Qt.NoBrush)
    elif name in ('alert.png', 'mismatch.png'):
        line((12, 4), (21, 20), (3, 20), (12, 4))
        line((12, 10), (12, 14))
        circle(12, 17, 0.35)
    elif name in ('plus.png', 'minus.png'):
        line((6, 12), (18, 12))
        if name == 'plus.png':
            line((12, 6), (12, 18))
    elif name == 'nosync.png':
        circle(12, 12, 7)
        line((7, 7), (17, 17))
    else:
        raise ValueError('Unknown icon alias: ' + name)
    painter.end()
    # Preserve the 24px logical size expected by the recovered controls.
    image = image.scaled(24, 24, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    raw = QByteArray()
    buffer = QBuffer(raw)
    buffer.open(QIODevice.WriteOnly)
    if not image.save(buffer, 'PNG'):
        raise RuntimeError('Could not encode icon')
    return bytes(raw)


def spinner(green=False):
    """Small looping GIF, encoded locally with a fixed palette and LZW codes."""
    palette = [(0, 0, 0)] + [(int(60 + i * 18), int(100 + i * 18),
                             int(80 + i * 18) if green else int(125 + i * 18))
                            for i in range(8)] + [(0, 0, 0)] * 7
    output = bytearray(b'GIF89a' + struct.pack('<HHBBB', 24, 24, 0xf3, 0, 0))
    output.extend(bytes(component for color in palette for component in color))
    output.extend(b'!\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00')
    for frame in range(8):
        pixels = [0] * (24 * 24)
        for dot in range(8):
            angle = dot * math.pi / 4
            cx, cy = 11.5 + 7 * math.cos(angle), 11.5 + 7 * math.sin(angle)
            for y in range(24):
                for x in range(24):
                    if (x - cx) ** 2 + (y - cy) ** 2 <= 4:
                        pixels[y * 24 + x] = 1 + (dot - frame) % 8
        # Clear before each pixel: five-bit codes stay fixed and portable.
        codes = [code for pixel in pixels for code in (16, pixel)] + [17]
        bits = sum(code << (5 * index) for index, code in enumerate(codes))
        encoded = bits.to_bytes((len(codes) * 5 + 7) // 8, 'little')
        output.extend(b'!\xf9\x04\x09' + struct.pack('<H', 8) + b'\x00\x00')
        output.extend(b',' + struct.pack('<HHHHB', 0, 0, 24, 24, 0) + b'\x04')
        for offset in range(0, len(encoded), 255):
            block = encoded[offset:offset + 255]
            output.extend(bytes([len(block)]) + block)
        output.append(0)
    return bytes(output + b';')


def build(rcc, output):
    output = Path(output)
    if output.exists():
        raise ValueError('Resource output must not already exist')
    with tempfile.TemporaryDirectory(prefix='cachecad-dev-resources-') as directory:
        root = Path(directory)
        for name in ALIASES:
            (root / name).write_bytes(spinner(name == 'loading2.gif')
                                     if name.endswith('.gif') else png(name))
        entries = ''.join(f'<file alias="{name}">{name}</file>' for name in ALIASES)
        (root / 'development.qrc').write_text('<RCC><qresource prefix="/assets">' + entries + '</qresource></RCC>')
        for resource in root.iterdir():
            os.utime(resource, (946684800, 946684800))
        # Qt's Linux build defaults to zstd, while the reviewed Mac resources use
        # zlib level 9. Pin both so payloads match on both platforms.
        subprocess.run([str(rcc), '--compress-algo', 'zlib', '--compress', '9', '-g', 'python',
                        'development.qrc', '-o', 'resources_rc.py'], cwd=root, check=True)
        raw = (root / 'resources_rc.py').read_bytes()
    output.write_bytes(raw)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rcc', required=True, type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    build(args.rcc, args.output)
