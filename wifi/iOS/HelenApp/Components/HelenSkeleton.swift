import SwiftUI

/// Inline shimmer placeholder. Drop in wherever a row, card, or block of
/// text is loading — it'll keep the layout stable and signal progress
/// more accurately than a centered spinner.
struct HelenSkeleton: View {
    var height: CGFloat = 14
    var width:  CGFloat? = nil
    var radius: CGFloat = HelenRadius.xs

    @Environment(\.theme)        private var theme
    @Environment(\.colorScheme)  private var scheme
    @State private var phase: CGFloat = -1

    var body: some View {
        RoundedRectangle(cornerRadius: radius, style: .continuous)
            .fill(theme.colors.surfaceAlt)
            .frame(maxWidth: width ?? .infinity)
            .frame(height: height)
            .overlay(shimmer)
            .accessibilityHidden(true)
            .onAppear {
                withAnimation(.linear(duration: 1.4).repeatForever(autoreverses: false)) {
                    phase = 2
                }
            }
    }

    private var shimmer: some View {
        // White on light surfaces, lifted-grey on dark. Subtle either way —
        // shimmer should suggest activity, not flash.
        let highlight: Color = scheme == .dark
            ? Color.white.opacity(0.06)
            : Color.white.opacity(0.55)
        return GeometryReader { geo in
            LinearGradient(
                stops: [
                    .init(color: .clear,   location: max(0, phase - 0.25)),
                    .init(color: highlight, location: phase),
                    .init(color: .clear,   location: min(1, phase + 0.25)),
                ],
                startPoint: .leading,
                endPoint:   .trailing
            )
            .frame(width: geo.size.width, height: geo.size.height)
        }
        .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
        .blendMode(.plusLighter)
    }
}

/// Pre-baked skeleton row that mirrors `HelenListRow` proportions.
/// Use to keep list-shaped UIs stable while data loads.
struct HelenSkeletonRow: View {
    var showsTrailing: Bool = false
    @Environment(\.theme) private var theme

    var body: some View {
        HStack(spacing: HelenSpace.md) {
            Circle()
                .fill(theme.colors.surfaceAlt)
                .frame(width: HelenSize.avatarMd, height: HelenSize.avatarMd)
            VStack(alignment: .leading, spacing: 6) {
                HelenSkeleton(height: 14, width: 160)
                HelenSkeleton(height: 11, width: 110)
            }
            Spacer(minLength: HelenSpace.sm)
            if showsTrailing {
                HelenSkeleton(height: 11, width: 40)
            }
        }
        .padding(.horizontal, HelenSpace.lg)
        .padding(.vertical,   HelenSpace.md)
    }
}

/// A grouped card of skeleton rows — drops straight into a `ScrollView`
/// where a populated card would later live.
struct HelenSkeletonList: View {
    var rows: Int = 4
    var body: some View {
        VStack(spacing: 0) {
            ForEach(0..<rows, id: \.self) { _ in
                HelenSkeletonRow(showsTrailing: true)
            }
        }
        .helenCardSurface()
    }
}

#Preview {
    VStack(spacing: HelenSpace.lg) {
        HelenSkeletonList(rows: 5)
        HelenSkeleton(height: 80, radius: HelenRadius.lg)
    }
    .padding()
    .background(HelenColor.background)
}
