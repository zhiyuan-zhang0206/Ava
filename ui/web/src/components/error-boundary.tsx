"use client";

import { Component, type ReactNode } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { FLEX, FLEX_COL } from "@/lib/layout";
import { cn } from "@/lib/utils";

/**
 * Section-level error boundary — catches render errors in a subtree and
 * shows a fallback UI instead of a white screen. Unlike ItemErrorBoundary
 * (which scopes to a single timeline item), this one guards a whole UI
 * section (sidebar, chat area, fleet view).
 *
 * Errors are logged to console with their component stack for debugging.
 * A "Retry" button resets the error state and re-renders children.
 */

interface Props {
  /** Optional custom fallback UI. Receives the error and a reset callback. */
  fallback?: (error: Error, reset: () => void) => ReactNode;
  /** Called when an error is caught, after logging. */
  onError?: (error: Error, errorInfo: React.ErrorInfo) => void;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo): void {
    console.error(
      "[ErrorBoundary] Caught render error:",
      error.message,
      "\nComponent stack:",
      errorInfo.componentStack,
    );
    this.props.onError?.(error, errorInfo);
  }

  handleReset = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    if (this.state.error) {
      if (this.props.fallback) {
        return this.props.fallback(this.state.error, this.handleReset);
      }
      return (
        <div className={cn("items-center justify-center p-8", FLEX)}>
          <div className={cn("items-center gap-3 max-w-md text-center", FLEX, FLEX_COL)}>
            <AlertTriangle className="size-8 text-destructive" />
            <div>
              <p className="text-sm font-semibold text-destructive">
                Something went wrong
              </p>
              <p className="text-xs text-muted-foreground mt-1 font-mono [overflow-wrap:anywhere]">
                {this.state.error.message}
              </p>
            </div>
            <button
              type="button"
              onClick={this.handleReset}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium bg-secondary text-secondary-foreground hover:bg-secondary/80 transition-colors"
            >
              <RefreshCw className="size-3" />
              Retry
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
