/**
 * ErrorBoundary — catches React render errors and prevents full app crash.
 * Shows a recovery UI instead of white screen.
 */
import React, { Component, ErrorInfo, ReactNode } from 'react';

interface ErrorBoundaryProps {
  children: ReactNode;
  fallback?: ReactNode;
  onError?: (error: Error, errorInfo: ErrorInfo) => void;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
  errorInfo: ErrorInfo | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false, error: null, errorInfo: null };
  }

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    this.setState({ errorInfo });
    console.error('[ErrorBoundary] Caught error:', error, errorInfo);
    this.props.onError?.(error, errorInfo);
  }

  private handleReload = (): void => {
    this.setState({ hasError: false, error: null, errorInfo: null });
  };

  private handleHardReload = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback;
      }

      return (
        <div className="h-screen w-screen bg-surface-950 flex items-center justify-center">
          <div className="text-center max-w-md p-8">
            <div className="text-5xl mb-4">⚠️</div>
            <h1 className="text-xl font-semibold text-surface-100 mb-2">
              Something went wrong
            </h1>
            <p className="text-surface-400 text-sm mb-6">
              {this.state.error?.message || 'An unexpected error occurred'}
            </p>
            <div className="flex gap-3 justify-center">
              <button
                onClick={this.handleReload}
                className="px-4 py-2 bg-primary-600 text-white rounded-lg text-sm hover:bg-primary-700 transition"
              >
                Try Again
              </button>
              <button
                onClick={this.handleHardReload}
                className="px-4 py-2 bg-surface-700 text-surface-200 rounded-lg text-sm hover:bg-surface-600 transition"
              >
                Reload App
              </button>
            </div>
            {this.state.errorInfo && (
              <details className="mt-6 text-left">
                <summary className="text-surface-500 text-xs cursor-pointer">
                  Error details
                </summary>
                <pre className="mt-2 text-xs text-surface-500 overflow-auto max-h-40 bg-surface-900 p-3 rounded">
                  {this.state.error?.stack}
                </pre>
              </details>
            )}
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
