/**
 * AppErrorBoundary — top-level safety net.
 *
 * If any component in the tree throws during render or in a lifecycle
 * method, React's default behaviour is to unmount the *entire* app. The
 * window then shows a single empty background colour and the user has
 * no way to recover except force-quit.
 *
 * This boundary catches that case, surfaces the error visibly, and
 * offers two recovery paths:
 *   1. Reload the renderer (preserves the Electron Main process state).
 *   2. Open the DebugCallPanel to inspect the failure log.
 *
 * The boundary also reports every catch into `callErrorLog` so the
 * crash is preserved for the operator. Combined with the renderer
 * watchdog (`installRendererWatchdog`), repeated failures eventually
 * trigger an automatic reload — but the boundary alone usually keeps
 * the user in business without one.
 */

import React, { type ReactNode } from 'react';
import { callErrorLog } from '@/services/call/CallErrorLog';

interface State {
    error: Error | null;
    errorInfo: React.ErrorInfo | null;
    /** Bumped on retry so children re-mount fresh. */
    nonce: number;
}

interface Props {
    children: ReactNode;
}

export class AppErrorBoundary extends React.Component<Props, State> {
    state: State = { error: null, errorInfo: null, nonce: 0 };

    static getDerivedStateFromError(error: Error): Partial<State> {
        return { error };
    }

    componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
        callErrorLog.error(
            'ErrorBoundary',
            error.message || 'Render error',
            { stack: error.stack, componentStack: errorInfo.componentStack },
        );
        this.setState({ errorInfo });
    }

    private handleRetry = () => {
        this.setState((s) => ({
            error: null,
            errorInfo: null,
            nonce: s.nonce + 1,
        }));
    };

    private handleReload = () => {
        // Soft renderer reload — the main process and its server connection
        // survive. Cheaper than a full app restart.
        window.location.reload();
    };

    render() {
        if (!this.state.error) {
            return (
                <React.Fragment key={this.state.nonce}>
                    {this.props.children}
                </React.Fragment>
            );
        }

        const err = this.state.error;
        return (
            <div style={{
                position: 'fixed', inset: 0, padding: 32,
                background: '#0d1018', color: '#e6e9f1',
                fontFamily: 'ui-sans-serif, system-ui, sans-serif',
                display: 'flex', flexDirection: 'column',
                alignItems: 'center', justifyContent: 'center',
                zIndex: 100_000,
                textAlign: 'center',
            }}>
                <div style={{
                    maxWidth: 540, width: '100%',
                    padding: 28, background: 'rgba(36,40,52,0.85)',
                    border: '1px solid rgba(120,130,160,0.35)', borderRadius: 16,
                    boxShadow: '0 24px 64px rgba(0,0,0,0.6)',
                }}>
                    <div style={{
                        fontSize: 32, marginBottom: 6, color: '#ff6f7d',
                    }}>⚠</div>
                    <h2 style={{ margin: '0 0 8px', fontSize: 18 }}>
                        Helen ran into a problem
                    </h2>
                    <p style={{ margin: '0 0 20px', color: '#a3aac0', fontSize: 14, lineHeight: 1.5 }}>
                        {err.message || 'A component crashed during render.'}
                    </p>
                    <div style={{ display: 'flex', gap: 8, justifyContent: 'center' }}>
                        <button onClick={this.handleRetry} style={btn('ghost')}>
                            Try again
                        </button>
                        <button onClick={this.handleReload} style={btn('primary')}>
                            Reload
                        </button>
                    </div>
                    <p style={{
                        marginTop: 18, color: '#6b7287', fontSize: 11,
                    }}>
                        Press <kbd style={kbd}>Ctrl+Shift+D</kbd> to open the debug panel.
                    </p>
                </div>
            </div>
        );
    }
}

function btn(variant: 'primary' | 'ghost'): React.CSSProperties {
    return {
        padding: '10px 18px',
        borderRadius: 10,
        fontSize: 14, fontWeight: 600,
        cursor: 'pointer',
        border: variant === 'primary'
            ? '1px solid #4f7cff'
            : '1px solid rgba(120,130,160,0.4)',
        background: variant === 'primary' ? '#4f7cff' : 'transparent',
        color: variant === 'primary' ? '#fff' : '#e6e9f1',
    };
}

const kbd: React.CSSProperties = {
    padding: '1px 6px', borderRadius: 4,
    background: 'rgba(120,130,160,0.18)',
    fontFamily: 'ui-monospace, monospace', fontSize: 11,
};
