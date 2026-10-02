#!/usr/bin/env swift
import AppKit
import CoreGraphics
import Foundation

// Draws the CLINX Monitor app icon and writes an .icns for the app bundle.
//
// The accepted open-ring mark on a 1024 grid: 824 tile at (100,100),
// 185 corner radius, ring radius 210 / 66 stroke, and a 59.7° right opening.
// Center the visible stroked bounds, including the round caps: ring x =
// 512 + 210 * (1 - cos(29.86°)) / 2 ≈ 526. Move the dot with the ring.
// The drop shadow is dropped below 64px, exactly as the design notes.
//
// usage: swift Scripts/make-app-icon.swift <path/to/AppIcon.icns>

let tileRect = CGRect(x: 100, y: 100, width: 824, height: 824)
let edgeRect = CGRect(x: 101.5, y: 101.5, width: 821, height: 821)
let ringCenter = CGPoint(x: 526, y: 512)
let ringRadius: CGFloat = 210
let ringStroke: CGFloat = 66
let gapHalfAngle = 29.86
let dotCenter = CGPoint(x: 558, y: 512)
let dotRadius: CGFloat = 64

let tileTop = CGColor(red: 0x2B / 255, green: 0x2C / 255, blue: 0x31 / 255, alpha: 1)
let tileBottom = CGColor(red: 0x0D / 255, green: 0x0E / 255, blue: 0x10 / 255, alpha: 1)
let ringColor = CGColor(red: 0xF4 / 255, green: 0xF4 / 255, blue: 0xF5 / 255, alpha: 1)
let dotColor = CGColor(red: 0x7C / 255, green: 0x84 / 255, blue: 0xE8 / 255, alpha: 1)

let sRGB = CGColorSpace(name: CGColorSpace.sRGB)!

func render(px: Int, withShadow: Bool) -> CGImage? {
    guard let ctx = CGContext(data: nil, width: px, height: px, bitsPerComponent: 8,
                              bytesPerRow: 0, space: sRGB,
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
        return nil
    }
    let scale = CGFloat(px) / 1024
    ctx.interpolationQuality = .high
    // Flip into the design's top-left origin space.
    ctx.translateBy(x: 0, y: CGFloat(px))
    ctx.scaleBy(x: scale, y: -scale)

    let tilePath = CGPath(roundedRect: tileRect, cornerWidth: 185, cornerHeight: 185, transform: nil)

    if withShadow {
        ctx.saveGState()
        ctx.setShadow(offset: CGSize(width: 0, height: 14), blur: 16,
                      color: CGColor(red: 0, green: 0, blue: 0, alpha: 0.32))
        ctx.addPath(tilePath)
        ctx.setFillColor(tileBottom)
        ctx.fillPath()
        ctx.restoreGState()
    }

    ctx.saveGState()
    ctx.addPath(tilePath)
    ctx.clip()
    if let gradient = CGGradient(colorsSpace: sRGB, colors: [tileTop, tileBottom] as CFArray, locations: [0, 1]) {
        ctx.drawLinearGradient(gradient, start: CGPoint(x: 512, y: 100), end: CGPoint(x: 512, y: 924), options: [])
    }
    ctx.restoreGState()

    // Top edge highlight (white 16% fading to nothing over the upper third).
    ctx.saveGState()
    ctx.addPath(CGPath(roundedRect: edgeRect, cornerWidth: 183.5, cornerHeight: 183.5, transform: nil))
    ctx.setLineWidth(3)
    ctx.replacePathWithStrokedPath()
    ctx.clip()
    let highlight = [CGColor(red: 1, green: 1, blue: 1, alpha: 0.16),
                     CGColor(red: 1, green: 1, blue: 1, alpha: 0),
                     CGColor(red: 1, green: 1, blue: 1, alpha: 0)] as CFArray
    if let gradient = CGGradient(colorsSpace: sRGB, colors: highlight, locations: [0, 0.35, 1]) {
        ctx.drawLinearGradient(gradient, start: CGPoint(x: 512, y: 100), end: CGPoint(x: 512, y: 924), options: [])
    }
    ctx.restoreGState()

    // Open ring: from +29.86° the long way round to +330.14°, leaving the gap on the right.
    let ringPath = CGMutablePath()
    let start = gapHalfAngle * .pi / 180
    let end = (360 - gapHalfAngle) * .pi / 180
    let steps = 512
    for step in 0...steps {
        let angle = start + (end - start) * Double(step) / Double(steps)
        let point = CGPoint(x: ringCenter.x + ringRadius * CGFloat(cos(angle)),
                            y: ringCenter.y + ringRadius * CGFloat(sin(angle)))
        step == 0 ? ringPath.move(to: point) : ringPath.addLine(to: point)
    }
    ctx.addPath(ringPath)
    ctx.setStrokeColor(ringColor)
    ctx.setLineWidth(ringStroke)
    ctx.setLineCap(.round)
    ctx.setLineJoin(.round)
    ctx.strokePath()

    // The observed execution.
    ctx.setFillColor(dotColor)
    ctx.fillEllipse(in: CGRect(x: dotCenter.x - dotRadius, y: dotCenter.y - dotRadius,
                               width: dotRadius * 2, height: dotRadius * 2))

    return ctx.makeImage()
}

func pngData(px: Int, withShadow: Bool) -> Data? {
    guard let image = render(px: px, withShadow: withShadow) else { return nil }
    return NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:])
}

// MARK: - iconset

let outputPath = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : FileManager.default.currentDirectoryPath + "/AppIcon.icns"
let outputURL = URL(fileURLWithPath: outputPath)
let iconsetURL = outputURL.deletingLastPathComponent().appendingPathComponent("AppIcon.iconset")

try? FileManager.default.removeItem(at: iconsetURL)
try FileManager.default.createDirectory(at: iconsetURL, withIntermediateDirectories: true)

let entries: [(String, Int)] = [
    ("icon_16x16", 16), ("icon_16x16@2x", 32),
    ("icon_32x32", 32), ("icon_32x32@2x", 64),
    ("icon_128x128", 128), ("icon_128x128@2x", 256),
    ("icon_256x256", 256), ("icon_256x256@2x", 512),
    ("icon_512x512", 512), ("icon_512x512@2x", 1024),
]

for (name, px) in entries {
    guard let data = pngData(px: px, withShadow: px >= 64) else {
        FileHandle.standardError.write("failed to render \(name)\n".data(using: .utf8)!)
        exit(1)
    }
    try data.write(to: iconsetURL.appendingPathComponent("\(name).png"))
}

// Optional 1024 master, kept outside the app bundle for review.
if CommandLine.arguments.count > 2, let master = pngData(px: 1024, withShadow: true) {
    try master.write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
}

let iconutil = Process()
iconutil.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
iconutil.arguments = ["-c", "icns", iconsetURL.path, "-o", outputURL.path]
try iconutil.run()
iconutil.waitUntilExit()
guard iconutil.terminationStatus == 0 else {
    FileHandle.standardError.write("iconutil failed\n".data(using: .utf8)!)
    exit(1)
}
try? FileManager.default.removeItem(at: iconsetURL)
print(outputURL.path)
