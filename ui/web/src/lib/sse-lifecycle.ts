// Timers and legacy EventSource error handling shared by the system and alert
// streams. Each provider owns its frame parsing, state changes, and transport.

interface SseLifecycleOptions {
  failCount: { current: number };
  watchdogMs: number;
  reconnectBaseMs: number;
  reconnectMaxMs: number;
  onWatchdog: () => void;
  onRetry: () => void;
  onClosed: () => void;
  onConnecting: () => void;
  checkAuth: () => Promise<{ authenticated: boolean }>;
  onInvalidSession: () => void;
}

export function createSseLifecycle(options: SseLifecycleOptions) {
  let disposed = false;
  let watchdog: ReturnType<typeof setTimeout> | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  const clearWatchdog = () => {
    if (watchdog !== null) clearTimeout(watchdog);
    watchdog = null;
  };

  const armWatchdog = () => {
    clearWatchdog();
    watchdog = setTimeout(options.onWatchdog, options.watchdogMs);
  };

  const scheduleReopen = () => {
    if (retryTimer !== null) return;
    const delay = Math.min(
      options.reconnectBaseMs * 2 ** options.failCount.current,
      options.reconnectMaxMs,
    );
    options.failCount.current += 1;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      options.onRetry();
    }, delay);
  };

  const probeAndReopen = () => {
    void options.checkAuth()
      .then((res) => {
        if (disposed) return;
        if (!res.authenticated) {
          options.onInvalidSession();
          return;
        }
        scheduleReopen();
      })
      .catch(() => {
        if (disposed) return;
        // An unreachable gateway is transient; a failed probe still retries.
        scheduleReopen();
      });
  };

  const handleLegacyError = (es: EventSource) => {
    if (disposed) return;
    switch (es.readyState) {
      case EventSource.CLOSED:
        options.onClosed();
        probeAndReopen();
        return;
      case EventSource.CONNECTING:
        options.onConnecting();
        return;
      case EventSource.OPEN:
        return;
      default:
        throw new Error(`unknown EventSource readyState: ${es.readyState}`);
    }
  };

  const dispose = () => {
    disposed = true;
    clearWatchdog();
    if (retryTimer !== null) clearTimeout(retryTimer);
  };

  return {
    armWatchdog,
    clearWatchdog,
    scheduleReopen,
    resetBackoff: () => { options.failCount.current = 0; },
    handleLegacyError,
    dispose,
    isDisposed: () => disposed,
  };
}
