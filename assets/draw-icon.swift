// SPDX-License-Identifier: GPL-3.0-or-later
import AppKit

let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
            bytesPerRow: 0, bitsPerPixel: 0)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
        let transform = NSAffineTransform()
        transform.scale(by: CGFloat(pixels) / 1024)
        transform.concat()
        let tile = NSBezierPath(roundedRect: NSRect(x: 74, y: 74, width: 876, height: 876), xRadius: 200, yRadius: 200)
        NSGradient(starting: NSColor(srgbRed: 0.44, green: 0.24, blue: 0.90, alpha: 1),
                   ending: NSColor(srgbRed: 0.18, green: 0.08, blue: 0.42, alpha: 1))!.draw(in: tile, angle: -65)
        NSColor.white.setFill()
        NSBezierPath(roundedRect: NSRect(x: 280, y: 221, width: 464, height: 582), xRadius: 57, yRadius: 57).fill()
        NSColor(srgbRed: 0.80, green: 0.75, blue: 0.98, alpha: 1).setFill()
        for y in [665, 595] {
            NSBezierPath(roundedRect: NSRect(x: 365, y: y, width: 294, height: 28), xRadius: 14, yRadius: 14).fill()
        }
        NSColor(srgbRed: 0.38, green: 0.19, blue: 0.79, alpha: 1).setStroke()
        let arrow = NSBezierPath()
        arrow.move(to: NSPoint(x: 512, y: 330))
        arrow.line(to: NSPoint(x: 512, y: 510))
        arrow.move(to: NSPoint(x: 430, y: 428))
        arrow.line(to: NSPoint(x: 512, y: 510))
        arrow.line(to: NSPoint(x: 594, y: 428))
        arrow.lineWidth = 39
        arrow.lineCapStyle = .round
        arrow.lineJoinStyle = .round
        arrow.stroke()
        NSGraphicsContext.restoreGraphicsState()
        let suffix = scale == 2 ? "@2x" : ""
        try bitmap.representation(using: .png, properties: [:])!.write(to: output.appendingPathComponent("icon_\(size)x\(size)\(suffix).png"))
    }
}
