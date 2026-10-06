import SwiftUI

/// SAINT's look, the same as the desktop app (ui/theme.py): near-black surfaces, hairline borders, one accent —
/// SAINT orange — and colour reserved for state. Designed in Figma ("SAINT for iPhone — UI 2.0").
enum Theme {
    static func hex(_ value: UInt32) -> Color {
        Color(red: Double((value >> 16) & 0xff) / 255, green: Double((value >> 8) & 0xff) / 255, blue: Double(value & 0xff) / 255)
    }

    // surfaces and text (desktop dark palette)
    static let bg = hex(0x0a0b0d)
    static let surface = hex(0x121318)
    static let surface2 = hex(0x18191e)
    static let raised = hex(0x1e1f25)
    static let border = hex(0x1f2127)
    static let borderStrong = hex(0x2b2d34)
    static let text = hex(0xeceef2)
    static let muted = hex(0x8a8f98)
    static let faint = hex(0x555a63)

    // the accent
    static let accent = hex(0xfeaa34)
    static let accentSoft = hex(0x382b1c)
    static let accentLine = hex(0x5a4220)
    static let onAccent = hex(0x16120a)

    // status
    static let success = hex(0x4cc38a)
    static let successSoft = hex(0x1b2f2a)
    static let danger = hex(0xf06a6a)
    static let dangerSoft = hex(0x33191b)
    static let info = hex(0x8b9cff)
    static let infoSoft = hex(0x22243a)

    // assistant states (desktop state_color)
    static let ready = accent                 // waiting for "SAINT"
    static let listening = hex(0x5eead4)      // hearing a command
    static let capturing = listening
    static let thinking = hex(0x8b9cff)
    static let working = hex(0xc084fc)
    static let speaking = hex(0xff8a5c)
    static let idle = faint
    static let error = danger
}

/// A card on SAINT's surface with a hairline border, like the desktop's cards.
struct GlassCard: ViewModifier {
    var radius: CGFloat = 16
    var fill: Color = Theme.surface
    var stroke: Color = Theme.border

    func body(content: Content) -> some View {
        content
            .background(fill, in: RoundedRectangle(cornerRadius: radius, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: radius, style: .continuous).strokeBorder(stroke, lineWidth: 1))
    }
}

extension View {
    func glassCard(radius: CGFloat = 16, fill: Color = Theme.surface, stroke: Color = Theme.border) -> some View {
        modifier(GlassCard(radius: radius, fill: fill, stroke: stroke))
    }

    /// The background behind every screen.
    func saintBackground() -> some View {
        background(Theme.bg.ignoresSafeArea())
    }

    /// Close the keyboard from anywhere.
    func hideKeyboard() {
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
    }

    /// Drag down anywhere on the screen to put the keyboard away.
    func dismissKeyboardOnDragDown() -> some View {
        simultaneousGesture(DragGesture(minimumDistance: 24).onEnded { value in
            if value.translation.height > 40 && abs(value.translation.width) < value.translation.height {
                UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
            }
        })
    }
}

struct SectionTitle: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text)
            .font(.system(size: 11, weight: .semibold))
            .foregroundStyle(Theme.faint)
            .textCase(.uppercase)
            .tracking(0.6)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct Pill: View {
    let text: String
    var icon: String?
    var tint: Color = Theme.muted
    var fill: Color?

    var body: some View {
        HStack(spacing: 6) {
            if let icon = icon { Image(systemName: icon).imageScale(.small) }
            Text(text).lineLimit(1)
        }
        .font(.system(size: 12, weight: .medium))
        .foregroundStyle(tint)
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(fill ?? Theme.surface2, in: Capsule())
    }
}

/// "Done" / "Failed" / "On PC" / "Queued".
struct StatusChip: View {
    let status: String

    private var style: (String, Color, Color) {
        switch status {
        case "done": return ("Done", Theme.success, Theme.successSoft)
        case "failed": return ("Failed", Theme.danger, Theme.dangerSoft)
        case "sent": return ("On PC", Theme.info, Theme.infoSoft)
        case "queued": return ("Queued", Theme.accent, Theme.accentSoft)
        default: return ("Info", Theme.muted, Theme.surface2)
        }
    }

    var body: some View {
        let (label, tint, fill) = style
        Text(label)
            .font(.system(size: 11.5, weight: .semibold))
            .foregroundStyle(tint)
            .padding(.horizontal, 10)
            .padding(.vertical, 4)
            .background(fill, in: Capsule())
    }
}

/// The desktop's settings rows: an icon tile, a title and an optional subtitle, something on the right.
///
/// The text takes the room that's left and wraps; the control on the right keeps its own size and is never squeezed.
/// (Before, both were squeezable, so on smaller iPhones long titles and subtitles ran under switches and pickers.)
/// `stacked`: a wide control (a picker with a long choice) goes under the text instead of beside it.
struct SettingRow<Right: View>: View {
    let icon: String
    let title: String
    var subtitle: String? = nil
    var iconTint: Color = Theme.text
    var tile: Color = Theme.raised
    var stacked: Bool = false
    @ViewBuilder var right: () -> Right

    var body: some View {
        HStack(alignment: stacked ? .top : .center, spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(iconTint)
                .frame(width: 32, height: 32)
                .background(tile, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
            if stacked {
                VStack(alignment: .leading, spacing: 6) {
                    texts
                    right().fixedSize(horizontal: false, vertical: true)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                texts
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .layoutPriority(1)
                right().fixedSize()
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .contentShape(Rectangle())
    }

    private var texts: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.system(size: 15)).foregroundStyle(Theme.text)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
            if let subtitle = subtitle {
                Text(subtitle).font(.system(size: 12)).foregroundStyle(Theme.muted)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

extension SettingRow where Right == EmptyView {
    init(icon: String, title: String, subtitle: String? = nil, iconTint: Color = Theme.text, tile: Color = Theme.raised) {
        self.init(icon: icon, title: title, subtitle: subtitle, iconTint: iconTint, tile: tile, right: { EmptyView() })
    }
}

/// A group of rows in one card, separated by hairlines.
struct CardList<Content: View>: View {
    @ViewBuilder var content: () -> Content
    var body: some View {
        VStack(spacing: 0) { content() }
            .glassCard(radius: 16)
    }
}

struct RowDivider: View {
    var body: some View { Rectangle().fill(Theme.border).frame(height: 1) }
}

/// SAINT's logo (the orange star) from the asset catalog.
struct SaintLogo: View {
    var size: CGFloat = 28
    var body: some View {
        Image("SaintLogo")
            .resizable()
            .interpolation(.high)
            .scaledToFit()
            .frame(width: size, height: size)
            .accessibilityHidden(true)
    }
}
