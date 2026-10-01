import SwiftUI

/// The look: system colours and materials, big rounded type, and Liquid Glass where the OS has it.
enum Theme {
    static let accent = Color(red: 0.36, green: 0.52, blue: 1.0)
    static let listening = Color(red: 0.31, green: 0.55, blue: 1.0)
    static let capturing = Color(red: 0.20, green: 0.85, blue: 0.75)
    static let thinking = Color(red: 0.68, green: 0.42, blue: 1.0)
    static let speaking = Color(red: 1.0, green: 0.55, blue: 0.40)
    static let idle = Color.gray
    static let error = Color(red: 1.0, green: 0.36, blue: 0.36)
}

/// A rounded card on a material, or Liquid Glass on iOS 26.
struct GlassCard: ViewModifier {
    var radius: CGFloat = 22

    func body(content: Content) -> some View {
        #if compiler(>=6.2)
        if #available(iOS 26.0, *) {
            content.glassEffect(.regular, in: RoundedRectangle(cornerRadius: radius, style: .continuous))
        } else {
            fallback(content)
        }
        #else
        fallback(content)
        #endif
    }

    private func fallback(_ content: Content) -> some View {
        content
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: radius, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: radius, style: .continuous).strokeBorder(.white.opacity(0.08)))
    }
}

extension View {
    func glassCard(radius: CGFloat = 22) -> some View { modifier(GlassCard(radius: radius)) }

    /// The soft background behind every screen.
    func saintBackground() -> some View {
        background(
            LinearGradient(colors: [Color(.systemBackground), Color(.secondarySystemBackground)], startPoint: .top, endPoint: .bottom)
                .ignoresSafeArea()
        )
    }
}

struct SectionTitle: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text)
            .font(.system(.footnote, design: .rounded, weight: .semibold))
            .foregroundStyle(.secondary)
            .textCase(.uppercase)
            .tracking(0.6)
    }
}

struct Pill: View {
    let text: String
    var icon: String?
    var tint: Color = .secondary

    var body: some View {
        HStack(spacing: 5) {
            if let icon = icon { Image(systemName: icon).imageScale(.small) }
            Text(text).lineLimit(1)
        }
        .font(.system(.caption, design: .rounded, weight: .medium))
        .foregroundStyle(tint)
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(tint.opacity(0.12), in: Capsule())
    }
}
