import SwiftUI

/// SAINT's presence: a soft, breathing orb that follows the microphone while it listens, brightens when it
/// hears its name, swirls while it thinks and pulses while it speaks.
struct OrbView: View {
    let phase: VoiceEngine.Phase
    let level: Float
    var speaking = false

    private var color: Color {
        if speaking { return Theme.speaking }
        switch phase {
        case .off: return Theme.idle
        case .listening: return Theme.listening
        case .capturing: return Theme.capturing
        case .thinking: return Theme.thinking
        case .speaking: return Theme.speaking
        }
    }

    private var energy: Double {
        switch phase {
        case .off: return 0.05
        case .listening: return 0.25 + Double(level) * 0.6
        case .capturing: return 0.55 + Double(level) * 0.9
        case .thinking: return 0.5
        case .speaking: return 0.6
        }
    }

    var body: some View {
        TimelineView(.animation) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            Canvas { canvas, size in
                let center = CGPoint(x: size.width / 2, y: size.height / 2)
                let base = min(size.width, size.height) * 0.30
                let swirl = phase == .thinking ? 2.2 : 1.0
                canvas.addFilter(.blur(radius: 18))
                for i in 0..<4 {
                    let a = t * (0.5 + Double(i) * 0.23) * swirl + Double(i) * 1.7
                    let wobble = base * (0.18 + 0.22 * energy)
                    let x = center.x + cos(a) * wobble
                    let y = center.y + sin(a * 1.3) * wobble
                    let radius = base * (0.78 + 0.18 * sin(t * 1.4 + Double(i))) * (1 + 0.28 * energy)
                    let rect = CGRect(x: x - radius, y: y - radius, width: radius * 2, height: radius * 2)
                    let shade = color.opacity(0.55 - Double(i) * 0.08)
                    canvas.fill(Path(ellipseIn: rect), with: .color(shade))
                }
            }
            .overlay {
                Circle()
                    .fill(RadialGradient(colors: [color.opacity(0.9), color.opacity(0.25)], center: .center, startRadius: 4, endRadius: 90))
                    .frame(width: 120, height: 120)
                    .scaleEffect(1 + 0.12 * energy + 0.03 * sin(t * 2))
                    .blur(radius: 2)
                    .overlay(Circle().strokeBorder(.white.opacity(0.35), lineWidth: 1).frame(width: 120, height: 120)
                        .scaleEffect(1 + 0.12 * energy + 0.03 * sin(t * 2)))
            }
        }
        .animation(.easeInOut(duration: 0.4), value: phase)
        .accessibilityHidden(true)
    }
}
