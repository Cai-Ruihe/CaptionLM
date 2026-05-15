// CaptionLM — Combined audio capture + transcription via Apple SpeechAnalyzer
//
// Captures system audio with ScreenCaptureKit and feeds it directly to the
// SpeechAnalyzer/SpeechTranscriber API introduced in macOS 26 Tahoe (June 2025).
// Outputs JSON-line transcription results to stdout.
//
// EMPIRICAL EVIDENCE FOR THIS APPROACH:
//   - Apple's SpeechAnalyzer is ~55% faster than OpenAI Whisper (per Apple's
//     own benchmarks at WWDC25, processing a 34-min video in 45 seconds).
//   - Loads natively in Swift — no PyObjC bridge cold-load (which was 48s on
//     Python 3.14 per user's empirical data).
//   - Uses Apple Neural Engine via the Speech framework's on-device models.
//
// API REFERENCE: argmaxinc/apple-speechanalyzer-cli-example (verified working
// CLI demonstrating SpeechTranscriber + SpeechAnalyzer + AssetInventory).
//
// Build:
//   swiftc -O -o capture_and_transcribe capture_and_transcribe.swift \
//       -framework ScreenCaptureKit -framework CoreMedia \
//       -framework AVFoundation -framework Speech
//
// Usage:
//   ./capture_and_transcribe --locale en-US
//
// Protocol:
//   stderr: "READY\n" once initialization is complete.
//   stderr: "INFO: ...\n" for diagnostic messages.
//   stderr: "ERROR: ...\n" on fatal errors (process then exits non-zero).
//   stdout: JSON line per transcription result. One per line.
//           {"text": "transcribed text", "is_final": true|false}
//
// Requires: macOS 26.0 Tahoe (released 2025-06) on Apple Silicon.

import Foundation
import ScreenCaptureKit
import CoreMedia
import AVFoundation
import Speech
import Darwin

// MARK: - Stderr logging helpers

func logInfo(_ message: String) {
    fputs("INFO: \(message)\n", stderr)
}

func logError(_ message: String) {
    fputs("ERROR: \(message)\n", stderr)
}

// MARK: - JSON output helper

func emitTranscript(text: String, isFinal: Bool) {
    let payload: [String: Any] = ["text": text, "is_final": isFinal]
    guard let data = try? JSONSerialization.data(withJSONObject: payload, options: []),
          let line = String(data: data, encoding: .utf8) else {
        return
    }
    print(line)
    // Flush immediately so Python sees results as they happen
    fflush(stdout)
}

// MARK: - Combined capturer + transcriber (macOS 26+)

@available(macOS 26.0, *)
final class TranscribingCapturer: NSObject, SCStreamOutput, SCStreamDelegate {
    let locale: Locale

    var stream: SCStream?
    var analyzer: SpeechAnalyzer?
    var transcriber: SpeechTranscriber?
    var inputContinuation: AsyncStream<AnalyzerInput>.Continuation?
    var resultsTask: Task<Void, Never>?
    var diagnosticsTask: Task<Void, Never>?

    // Audio format conversion state — created lazily on first sample buffer
    var converter: AVAudioConverter?
    var sourceFormat: AVAudioFormat?
    var targetFormat: AVAudioFormat?

    // Diagnostic counters — Swift doesn't have built-in atomics, but for
    // monotonic counters read by a logging task this is OK in practice
    var buffersReceived: Int = 0
    var buffersFedToAnalyzer: Int = 0
    var resultsEmitted: Int = 0
    // Rolling peak amplitude over recent buffers — lets us see if audio
    // actually has voice content vs being silent / background noise only.
    var recentPeakSum: Float = 0.0
    var recentPeakCount: Int = 0

    init(locale: Locale) {
        self.locale = locale
        super.init()
    }

    func start() async throws {
        // ORDERING NOTE: We set up ScreenCaptureKit FIRST, then SpeechAnalyzer
        // last. Empirical: doing SpeechAnalyzer setup first appeared to put
        // macOS into a state where the SCStream produced 5 zero-filled
        // buffers and then went silent for the rest of the run.
        //
        // Also removed the SFSpeechRecognizer.requestAuthorization() call
        // that was here — Apple's CLI sample (argmaxinc) doesn't use it for
        // SpeechAnalyzer (which is a different API from SFSpeechRecognizer).
        // Speech Recognition permission is requested implicitly when the
        // analyzer first runs.

        // 1. Validate locale support — empirical: SpeechTranscriber.supportedLocales
        //    returns identifiers like "en_US", "ja_JP", "zh_CN".
        let supported = await SpeechTranscriber.supportedLocales
        if !supported.contains(locale) {
            let available = supported.map { $0.identifier }.sorted().joined(separator: ", ")
            throw NSError(domain: "CaptionLM", code: 1, userInfo: [
                NSLocalizedDescriptionKey:
                    "Locale '\(locale.identifier)' not supported by SpeechTranscriber. " +
                    "Supported: \(available)"
            ])
        }

        // 2. Create transcriber configured for streaming live audio.
        //    EMPIRICAL FIX: SpeechTranscriber.Preset.progressiveLiveTranscription
        //    does NOT exist in the released macOS 26 SDK (only in older betas).
        //    Use the explicit-options initializer with `.volatileResults` —
        //    this is the documented way to get streaming partial results.
        //    Volatile results are real-time guesses; final results replace them.
        let trans = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: [],
            reportingOptions: [.volatileResults],
            attributeOptions: [.audioTimeRange]
        )
        self.transcriber = trans

        // 3. Auto-download the on-device model for this locale if not installed
        //    (verified API from argmaxinc/apple-speechanalyzer-cli-example)
        if !(await SpeechTranscriber.installedLocales).contains(locale) {
            logInfo("Downloading on-device speech model for \(locale.identifier)…")
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [trans]) {
                try await request.downloadAndInstall()
                logInfo("Model installed")
            }
        }

        // 4. Create analyzer
        let analyzer = SpeechAnalyzer(modules: [trans])
        self.analyzer = analyzer

        // 5. Get the audio format SpeechAnalyzer wants and USE IT AS-IS.
        //    Empirical: forcing non-interleaved when SpeechAnalyzer wants
        //    interleaved makes the analyzer reject buffers and the stream
        //    dies after a few buffers. Use the suggested format unchanged,
        //    and write data via audioBufferList (which works for both
        //    interleaved and non-interleaved layouts).
        if let bestFormat = try await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [trans]) {
            self.targetFormat = bestFormat
            logInfo("Using SpeechAnalyzer suggested format: rate=\(bestFormat.sampleRate), " +
                    "channels=\(bestFormat.channelCount), " +
                    "commonFormat=\(bestFormat.commonFormat.rawValue), " +
                    "interleaved=\(bestFormat.isInterleaved)")
        } else {
            self.targetFormat = AVAudioFormat(
                commonFormat: .pcmFormatFloat32,
                sampleRate: 16000,
                channels: 1,
                interleaved: false
            )
            logInfo("Using fallback target format: 16kHz mono float32 non-interleaved")
        }

        // 6. Set up ScreenCaptureKit FIRST (before analyzer.start which seems
        //    to disrupt the audio session). The didOutputSampleBuffer callback
        //    will skip-return (`guard let continuation = inputContinuation
        //    else { return }`) until we set inputContinuation in step 8.
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false
        )
        guard let display = content.displays.first else {
            throw NSError(domain: "CaptionLM", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "No display found for ScreenCaptureKit"
            ])
        }
        let filter = SCContentFilter(
            display: display,
            excludingApplications: [],
            exceptingWindows: []
        )
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        // Request audio in the format SpeechAnalyzer wants (typically 16kHz mono).
        // This eliminates the need for our own format conversion → safer.
        config.sampleRate = Int(self.targetFormat?.sampleRate ?? 16000)
        config.channelCount = Int(self.targetFormat?.channelCount ?? 1)
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream!.addStreamOutput(
            self,
            type: .audio,
            sampleHandlerQueue: .global(qos: .userInteractive)
        )
        try await stream!.startCapture()
        logInfo("ScreenCaptureKit started — waiting for audio buffers to flow before connecting SpeechAnalyzer")

        // 6b. Wait briefly so SCStream's audio session is established BEFORE
        //     SpeechAnalyzer activates. Empirical: starting them in the
        //     other order made SCStream go silent after 5 buffers.
        try? await Task.sleep(nanoseconds: 1_000_000_000)  // 1 second

        // 7. Now create AsyncStream + start SpeechAnalyzer
        let (inputStream, continuation) = AsyncStream<AnalyzerInput>.makeStream()

        // 7b. Start consuming results in background BEFORE attaching the
        //     input stream — so we don't miss any early results.
        resultsTask = Task { [weak self] in
            guard let self = self, let trans = self.transcriber else { return }
            do {
                for try await result in trans.results {
                    let text = String(result.text.characters)
                        .trimmingCharacters(in: .whitespacesAndNewlines)
                    if text.isEmpty { continue }
                    self.resultsEmitted += 1
                    emitTranscript(text: text, isFinal: result.isFinal)
                }
                logInfo("transcriber.results stream ended (no more results)")
            } catch {
                logError("Results stream error: \(error.localizedDescription)")
            }
        }

        // 7c. Periodic diagnostics every 5 seconds — STARTED HERE so we see
        //     diag lines even if analyzer.start hangs or fails downstream.
        diagnosticsTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                guard let self = self else { return }
                let avgPeak = self.recentPeakCount > 0
                    ? self.recentPeakSum / Float(self.recentPeakCount)
                    : 0.0
                logInfo("[diag] buffers=\(self.buffersReceived), fed=\(self.buffersFedToAnalyzer), " +
                        "results=\(self.resultsEmitted), avg_peak_5s=\(avgPeak)")
                self.recentPeakSum = 0.0
                self.recentPeakCount = 0
            }
        }

        // 8. Connect: now that capture is running, hook up SpeechAnalyzer.
        //    Setting inputContinuation makes didOutputSampleBuffer start
        //    yielding buffers; analyzer.start consumes them.
        try await analyzer.start(inputSequence: inputStream)
        self.inputContinuation = continuation

        fputs("READY\n", stderr)
    }

    func stop() async {
        if let stream = stream {
            try? await stream.stopCapture()
        }
        inputContinuation?.finish()
        resultsTask?.cancel()
        if let analyzer = analyzer {
            await analyzer.cancelAndFinishNow()
        }
    }

    // MARK: - SCStreamOutput

    func stream(_ stream: SCStream,
                didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio else { return }
        guard sampleBuffer.isValid else { return }
        buffersReceived += 1
        guard let continuation = inputContinuation else { return }
        guard let targetFormat = targetFormat else { return }

        // Convert CMSampleBuffer → AVAudioPCMBuffer in target format
        guard let pcmBuffer = pcmBuffer(from: sampleBuffer, targetFormat: targetFormat) else {
            if buffersReceived <= 5 {
                logInfo("Buffer #\(buffersReceived): conversion FAILED")
            }
            return
        }

        let frameLen = pcmBuffer.frameLength

        // Sample peak from the Int16 buffer we just wrote.
        // CRITICAL FIX: was reading as Float32 from an Int16 buffer — that
        // reads 2x the buffer's actual byte size = SIGSEGV on first buffer.
        var peak: Float = 0.0
        let pBuffers = UnsafeMutableAudioBufferListPointer(pcmBuffer.mutableAudioBufferList)
        if pBuffers.count > 0, let raw = pBuffers[0].mData {
            let p = raw.assumingMemoryBound(to: Int16.self)
            let count = min(Int(frameLen), 1000)
            for i in 0..<count {
                let v = abs(Float(p[i])) / 32768.0  // normalize to 0..1
                if v > peak { peak = v }
            }
        }
        recentPeakSum += peak
        recentPeakCount += 1

        if buffersReceived <= 5 || frameLen == 0 {
            logInfo("Buffer #\(buffersReceived): frameLength=\(frameLen), peak=\(peak)")
        }
        if frameLen == 0 {
            return  // Don't feed empty buffers to SpeechAnalyzer
        }

        // Yield to analyzer's input stream — non-blocking
        continuation.yield(AnalyzerInput(buffer: pcmBuffer))
        buffersFedToAnalyzer += 1
    }

    // MARK: - Audio format conversion (manual, proven-working approach)
    //
    // EMPIRICAL FIX: AVAudioConverter with the endOfStream pattern produced
    // frameLength=0 for ALL buffers after the first one — the converter became
    // permanently "finalized" after the first endOfStream signal. Result:
    // SpeechAnalyzer received 1 mostly-empty buffer and never transcribed.
    //
    // This rewrite uses the same pattern as capture_audio.swift (which has
    // been working reliably for weeks): direct byte extraction from
    // CMBlockBuffer, manual mono-mix, manual sample-rate decimation, then
    // construct the AVAudioPCMBuffer manually. No AVAudioConverter involved.

    // ★ CRITICAL EMPIRICAL FIX ★
    // SpeechAnalyzer.bestAvailableAudioFormat() returns commonFormat=3
    // which is **pcmFormatInt16** (not Float32 as I'd been assuming).
    // SCStream gives us Float32. We must convert Float32 → Int16 before
    // handing the buffer to SpeechAnalyzer, otherwise it interprets our
    // Float bytes as Int16 garbage and crashes (signal -5, SIGTRAP).
    //
    // AVAudioCommonFormat enum:
    //   0 = otherFormat  1 = pcmFormatFloat32  2 = pcmFormatFloat64
    //   3 = pcmFormatInt16  4 = pcmFormatInt32
    func pcmBuffer(from sampleBuffer: CMSampleBuffer,
                   targetFormat: AVAudioFormat) -> AVAudioPCMBuffer? {
        // Step 1: Get raw Float32 PCM bytes from CMBlockBuffer
        guard let formatDesc = sampleBuffer.formatDescription else { return nil }
        guard let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc)?.pointee else {
            return nil
        }
        guard let blockBuffer = sampleBuffer.dataBuffer else { return nil }

        var dataPointer: UnsafeMutablePointer<Int8>?
        var lengthAtOffset: Int = 0
        let status = CMBlockBufferGetDataPointer(
            blockBuffer, atOffset: 0,
            lengthAtOffsetOut: &lengthAtOffset,
            totalLengthOut: nil,
            dataPointerOut: &dataPointer
        )
        guard status == kCMBlockBufferNoErr, let srcPtr = dataPointer else { return nil }

        let bytesPerSample = Int(asbd.mBitsPerChannel / 8)
        let channelCount = Int(asbd.mChannelsPerFrame)
        let sampleCount = lengthAtOffset / (bytesPerSample * max(channelCount, 1))
        guard sampleCount > 0 else { return nil }

        // We require Float32 input from SCStream (its default)
        guard asbd.mFormatFlags & kAudioFormatFlagIsFloat != 0, bytesPerSample == 4 else {
            logError("Source not Float32: bytes=\(bytesPerSample), isFloat=\((asbd.mFormatFlags & kAudioFormatFlagIsFloat) != 0)")
            return nil
        }

        // Step 2: Mix to mono if multichannel
        let floatPtr = UnsafeRawPointer(srcPtr).bindMemory(
            to: Float.self, capacity: sampleCount * channelCount
        )
        var monoFloats = [Float](repeating: 0, count: sampleCount)
        if channelCount > 1 {
            for i in 0..<sampleCount {
                var sum: Float = 0
                for ch in 0..<channelCount { sum += floatPtr[i * channelCount + ch] }
                monoFloats[i] = sum / Float(channelCount)
            }
        } else {
            for i in 0..<sampleCount { monoFloats[i] = floatPtr[i] }
        }

        // Step 3: Convert Float32 [-1.0, 1.0] → Int16 [-32768, 32767]
        // SpeechAnalyzer wants Int16 (commonFormat=3).
        var int16Samples = [Int16](repeating: 0, count: sampleCount)
        for i in 0..<sampleCount {
            let clamped = max(-1.0, min(1.0, monoFloats[i]))
            int16Samples[i] = Int16(clamped * Float(Int16.max))
        }

        // Step 4: Build AVAudioPCMBuffer in TARGET format (Int16) and copy in
        guard let pcmBuffer = AVAudioPCMBuffer(
            pcmFormat: targetFormat,
            frameCapacity: AVAudioFrameCount(sampleCount)
        ) else {
            return nil
        }
        pcmBuffer.frameLength = AVAudioFrameCount(sampleCount)

        let buffers = UnsafeMutableAudioBufferListPointer(pcmBuffer.mutableAudioBufferList)
        guard buffers.count > 0, let dstRaw = buffers[0].mData else { return nil }
        let dst = dstRaw.assumingMemoryBound(to: Int16.self)
        int16Samples.withUnsafeBufferPointer { src in
            dst.update(from: src.baseAddress!, count: sampleCount)
        }

        return pcmBuffer
    }

    // MARK: - SCStreamDelegate

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        logError("Stream stopped: \(error.localizedDescription)")
    }
}

// MARK: - Argument parsing

func parseArgs() -> String {
    var localeID = "en-US"
    var it = CommandLine.arguments.dropFirst().makeIterator()
    while let arg = it.next() {
        switch arg {
        case "--locale":
            localeID = it.next() ?? localeID
        case "--help", "-h":
            fputs("""
                Usage: capture_and_transcribe --locale <locale-id>
                Examples:
                  capture_and_transcribe --locale en-US
                  capture_and_transcribe --locale zh-CN
                  capture_and_transcribe --locale ja-JP

                Outputs JSON-line transcripts to stdout. Use Ctrl+C to stop.

                """, stderr)
            Darwin.exit(0)
        default:
            break
        }
    }
    return localeID
}

// MARK: - Main

let localeID = parseArgs()

guard #available(macOS 26.0, *) else {
    logError("SpeechAnalyzer requires macOS 26.0 Tahoe or newer.")
    Darwin.exit(EXIT_FAILURE)
}

// SpeechTranscriber.supportedLocales returns identifiers like "en_US"
// (underscore form). Locale() accepts both "en-US" and "en_US"; normalize.
let normalizedID = localeID.replacingOccurrences(of: "-", with: "_")
let locale = Locale(identifier: normalizedID)

let capturer = TranscribingCapturer(locale: locale)

// Signal handlers for clean shutdown
signal(SIGINT) { _ in
    fputs("Stopping (SIGINT)...\n", stderr)
    Darwin.exit(0)
}
signal(SIGTERM) { _ in
    Darwin.exit(0)
}

Task {
    do {
        try await capturer.start()
    } catch {
        logError(error.localizedDescription)
        logError("Common causes: missing Screen Recording permission " +
                 "(System Settings → Privacy & Security → Screen Recording), " +
                 "missing Speech Recognition permission, " +
                 "or unsupported locale.")
        Darwin.exit(EXIT_FAILURE)
    }
}

// Keep the run loop alive so the async tasks can run
RunLoop.main.run()
