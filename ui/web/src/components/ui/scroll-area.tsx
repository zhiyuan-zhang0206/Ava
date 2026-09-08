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

  // Element refs for the direct thumb positioning in positionThumb below.
  const viewportRef = React.useRef<HTMLDivElement | null>(null)
  const trackRef = React.useRef<HTMLDivElement | null>(null)
  const thumbRef = React.useRef<HTMLDivElement | null>(null)

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

  // Radix positions the thumb via an internal rAF polling loop that is armed
  // only on the FIRST scroll event of a gesture (addUnlinkedScrollListener);
  // when that loop is absent from the bundle the thumb freezes for the whole
  // gesture and only catches up at the next discrete scroll event — the
  // "scrollbar does not follow the content" report (2026-09-08, messages +
  // agent tree). Reposition the thumb directly from the viewport's scroll
  // events here instead: every event (60 Hz during a wheel/trackpad gesture)
  // moves the thumb in real time, with no dependency on Radix internals.
  // When Radix's loop IS present it writes the same value, so the two never
  // conflict.
  const positionThumb = React.useCallback(() => {
    const viewport = viewportRef.current
    const track = trackRef.current
    const thumb = thumbRef.current
    if (!viewport || !track || !thumb) return
    const maxScroll = viewport.scrollHeight - viewport.clientHeight
    if (maxScroll <= 0) {
      // Content fits (or layout is not measured yet): park the thumb at the
      // top of the track instead of a stale mid-track offset.
      thumb.style.transform = "translate3d(0, 0, 0)"
      return
    }
    const trackStyle = getComputedStyle(track)
    const padTop = Number.parseFloat(trackStyle.paddingTop) || 0
    const padBottom = Number.parseFloat(trackStyle.paddingBottom) || 0
    const travel = Math.max(
      0,
      track.clientHeight - padTop - padBottom - thumb.offsetHeight,
    )
    const offset = (viewport.scrollTop / maxScroll) * travel
    thumb.style.transform = `translate3d(0, ${offset}px, 0)`
  }, [])

  // Radix renders the thumb only while content overflows; when it (un)mounts
  // (fit -> overflow transition) position it right away.
  const setThumbRef = React.useCallback(
    (el: HTMLDivElement | null) => {
      thumbRef.current = el
      if (el) positionThumb()
    },
    [positionThumb],
  )

  React.useEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    viewport.addEventListener("scroll", positionThumb, { passive: true })
    // A viewport resize (window resize / panel drag) changes the scroll range
    // and thumb travel; re-anchor even when no scroll event follows.
    let resizeObserver: ResizeObserver | null = null
    if (typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(() => positionThumb())
      resizeObserver.observe(viewport)
    }
    positionThumb()
    return () => {
      viewport.removeEventListener("scroll", positionThumb)
      resizeObserver?.disconnect()
    }
  }, [positionThumb])

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
        ref={viewportRef}
        data-slot="scroll-area-viewport"
        className={cn(
          "size-full rounded-[inherit] transition-[color,box-shadow] outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-1",
          viewportClassName,
        )}
      >
        {children}
      </ScrollAreaPrimitive.Viewport>
      <ScrollBar
        trackRef={trackRef}
        thumbRef={setThumbRef}
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
  trackRef,
  thumbRef,
  ...props
}: React.ComponentProps<typeof ScrollAreaPrimitive.ScrollAreaScrollbar> & {
  // Internal plumbing: ScrollArea drives the thumb's translate directly from
  // viewport scroll events (see positionThumb), so it needs the track and
  // thumb elements.
  trackRef?: React.Ref<HTMLDivElement>;
  thumbRef?: React.Ref<HTMLDivElement>;
}) {
  return (
    <ScrollAreaPrimitive.ScrollAreaScrollbar
      ref={trackRef}
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
        ref={thumbRef}
        data-slot="scroll-area-thumb"
        tabIndex={0}
        className={cn("relative rounded-full bg-border", FLEX_1)}
      />
    </ScrollAreaPrimitive.ScrollAreaScrollbar>
  )
}

export { ScrollArea, ScrollBar }
