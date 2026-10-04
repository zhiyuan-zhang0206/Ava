// Screen-text OCR for the computer-use daemon (Task #1101 Phase 2).
//
// Usage: ocr <image_path>
// Output: one TSV line per recognized text run —
//     text\tx\ty\tw\th
// with x/y/w/h in PHYSICAL pixels, top-left origin (Vision's boundingBox is
// normalized with a bottom-left origin; the y flip happens here), so the
// caller can click exactly where it read a word. Same coordinate contract as
// the daemon's other tools.
//
// Recognition: Vision VNRecognizeTextRequest, zh-Hans + en (the user's
// screens mix Chinese and English). Reads a PNG file — no TCC grant needed
// (screen-capture authorization lives in the permissions helper, which
// produced the PNG).

import Vision
import AppKit
import Foundation

let args = CommandLine.arguments
guard args.count >= 2 else {
    print("Usage: ocr <image_path>")
    exit(1)
}

let imagePath = args[1]
guard let img = NSImage(contentsOfFile: imagePath),
      let cgImage = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    print("ERROR: Could not load image: \(imagePath)")
    exit(1)
}

let imgWidth = CGFloat(cgImage.width)
let imgHeight = CGFloat(cgImage.height)

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en"]

do {
    try handler.perform([request])
    guard let results = request.results else { exit(0) }
    for observation in results {
        if let candidate = observation.topCandidates(1).first {
            let box = observation.boundingBox
            let x = box.origin.x * imgWidth
            let y = (1.0 - box.origin.y - box.size.height) * imgHeight
            let w = box.size.width * imgWidth
            let h = box.size.height * imgHeight
            print("\(candidate.string)\t\(x)\t\(y)\t\(w)\t\(h)")
        }
    }
} catch {
    print("ERROR: \(error.localizedDescription)")
    exit(1)
}
