// CaptionLM — System audio capture helper
// Captures all system audio output via ScreenCaptureKit and writes raw PCM
// (float32, mono, 16kHz) to stdout. Python reads from this pipe.
//
// Build: swiftc -O -o capture_audio capture_audio.swift -framework ScreenCaptureKit -framework CoreMedia -framework AVFoundation
// Usage: ./capture_audio  (outputs raw PCM float32 to stdout, Ctrl+C to stop)

import Foundation
import ScreenCaptureKit
import CoreMedia
import AVFoundation

class AudioCapturer: NSObject, SCStreamOutput, SCStreamDelegate {
    let targetSampleRate: Double = 16000.0
    var stream: SCStream?
    var isRunning = false

    func start() async throws {
        // Get available content
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)

        guard let display = content.displays.first else {
            fputs("ERROR: No display found\n", stderr)
            exit(1)
        }

        // Filter: capture everything on the display
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])

        // Configure for audio capture
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48000  // ScreenCaptureKit default, we resample later
        config.channelCount = 1

        // Minimize video overhead (we only need audio)
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1) // 1 fps

        // Create and start stream
        stream = SCStream(filter: filter, configuration: config, delegate: self)

        try stream!.addStreamOutput(self, type: .audio, sampleHandlerQueue: .global(qos: .userInteractive))
        try await stream!.startCapture()

        isRunning = true
        fputs("READY\n", stderr)
    }

    func stop() async {
        if let stream = stream {
            try? await stream.stopCapture()
        }
        isRunning = false
    }

    // MARK: - SCStreamOutput

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        guard sampleBuffer.isValid else { return }

        // Get audio buffer
        guard let blockBuffer = sampleBuffer.dataBuffer else { return }
        let length = CMBlockBufferGetDataLength(blockBuffer)
        guard length > 0 else { return }

        // Get raw bytes
        var dataPointer: UnsafeMutablePointer<Int8>?
        var lengthAtOffset: Int = 0
        let status = CMBlockBufferGetDataPointer(blockBuffer, atOffset: 0, lengthAtOffsetOut: &lengthAtOffset, totalLengthOut: nil, dataPointerOut: &dataPointer)

        guard status == kCMBlockBufferNoErr, let ptr = dataPointer else { return }

        // Get format description
        guard let formatDesc = sampleBuffer.formatDescription else { return }
        guard let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc)?.pointee else { return }

        let sourceSampleRate = asbd.mSampleRate
        let bytesPerSample = Int(asbd.mBitsPerChannel / 8)
        let channelCount = Int(asbd.mChannelsPerFrame)

        // Convert raw bytes to Float32 samples
        let sampleCount = lengthAtOffset / (bytesPerSample * channelCount)
        guard sampleCount > 0 else { return }

        var floatSamples: [Float]

        if asbd.mFormatFlags & kAudioFormatFlagIsFloat != 0 && bytesPerSample == 4 {
            // Already float32
            let floatPtr = UnsafeRawPointer(ptr).bindMemory(to: Float.self, capacity: sampleCount * channelCount)
            if channelCount > 1 {
                // Mix to mono
                floatSamples = [Float](repeating: 0, count: sampleCount)
                for i in 0..<sampleCount {
                    var sum: Float = 0
                    for ch in 0..<channelCount {
                        sum += floatPtr[i * channelCount + ch]
                    }
                    floatSamples[i] = sum / Float(channelCount)
                }
            } else {
                floatSamples = Array(UnsafeBufferPointer(start: floatPtr, count: sampleCount))
            }
        } else {
            // Unsupported format, skip
            return
        }

        // Simple downsample from source rate to 16kHz
        if sourceSampleRate != targetSampleRate && sourceSampleRate > 0 {
            let ratio = sourceSampleRate / targetSampleRate
            let outputCount = Int(Double(floatSamples.count) / ratio)
            var downsampled = [Float](repeating: 0, count: outputCount)
            for i in 0..<outputCount {
                let srcIdx = min(Int(Double(i) * ratio), floatSamples.count - 1)
                downsampled[i] = floatSamples[srcIdx]
            }
            floatSamples = downsampled
        }

        // Write raw float32 PCM to stdout
        floatSamples.withUnsafeBufferPointer { buffer in
            let rawPtr = UnsafeRawPointer(buffer.baseAddress!)
            let byteCount = buffer.count * MemoryLayout<Float>.size
            let data = Data(bytes: rawPtr, count: byteCount)
            FileHandle.standardOutput.write(data)
        }
    }

    // MARK: - SCStreamDelegate

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        // Increased verbosity 2026-05-13 after real meeting log showed
        // the bare "Stream was stopped by the system" wasn't actionable.
        // Now also log: NSError domain, code, userInfo, full localized
        // description. Common SCStream error codes:
        //   -3801  SCStreamErrorUserStopped
        //   -3802  SCStreamErrorNoDisplayList
        //   -3803  SCStreamErrorNoCaptureSource
        //   -3804  SCStreamErrorRemovedByUser (e.g. screen lock)
        //   -3805  SCStreamErrorUserDeclined
        //   -3806  SCStreamErrorFailedToStart
        //   -3807  SCStreamErrorMissingEntitlements
        //   -3808  SCStreamErrorFailedApplicationConnectionInvalid
        //   -3809  SCStreamErrorFailedApplicationConnectionInterrupted
        //   -3810  SCStreamErrorFailedNoMatchingApplicationContext
        //   -3811  SCStreamErrorAttemptToStartStreamState
        //   -3812  SCStreamErrorAttemptToStopStreamState
        //   -3813  SCStreamErrorAttemptToUpdateFilterState
        //   -3814  SCStreamErrorAttemptToConfigState
        //   -3815  SCStreamErrorInternalError
        //   -3816  SCStreamErrorInvalidParameter
        //   -3817  SCStreamErrorNoWindowList
        //   -3818  SCStreamErrorNoCaptureSourceAccess
        //   -3819  SCStreamErrorRemovedSystem (e.g. audio device changed)
        let ns = error as NSError
        let domain = ns.domain
        let code = ns.code
        let userInfoStr = ns.userInfo.isEmpty ? "<none>" : "\(ns.userInfo)"
        fputs("ERROR: Stream stopped: \(error.localizedDescription)\n", stderr)
        fputs("ERROR: Stream stop detail: domain=\(domain) code=\(code) userInfo=\(userInfoStr)\n", stderr)
        isRunning = false
    }
}

// Main
let capturer = AudioCapturer()

// Handle SIGINT/SIGTERM gracefully
signal(SIGINT) { _ in
    fputs("Stopping...\n", stderr)
    exit(0)
}
signal(SIGTERM) { _ in
    exit(0)
}

// Parent-PID watchdog (Layer 3 of subprocess-leak defense).
// Why: if Python parent dies via SIGKILL, crash, or any path that doesn't
// give it a chance to send SIGTERM to us, we get reparented to launchd
// (PID 1) and would otherwise live forever consuming CPU and ScreenCaptureKit
// resources. We poll getppid() once per second and self-exit when orphaned.
let originalParentPID = getppid()
fputs("INFO: capture_audio parent PID = \(originalParentPID)\n", stderr)
DispatchQueue.global(qos: .background).async {
    while true {
        Thread.sleep(forTimeInterval: 1.0)
        let currentParent = getppid()
        if currentParent == 1 || currentParent != originalParentPID {
            fputs("INFO: Parent died (was \(originalParentPID), now \(currentParent)), self-exiting\n", stderr)
            exit(0)
        }
    }
}

Task {
    do {
        try await capturer.start()
    } catch {
        fputs("ERROR: Failed to start capture: \(error.localizedDescription)\n", stderr)
        fputs("Make sure Screen Recording permission is granted in:\n", stderr)
        fputs("System Settings > Privacy & Security > Screen Recording\n", stderr)
        exit(1)
    }
}

// Keep running
RunLoop.main.run()
