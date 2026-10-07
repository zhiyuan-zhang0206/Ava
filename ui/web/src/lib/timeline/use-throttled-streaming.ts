"use client";

import { useEffect, useRef, useState } from "react";

// While `live` is true, returns `value` updated at most once per
// `intervalMs` (leading edge fires immediately, trailing edge catches the
// final chunk of a burst). When `live` is false, returns the latest
// `value` immediately.
//
// Purpose: a streaming timeline item's payload accumulates 10-50 chunks/s,
// and the chat-markdown / python-highlight render path re-parses the FULL
// accumulated text on each change. That parse is a single synchronous,
// uninterruptible call; at chunk rate on a mobile CPU it saturates the
// main thread (the composer can't even focus). The heavy parse only needs
// to run a few times a second while streaming, and once in full on settle.
// ChatMarkdown / PythonCode memo on their content prop, so feeding them a
// value held constant across an interval skips re-parsing entirely for
// that window — capping parse frequency while keeping live rendering.
export function useThrottledStreaming(
  value: string,
  live: boolean,
  intervalMs: number,
): string {
  const [shown, setShown] = useState(value);
  const lastFlushRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Latest value for the trailing-edge timer to read (avoids a stale
  // closure capturing the value at schedule time). Written in the effect,
  // never during render.
  const valueRef = useRef(value);

  useEffect(() => {
    valueRef.current = value;
    if (!live) {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      // Settle: render the final value in full immediately.
      // eslint-disable-next-line react-hooks/set-state-in-effect -- throttle settle
      setShown(value);
      return;
    }
    const elapsed = Date.now() - lastFlushRef.current;
    if (elapsed >= intervalMs) {
      lastFlushRef.current = Date.now();
      setShown(value);
      return;
    }
    // A burst arrived within the window: schedule one trailing flush that
    // reads the latest value. ??= keeps a single in-flight timer.
    timerRef.current ??= setTimeout(() => {
      timerRef.current = null;
      lastFlushRef.current = Date.now();
      setShown(valueRef.current);
    }, intervalMs - elapsed);
  }, [value, live, intervalMs]);

  useEffect(
    () => () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    },
    [],
  );

  // When not live, bypass the throttled state entirely so the final value
  // shows without waiting for the effect to commit.
  return live ? shown : value;
}
