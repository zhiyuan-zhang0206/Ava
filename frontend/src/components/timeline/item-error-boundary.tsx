"use client";

import { Component, type ReactNode } from "react";

// Per-item error boundary. If one timeline item throws while rendering
// (e.g. a transiently malformed payload mid-stream, or a child component's
// internal state getting out of sync), React unmounts up to the nearest
// boundary — without one, a single bad item blanks the ENTIRE timeline
// subtree. Scoping a boundary to each item degrades the failure to one
// small inline card; siblings keep rendering. The fallback prints the
// error message so it's visible on-device without opening a console.
//
// `resetKey` should change whenever the item's content changes (pass the
// payload). Once an item has thrown, the boundary stays in the error state
// for that instance; resetting on the next chunk lets a transient bad
// payload recover when a good one arrives.

interface Props {
  resetKey: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ItemErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidUpdate(prevProps: Props): void {
    if (this.state.error && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="border-l-2 border-destructive/50 bg-destructive/5 px-3 py-2 rounded-r-sm text-xs font-mono text-destructive [overflow-wrap:anywhere]">
          Failed to render this item: {this.state.error.message}
        </div>
      );
    }
    return this.props.children;
  }
}
