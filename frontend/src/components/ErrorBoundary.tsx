import { Component, type ErrorInfo, type ReactNode } from "react";

// Minimal React error boundary. Wraps any subtree so that a render-time
// exception in one component doesn't unmount everything else around it.
// Without this, a crash in a single CameraTile or ClipStage blanks the
// whole Home screen — which a non-technical user cannot recover from
// except by quitting and relaunching the app.
//
// React error boundaries MUST be class components; there is no hooks
// API for this yet.

interface Props {
  // Rendered when the child subtree throws. Kept as a render-function
  // so callers can do things like `<ErrorBoundary fallback={() => <Tile error />}>`
  // where the fallback has access to the surrounding component's props.
  fallback: (error: Error) => ReactNode;
  children: ReactNode;
  // Optional — called once with the caught error so the parent can log
  // or report it. Do not call setState from this handler; it fires
  // during React's commit phase.
  onError?: (error: Error, info: ErrorInfo) => void;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // console.error gives Ben-the-dev a stack trace in the Tauri devtools
    // without adding a runtime dependency. End users never see this.
    // eslint-disable-next-line no-console
    console.error("[ErrorBoundary]", error, info.componentStack);
    this.props.onError?.(error, info);
  }

  render(): ReactNode {
    if (this.state.error) {
      return this.props.fallback(this.state.error);
    }
    return this.props.children;
  }
}
