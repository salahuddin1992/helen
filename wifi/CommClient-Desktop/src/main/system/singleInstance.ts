/**
 * Single-instance lock + second-launch router.
 *
 * Enforces "one running CommClient at a time" via
 * `app.requestSingleInstanceLock`. Second launches pass their argv
 * into the running process, which is how we receive
 * `commclient://...` deep links without spawning a second window.
 *
 * The `second-instance` event is also wired in `protocolHandler.ts`
 * so deep-link parsing happens there; here we focus on the raw lock
 * + window-focus behaviour.
 */

import { app, BrowserWindow } from 'electron';

export interface SingleInstanceOptions {
  getMainWindow: () => BrowserWindow | null;
  /** Called when a second instance tries to launch. Receives the
   *  argv of the second attempt. */
  onSecondInstance?: (argv: string[], cwd: string) => void;
}

export function installSingleInstanceLock(opts: SingleInstanceOptions): boolean {
  const gotLock = app.requestSingleInstanceLock({ pid: process.pid });
  if (!gotLock) {
    console.log('[singleInstance] another instance already running — exiting');
    app.exit(0);
    return false;
  }

  app.on('second-instance', (_event, argv, cwd) => {
    console.log('[singleInstance] second-instance received', { cwd });
    // Raise existing window immediately — deep link parsing happens
    // inside protocolHandler.ts which also listens to this event.
    const w = opts.getMainWindow();
    if (w) {
      if (w.isMinimized()) w.restore();
      w.show();
      w.focus();
    }
    opts.onSecondInstance?.(argv, cwd);
  });

  return true;
}
