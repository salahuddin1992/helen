import SwiftUI

struct SignInView: View {
    @EnvironmentObject private var session: HelenSession
    @Environment(\.theme) private var theme

    @State private var username = ""
    @State private var password = ""
    @State private var displayName = ""
    @State private var mode: Mode = .signIn
    @State private var isLoading = false
    @State private var error: LocalizedStringKey? = nil

    private enum Mode { case signIn, register }

    var body: some View {
        ZStack {
            theme.colors.background.ignoresSafeArea()

            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: HelenSpace.xxl) {
                    header
                    serverChip
                    form
                    Spacer(minLength: HelenSpace.lg)
                    HelenButton(
                        title: mode == .signIn ? "Continue" : "Create an account",
                        variant: .primary,
                        isLoading: isLoading,
                        isDisabled: !canSubmit,
                        action: submit
                    )
                    footer
                }
                .padding(.horizontal, HelenSpace.pageH)
                .padding(.top, HelenSpace.huge)
                .padding(.bottom, HelenSpace.xl)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: HelenSpace.md) {
            ZStack {
                RoundedRectangle(cornerRadius: HelenRadius.lg, style: .continuous)
                    .fill(theme.colors.accent)
                    .frame(width: 64, height: 64)
                    .helenShadow(.md)
                Image(systemName: "wave.3.right")
                    .font(.system(size: 28, weight: .bold))
                    .foregroundStyle(.white)
            }
            .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: HelenSpace.xs) {
                Text(mode == .signIn ? "Welcome to Helen" : "Create your account")
                    .font(HelenFont.display)
                    .foregroundStyle(theme.colors.textPrimary)
                Text("Sign in to keep talking — even when there's no internet.")
                    .font(HelenFont.subhead)
                    .foregroundStyle(theme.colors.textSecondary)
            }
        }
    }

    private var serverChip: some View {
        HStack(spacing: HelenSpace.sm) {
            Image(systemName: "server.rack")
                .foregroundStyle(theme.colors.accent)
            Text(session.serverURL?.absoluteString ?? "—")
                .font(HelenFont.footnote.weight(.medium))
                .foregroundStyle(theme.colors.textSecondary)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer()
            Button {
                Task { await session.forgetServer() }
            } label: {
                Text("Change").font(HelenFont.footnote.weight(.semibold))
                    .foregroundStyle(theme.colors.accent)
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, HelenSpace.md)
        .padding(.vertical, HelenSpace.sm)
        .background(theme.colors.surface)
        .clipShape(RoundedRectangle(cornerRadius: HelenRadius.md, style: .continuous))
    }

    private var form: some View {
        VStack(spacing: HelenSpace.md) {
            if mode == .register {
                HelenTextField(label: "Full name", text: $displayName,
                               placeholder: "Your name", icon: "person.text.rectangle",
                               contentType: .name,
                               submitLabel: .next)
            }
            HelenTextField(label: "Username", text: $username,
                           placeholder: "yourname", icon: "person",
                           contentType: .username,
                           submitLabel: .next)
            HelenTextField(label: "Password", text: $password,
                           placeholder: "••••••••", icon: "lock",
                           contentType: mode == .signIn ? .password : .newPassword,
                           isSecure: true,
                           error: error,
                           submitLabel: .go,
                           onSubmit: submit)
        }
    }

    private var footer: some View {
        HStack(spacing: 4) {
            Text(mode == .signIn ? "New here?" : "Already have an account?")
                .font(HelenFont.footnote)
                .foregroundStyle(theme.colors.textSecondary)
            Button {
                withAnimation(HelenMotion.quick) {
                    mode = (mode == .signIn) ? .register : .signIn
                    error = nil
                }
            } label: {
                Text(mode == .signIn ? "Create an account" : "Sign in")
                    .font(HelenFont.footnote.weight(.semibold))
                    .foregroundStyle(theme.colors.accent)
            }
            .buttonStyle(.plain)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, HelenSpace.md)
    }

    private var canSubmit: Bool {
        guard !username.isEmpty, password.count >= 4 else { return false }
        if mode == .register { return !displayName.isEmpty }
        return true
    }

    private func submit() {
        guard canSubmit, !isLoading else { return }
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        error = nil
        isLoading = true
        Task { @MainActor in
            do {
                switch mode {
                case .signIn:
                    try await session.login(username: username, password: password)
                case .register:
                    try await session.register(username: username, password: password,
                                               displayName: displayName)
                }
            } catch {
                self.error = LocalizedStringKey(error.localizedDescription)
                UINotificationFeedbackGenerator().notificationOccurred(.error)
            }
            isLoading = false
        }
    }
}

#Preview {
    SignInView().environmentObject(HelenSession())
}
