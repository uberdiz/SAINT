import SwiftUI

/// SAINT's presence: the logo inside rings coloured by state (the desktop's state colours) — orange and calm while
/// it waits for its name, teal and following your voice while it listens, swirling while it thinks, pulsing while
/// it speaks, red on a problem. Respects Reduce Motion.
struct OrbView: View {
    let phase: VoiceEngine.Phase
    let level: Float
    var speaking = false
    var error = false
    /// Compact form for the talk button (no outer halos).
    var compact = false

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var color: Color {
        if error && phase == .off { return Theme.error }
        if speaking { return Theme.speaking }
        switch phase {
        case .off: return Theme.idle
        case .listening: return Theme.ready
        case .capturing: return Theme.listening
        case .thinking: return Theme.thinking
        case .speaking: return Theme.speaking
        }
    }

    private var energy: Double {
        if speaking { return 0.6 }
        switch phase {
        case .off: return 0.0
        case .listening: return 0.15 + Double(level) * 0.4
        case .capturing: return 0.45 + Double(level) * 1.2
        case .thinking: return 0.45
        case .speaking: return 0.6
        }
    }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: reduceMotion || phase == .off)) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            GeometryReader { geo in
                let d = min(geo.size.width, geo.size.height)
                let pulse = reduceMotion ? 0 : (phase == .thinking ? sin(t * 4) * 0.04 : sin(t * 2.2) * 0.025)
                ZStack {
                    if !compact {
                        ForEach(0..<3, id: \.self) { i in
                            let base = d * (0.62 + 0.19 * Double(i))
                            let size = base * (1 + energy * 0.12 * Double(i + 1) + pulse)
                            Circle()
                                .fill(color.opacity(0.07 + 0.05 * Double(2 - i)))
                                .frame(width: size, height: size)
                        }
                    }
                    if phase == .thinking && !reduceMotion {
                        let spin = d * (compact ? 1.12 : 0.56)
                        Circle()
                            .trim(from: 0, to: 0.28)
                            .stroke(color, style: StrokeStyle(lineWidth: compact ? 2 : 3, lineCap: .round))
                            .frame(width: spin, height: spin)
                            .rotationEffect(.radians(t * 3))
                    }
                    let core = d * (compact ? 1 : 0.49)
                    Circle()
                        .fill(Theme.accentSoft)
                        .overlay(Circle().strokeBorder(color, lineWidth: compact ? 1.5 : 2.5))
                        .frame(width: core, height: core)
                        .shadow(color: color.opacity(phase == .off ? 0 : 0.5), radius: compact ? 10 : 18)
                        .scaleEffect(1 + (compact ? energy * 0.06 : 0) + pulse)
                    SaintLogo(size: d * (compact ? 0.64 : 0.32))
                }
                .frame(width: geo.size.width, height: geo.size.height)
            }
        }
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.35), value: phase)
        .accessibilityHidden(true)
    }
}
