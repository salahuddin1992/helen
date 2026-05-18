import SwiftUI

/// A tiny state machine for "load → empty / loaded / error" UI flows.
///
/// The view layer never has to model `isLoading: Bool && error == nil &&
/// items.isEmpty` again — drive a single `ContentState<T>` and pass it to
/// `AsyncContentView`.
enum ContentState<Value>: Equatable where Value: Equatable {
    case idle
    case loading
    case loaded(Value)
    case empty
    case failed(message: String)
}

/// Switches between idle / loading / empty / loaded / failed views.
///
/// ```swift
/// AsyncContentView(state: vm.state) { items in
///     ForEach(items) { ItemRow(item: $0) }
/// } emptyTitle: "No contacts" emptyMessage: "Add a contact to get started." {
///     vm.reload()                       // retry handler
/// }
/// ```
struct AsyncContentView<Value: Equatable, Loaded: View>: View {
    let state: ContentState<Value>
    let emptySymbol: String
    let emptyTitle: LocalizedStringKey
    let emptyMessage: LocalizedStringKey
    var loadingMessage: LocalizedStringKey? = nil
    @ViewBuilder let loaded: (Value) -> Loaded
    var onRetry: (() -> Void)? = nil

    var body: some View {
        switch state {
        case .idle:
            Color.clear

        case .loading:
            HelenLoadingState(message: loadingMessage)

        case .loaded(let value):
            loaded(value)

        case .empty:
            HelenEmptyState(
                symbol: emptySymbol,
                title: emptyTitle,
                message: emptyMessage
            )

        case .failed(let message):
            HelenErrorState(
                message: LocalizedStringKey(message),
                retry: onRetry
            )
        }
    }
}
