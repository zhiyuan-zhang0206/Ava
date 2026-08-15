import * as React from "react"

import { cn } from "@/lib/utils"
import { FLEX } from "@/lib/layout";

interface TextareaProps extends React.ComponentProps<"textarea"> {
  /** Show the focus ring/border highlight. The composer's message input
   *  turns it off (user ruling 2026-08-06): a blinking caret is the only
   *  focus affordance there — no bold highlighted border. */
  focusVisible?: boolean;
}

function Textarea({ className, focusVisible = true, ...props }: TextareaProps) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        "field-sizing-content min-h-16 w-full rounded-lg border border-input bg-transparent px-2.5 py-2 text-base transition-colors outline-none placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:bg-input/50 disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 md:text-sm dark:bg-input/30 dark:disabled:bg-input/80 dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40",
        focusVisible &&
          "focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50",
        className,
        FLEX
      )}
      {...props}
    />
  )
}

export { Textarea }
