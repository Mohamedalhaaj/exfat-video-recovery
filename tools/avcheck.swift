import AVFoundation
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let asset = AVURLAsset(url: url)
let sem = DispatchSemaphore(value: 0)
Task {
    do {
        let (playable, dur, tracks) = try await asset.load(.isPlayable, .duration, .tracks)
        print("playable=\(playable) duration=\(String(format: "%.3f", dur.seconds))s tracks=\(tracks.map { $0.mediaType.rawValue })")
        let gen = AVAssetImageGenerator(asset: asset)
        gen.appliesPreferredTrackTransform = true
        for t in [10.0, 1900.0, 3800.0] {
            let (img, actual) = try await gen.image(at: CMTime(seconds: t, preferredTimescale: 600))
            print("frame at \(String(format: "%.1f", actual.seconds))s: \(img.width)x\(img.height)")
        }
    } catch { print("ERROR: \(error)") }
    sem.signal()
}
sem.wait()
