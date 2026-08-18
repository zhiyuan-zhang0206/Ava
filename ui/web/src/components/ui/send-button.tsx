"use client";

import { ArrowUp, Loader2, Square } from "lucide-react";

import { cn } from "@/lib/utils";
import { FLEX } from "@/lib/layout";

// The circular send affordance shared by every reply surface (the home
// composer and the fleet "Waiting on you" answer box), so they never drift
// apart visually. State picks the glyph:
//  - "send"    → ArrowUp (submit)
//  - "sending" → spinner (in-flight, button disabled by the caller)
//  - "stop"    → filled square (cancel the current turn; composer only)
export type SendButtonState = "send" | "sending" | "stop";

interface SendButtonProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  state: SendButtonState;
}

export function SendButton({ state, className, ...props }: SendButtonProps) {
  return (
    <button
      type="button"
      className={cn(
        "size-9 shrink-0 rounded-full items-center justify-center transition",
        // disabled:pointer-events-none is more reliable than relying on
        // disabled:hover: variant source-order — it eats hover directly so
        // hover:bg-primary/90 can't apply while disabled.
        "bg-primary text-primary-foreground hover:bg-primary/90 active:scale-95",
        "disabled:opacity-40 disabled:cursor-not-allowed disabled:pointer-events-none",
        className,
        FLEX
      )}
      {...props}
    >
      {state === "sending" ? (
        <Loader2 className="size-4 animate-spin" strokeWidth={2.5} />
      ) : state === "stop" ? (
        // lucide Square defaults to a stroked square; we want filled.
        <Square className="size-3" fill="currentColor" />
      ) : (
        <ArrowUp className="size-4" strokeWidth={2.5} />
      )}
    </button>
  );
}
