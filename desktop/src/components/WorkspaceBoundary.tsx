import { Component, Fragment, type ReactNode } from "react";
import { api } from "../api";

/** Keep an unexpected rendering failure from leaving an unusable white window. */
export default class WorkspaceBoundary extends Component<
  { children: ReactNode },
  { failed: boolean; attempt: number; recovering: boolean }
> {
  state = { failed: false, attempt: 0, recovering: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  recover = async () => {
    this.setState({ recovering: true });
    // The native executor owns transfer state. Ask it to settle safely before
    // reopening the interface; uncertain writes remain reviewable in Activity.
    await api.cancel().catch(() => {});
    this.setState(({ attempt }) => ({
      failed: false,
      attempt: attempt + 1,
      recovering: false,
    }));
  };
  render() {
    if (!this.state.failed)
      return (
        <Fragment key={this.state.attempt}>{this.props.children}</Fragment>
      );
    return (
      <main className="workspace-recovery" role="alert">
        <span className="eyebrow">PERMAFROST</span>
        <h1>Let’s reopen your workspace</h1>
        <p>
          The interface ran into an unexpected problem. Completed work is saved.
          After reopening, review Activity before retrying a transfer.
        </p>
        <button
          className="primary"
          disabled={this.state.recovering}
          onClick={() => void this.recover()}
        >
          {this.state.recovering ? "Reopening…" : "Reopen workspace"}
        </button>
      </main>
    );
  }
}
