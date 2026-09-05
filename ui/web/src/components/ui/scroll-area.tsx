"use client"

import * as React from "react"
import * as ScrollAreaPrimitive from "@radix-ui/react-scroll-area"

import { cn } from "@/lib/utils"
import { FLEX, FLEX_1, OVERFLOW_HIDDEN } from "@/lib/layout";

const SCROLLBAR_IDLE_DELAY_MS = 800

function ScrollArea({
  className,
  viewportClassName,
  children,
  onScrollCapture,
  ...props
}: React.ComponentProps<typeof ScrollAreaPrimitive.Root> & {
  // Extra classes for the inner scroll viewport (the element that actually
  // scrolls). Used e.g. by the timeline to set `overflow-anchor: none`.
  viewportClassName?: string;
}) {
  const [isScrollActive, setIsScrollActive] = React.useState(false)
  const [isDraggingScrollbar, setIsDraggingScrollbar] = React.useState(false)
  const idleTimerRef = React.useRef<number | null>(null)

  const clearIdleTimer = React.useCallback(() => {
    if (idleTimerRef.current !== null) {
      window.clearTimeout(idleTimerRef.current)
      idleTimerRef.current = null
    }
  }, [])

  const showScrollbarUntilIdle = React.useCallback(() => {
    clearIdleTimer()
    setIsScrollActive(true)
    idleTimerRef.current = window.setTimeout(() => {
      setIsScrollActive(false)
      idleTimerRef.current = null
    }, SCROLLBAR_IDLE_DELAY_MS)
  }, [clearIdleTimer])

  React.useEffect(() => clearIdleTimer, [clearIdleTimer])

  return (
    <ScrollAreaPrimitive.Root
      data-slot="scroll-area"
      type="always"
      className={cn("relative", className, OVERFLOW_HIDDEN)}
      {...props}
      onScrollCapture={(event) => {
        onScrollCapture?.(event)
        showScrollbarUntilIdle()
      }}
    >
      <ScrollAreaPrimitive.Viewport
        data-slot="scroll-area-viewport"
        className={cn(
          "size-full rounded-[inherit] transition-[color,box-shadow] outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-1",
          viewportClassName,
        )}
      >
        {children}
      </ScrollAreaPrimitive.Viewport>
      <ScrollBar
        data-visible={isScrollActive || isDraggingScrollbar}
        onPointerDown={(event) => {
          if (event.button !== 0) return
          clearIdleTimer()
          setIsDraggingScrollbar(true)
        }}
        onPointerUp={() => {
          if (!isDraggingScrollbar) return
          setIsDraggingScrollbar(false)
          showScrollbarUntilIdle()
        }}
        onPointerCancel={() => {
          if (!isDraggingScrollbar) return
          setIsDraggingScrollbar(false)
          showScrollbarUntilIdle()
        }}
      />
      <ScrollAreaPrimitive.Corner />
    </ScrollAreaPrimitive.Root>
  )
}

function ScrollBar({
  className,
  orientation = "vertical",
  ...props
}: React.ComponentProps<typeof ScrollAreaPrimitive.ScrollAreaScrollbar>) {
  return (
    <ScrollAreaPrimitive.ScrollAreaScrollbar
      data-slot="scroll-area-scrollbar"
      orientation={orientation}
      className={cn(
        "touch-none p-px opacity-0 transition-opacity duration-300 select-none focus-within:opacity-100 data-[visible=true]:opacity-100 data-[orientation=horizontal]:h-2.5 data-[orientation=horizontal]:flex-col data-[orientation=horizontal]:border-t data-[orientation=horizontal]:border-t-transparent data-[orientation=vertical]:h-full data-[orientation=vertical]:w-2.5 data-[orientation=vertical]:border-l data-[orientation=vertical]:border-l-transparent",
        className,
        FLEX
      )}
      {...props}
    >
      <ScrollAreaPrimitive.ScrollAreaThumb
        data-slot="scroll-area-thumb"
        tabIndex={0}
        className={cn("relative rounded-full bg-border", FLEX_1)}
      />
    </ScrollAreaPrimitive.ScrollAreaScrollbar>
  )
}

export { ScrollArea, ScrollBar }
