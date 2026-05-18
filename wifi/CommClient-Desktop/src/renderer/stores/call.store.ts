/**
 * @deprecated v1 call store — DO NOT IMPORT.
 *
 * The legacy v1 store (joinGroupCall hard-coded routing="sfu", emitted
 * the old `call_join_group` socket event, never wired GroupCallManager
 * or peer connections) has been removed.
 *
 * Every component must import the unified store at `./call.store.v2`.
 * This module re-exports the v2 hook so any forgotten import compiles
 * but at least lands on the working implementation. A console warning
 * fires once per session so dev sees the legacy import path.
 *
 * The v1 split-brain (component-by-component routing inconsistency)
 * was the #1 production blocker called out in the §5 group-features
 * audit; this file is the gravestone.
 */

import { useCallStore as _useCallStoreV2 } from './call.store.v2';

let _warnedOnce = false;
function _warnDeprecated() {
  if (_warnedOnce) return;
  _warnedOnce = true;
   
  console.warn(
    "[deprecation] './stores/call.store' is removed. " +
    "Import from './stores/call.store.v2' instead. " +
    "Until you migrate, this shim forwards to v2.",
  );
}

export const useCallStore: typeof _useCallStoreV2 = ((...args: unknown[]) => {
  _warnDeprecated();
  // @ts-expect-error — forward all args to the v2 hook
  return _useCallStoreV2(...args);
}) as typeof _useCallStoreV2;
