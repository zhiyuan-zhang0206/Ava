"use client"

import * as ResizablePrimitive from "react-resizable-panels"

import { cn } from "@/lib/utils"
import { FLEX } from "@/lib/layout";

function ResizablePanelGroup({
  className,
  ...props
}: React.ComponentProps<typeof ResizablePrimitive.Group>) {
  // No vertical variant class: v4 sets display/flex-direction inline from
  // `orientation` (a Group style it does not let callers override).
  return (
    <ResizablePrimitive.Group
      data-slot="resizable-panel-group"
      className={cn("h-full w-full", className, FLEX)}
      {...props}
    />
  )
}

function ResizablePanel({
  ...props
}: React.ComponentProps<typeof ResizablePrimitive.Panel>) {
  return <ResizablePrimitive.Panel data-slot="resizable-panel" {...props} />
}

// Resize handle, matched to the home sidebar's drag affordance (agent-sidebar/):
// a 1px `after` separator at rest and on hover/drag. Keeping paint off the
// handle body lets callers inset that line while the full-height invisible
// `before` strip preserves the forgiving drag target.
//
// v4 styling hooks: `data-separator` carries the interaction state
// (inactive / hover / active / focus / disabled) and `aria-orientation` carries
// the separator's own axis — a separator in a vertical group is horizontal.
function ResizableHandle({
  className,
  ...props
}: React.ComponentProps<typeof ResizablePrimitive.Separator>) {
  return (
    <ResizablePrimitive.Separator
      data-slot="resizable-handle"
      className={cn(
        "relative z-10 w-px bg-transparent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        "before:absolute before:inset-y-0 before:left-1/2 before:w-2 before:-translate-x-1/2",
        "after:absolute after:top-0 after:bottom-0 after:left-1/2 after:w-px after:-translate-x-1/2 after:bg-border after:transition-colors hover:after:bg-primary/40 data-[separator=active]:after:bg-primary/60",
        "aria-[orientation=horizontal]:h-px aria-[orientation=horizontal]:w-full aria-[orientation=horizontal]:before:inset-x-0 aria-[orientation=horizontal]:before:left-0 aria-[orientation=horizontal]:before:h-2 aria-[orientation=horizontal]:before:w-full aria-[orientation=horizontal]:before:-translate-y-1/2 aria-[orientation=horizontal]:before:translate-x-0 aria-[orientation=horizontal]:after:inset-x-0 aria-[orientation=horizontal]:after:left-0 aria-[orientation=horizontal]:after:h-px aria-[orientation=horizontal]:after:w-full aria-[orientation=horizontal]:after:-translate-y-1/2 aria-[orientation=horizontal]:after:translate-x-0",
        className
      )}
      {...props}
    />
  )
}

export { ResizablePanelGroup, ResizablePanel, ResizableHandle }
