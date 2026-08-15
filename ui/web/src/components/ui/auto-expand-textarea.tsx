"use client";

import React, { useCallback } from "react";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

interface AutoExpandTextareaProps
  extends Omit<React.ComponentProps<"textarea">, "onKeyDown"> {
  /** Called when Enter is pressed (without Shift) and IME is not composing. */
  onSend: () => void;
  /** Additional keydown handler — runs for keys not consumed by Enter-to-send. */
  onKeyDown?: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
}

/**
 * A textarea that auto-grows via CSS field-sizing-content and sends on Enter.
 *
 * Shift+Enter inserts a newline; Enter fires `onSend`. IME composition is
 * detected via nativeEvent.isComposing + the "Process" key fallback (older
 * Safari), so committing a CJK candidate does not trigger a send.
 */
export const AutoExpandTextarea = React.forwardRef<
  HTMLTextAreaElement,
  AutoExpandTextareaProps
>(function AutoExpandTextarea({ onSend, onKeyDown, className, ...props }, ref) {
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      const composing = e.nativeEvent.isComposing || e.key === "Process";
      if (e.key === "Enter" && !e.shiftKey && !composing) {
        e.preventDefault();
        onSend();
        return;
      }
      onKeyDown?.(e);
    },
    [onSend, onKeyDown],
  );

  return (
    <Textarea
      ref={ref}
      onKeyDown={handleKeyDown}
      className={cn("resize-none", className)}
      {...props}
    />
  );
});
