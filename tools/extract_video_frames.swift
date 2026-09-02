#!/usr/bin/env swift

import AVFoundation
import Foundation
import ImageIO
import UniformTypeIdentifiers
import VideoToolbox

enum ExtractionError: Error, CustomStringConvertible {
    case usage
    case missingVideoTrack
    case cannotAddOutput
    case cannotStartReader(String)
    case missingPixelBuffer(Int)
    case cannotCreateImage(Int)
    case cannotCreateDestination(URL)
    case cannotWriteImage(URL)

    var description: String {
        switch self {
        case .usage:
            return "usage: extract_video_frames.swift INPUT.mp4 OUTPUT_DIRECTORY"
        case .missingVideoTrack:
            return "input contains no video track"
        case .cannotAddOutput:
            return "AVAssetReader rejected the decoded video output"
        case .cannotStartReader(let message):
            return "could not start AVAssetReader: \(message)"
        case .missingPixelBuffer(let index):
            return "decoded sample \(index) has no pixel buffer"
        case .cannotCreateImage(let index):
            return "could not create a CGImage for decoded sample \(index)"
        case .cannotCreateDestination(let url):
            return "could not create PNG destination \(url.path)"
        case .cannotWriteImage(let url):
            return "could not write PNG \(url.path)"
        }
    }
}

func extract(input: URL, outputDirectory: URL) throws {
    let manager = FileManager.default
    try manager.createDirectory(
        at: outputDirectory,
        withIntermediateDirectories: true
    )

    let asset = AVURLAsset(url: input)
    guard let track = asset.tracks(withMediaType: .video).first else {
        throw ExtractionError.missingVideoTrack
    }
    let reader = try AVAssetReader(asset: asset)
    let output = AVAssetReaderTrackOutput(
        track: track,
        outputSettings: [
            kCVPixelBufferPixelFormatTypeKey as String:
                Int(kCVPixelFormatType_32BGRA)
        ]
    )
    output.alwaysCopiesSampleData = false
    guard reader.canAdd(output) else {
        throw ExtractionError.cannotAddOutput
    }
    reader.add(output)
    guard reader.startReading() else {
        throw ExtractionError.cannotStartReader(
            reader.error?.localizedDescription ?? "unknown reader error"
        )
    }

    var frameCount = 0
    var firstTime = Double.nan
    var lastTime = Double.nan
    var dimensions = "unknown"
    while let sample = output.copyNextSampleBuffer() {
        let timestamp = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sample))
        if frameCount == 0 { firstTime = timestamp }
        lastTime = timestamp
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sample) else {
            throw ExtractionError.missingPixelBuffer(frameCount)
        }
        dimensions = "\(CVPixelBufferGetWidth(pixelBuffer))x\(CVPixelBufferGetHeight(pixelBuffer))"
        var image: CGImage?
        let status = VTCreateCGImageFromCVPixelBuffer(
            pixelBuffer,
            options: nil,
            imageOut: &image
        )
        guard status == noErr, let cgImage = image else {
            throw ExtractionError.cannotCreateImage(frameCount)
        }

        let outputURL = outputDirectory.appendingPathComponent(
            String(format: "frame-%03d.png", frameCount)
        )
        guard let destination = CGImageDestinationCreateWithURL(
            outputURL as CFURL,
            UTType.png.identifier as CFString,
            1,
            nil
        ) else {
            throw ExtractionError.cannotCreateDestination(outputURL)
        }
        CGImageDestinationAddImage(destination, cgImage, nil)
        guard CGImageDestinationFinalize(destination) else {
            throw ExtractionError.cannotWriteImage(outputURL)
        }
        frameCount += 1
    }

    if reader.status == .failed {
        throw ExtractionError.cannotStartReader(
            reader.error?.localizedDescription ?? "reader failed during decode"
        )
    }
    let duration = CMTimeGetSeconds(asset.duration)
    let report: [String: Any] = [
        "input": input.path,
        "output_directory": outputDirectory.path,
        "dimensions": dimensions,
        "frame_count": frameCount,
        "nominal_fps": track.nominalFrameRate,
        "duration_s": duration,
        "first_timestamp_s": firstTime,
        "last_timestamp_s": lastTime,
    ]
    let data = try JSONSerialization.data(
        withJSONObject: report,
        options: [.prettyPrinted, .sortedKeys]
    )
    print(String(decoding: data, as: UTF8.self))
}

do {
    guard CommandLine.arguments.count == 3 else {
        throw ExtractionError.usage
    }
    try extract(
        input: URL(fileURLWithPath: CommandLine.arguments[1]),
        outputDirectory: URL(fileURLWithPath: CommandLine.arguments[2])
    )
} catch {
    FileHandle.standardError.write(Data("error: \(error)\n".utf8))
    exit(1)
}
