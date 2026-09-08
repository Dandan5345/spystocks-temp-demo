#!/usr/bin/env swift
import Foundation
import PDFKit
import Vision
import AppKit

guard CommandLine.arguments.count == 2,
      let document = PDFDocument(url: URL(fileURLWithPath: CommandLine.arguments[1])) else {
    fputs("Usage: vision_ocr.swift document.pdf\n", stderr)
    exit(2)
}

for pageNumber in 0..<document.pageCount {
    guard let page = document.page(at: pageNumber) else { continue }
    let bounds = page.bounds(for: .mediaBox)
    let scale: CGFloat = 2.2
    let width = Int(bounds.width * scale)
    let height = Int(bounds.height * scale)
    guard let context = CGContext(
        data: nil,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else { continue }
    context.setFillColor(NSColor.white.cgColor)
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    context.saveGState()
    context.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: context)
    context.restoreGState()
    guard let image = context.makeImage() else { continue }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["en-US"]
    try VNImageRequestHandler(cgImage: image).perform([request])
    let rows = (request.results ?? []).compactMap { observation -> [String: Any]? in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        return [
            "page": pageNumber + 1,
            "text": candidate.string,
            "x": observation.boundingBox.minX,
            "y": observation.boundingBox.minY,
            "w": observation.boundingBox.width,
            "h": observation.boundingBox.height
        ]
    }
    for row in rows.sorted(by: {
        let ay = ($0["y"] as! NSNumber).doubleValue, by = ($1["y"] as! NSNumber).doubleValue
        if abs(ay - by) > 0.008 { return ay > by }
        return ($0["x"] as! NSNumber).doubleValue < ($1["x"] as! NSNumber).doubleValue
    }) {
        let data = try JSONSerialization.data(withJSONObject: row)
        print(String(data: data, encoding: .utf8)!)
    }
}
