import { describe, it, expect, vi } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ReviewPanel } from "../ReviewPanel";
import { carePlanView } from "@app/mocks/fixtures";

describe("ReviewPanel", () => {
  it("submits an approve decision", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<ReviewPanel reviewIteration={0} artifactRevision={"a".repeat(64)} carePlan={carePlanView} onSubmit={onSubmit} />);
    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ decision: "approve" }));
  });

  it("submits request_changes with a comment", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<ReviewPanel reviewIteration={0} artifactRevision={"a".repeat(64)} carePlan={carePlanView} onSubmit={onSubmit} />);
    await userEvent.click(screen.getByRole("button", { name: /request changes/i }));
    await userEvent.type(screen.getByLabelText(/overall comment/i), "Tighten the HbA1c target");
    await userEvent.click(screen.getByRole("button", { name: /submit changes/i }));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ decision: "request_changes", comment: "Tighten the HbA1c target" }),
    );
  });

  it("shows a persistent acknowledgment and hides the buttons after submit", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<ReviewPanel reviewIteration={0} artifactRevision={"a".repeat(64)} carePlan={carePlanView} onSubmit={onSubmit} />);
    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(await screen.findByText(/review submitted/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /request changes/i })).not.toBeInTheDocument();
  });

  it("surfaces the server error and re-enables the buttons on a failed submit", async () => {
    const onSubmit = vi
      .fn()
      .mockRejectedValue(new Error("Workflow engine temporarily unavailable — please try again."));
    render(<ReviewPanel reviewIteration={0} artifactRevision={"a".repeat(64)} carePlan={carePlanView} onSubmit={onSubmit} />);
    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(await screen.findByText(/temporarily unavailable/i)).toBeInTheDocument();
    const approve = screen.getByRole("button", { name: /approve/i });
    expect(approve).toBeEnabled();
  });

  it("disables the button while a submit is in flight", async () => {
    let resolve: () => void = () => {};
    const onSubmit = vi.fn(() => new Promise<void>((r) => { resolve = r; }));
    render(<ReviewPanel reviewIteration={0} artifactRevision={"a".repeat(64)} carePlan={carePlanView} onSubmit={onSubmit} />);
    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
    resolve();
    expect(await screen.findByText(/review submitted/i)).toBeInTheDocument();
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("binds the submission to the review round on screen", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<ReviewPanel artifactRevision={"a".repeat(64)} carePlan={carePlanView} reviewIteration={2} onSubmit={onSubmit} />);
    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ decision: "approve", reviewRound: 2 }),
    );
  });

  // fireEvent (sync) + act flush avoids userEvent's own timer waits clashing
  // with fake timers — this is the fiddly interaction the plan flagged.
  it("silently auto-retries the same round-bound action on a ~10s loop", async () => {
    vi.useFakeTimers();
    try {
      const onSubmit = vi.fn().mockResolvedValue(undefined);
      render(<ReviewPanel artifactRevision={"a".repeat(64)} carePlan={carePlanView} reviewIteration={1} onSubmit={onSubmit} />);
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /approve/i }));
      });
      expect(onSubmit).toHaveBeenCalledTimes(1); // initial submit
      expect(screen.getByText(/waiting for the pipeline/i)).toBeInTheDocument();

      // one retry per interval; only one timer is ever pending, so each tick
      // schedules the next.
      await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
      expect(onSubmit).toHaveBeenCalledTimes(2); // +10s

      await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
      expect(onSubmit).toHaveBeenCalledTimes(3); // +20s

      // every attempt carried the same round, and no manual affordance before
      // the max is reached
      expect(onSubmit).toHaveBeenLastCalledWith(
        expect.objectContaining({ decision: "approve", reviewRound: 1 }),
      );
      expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops after the max retries and offers a manual retry", async () => {
    vi.useFakeTimers();
    try {
      const onSubmit = vi.fn().mockResolvedValue(undefined);
      render(<ReviewPanel artifactRevision={"a".repeat(64)} carePlan={carePlanView} reviewIteration={0} onSubmit={onSubmit} />);
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /approve/i }));
      });

      // initial submit + MAX_REVIEW_RETRIES (3) auto-retries at 10/20/30s = 4
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      expect(onSubmit).toHaveBeenCalledTimes(4);
      expect(screen.getByText(/you can retry/i)).toBeInTheDocument();

      // no further auto-retries once the loop has given up
      await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
      expect(onSubmit).toHaveBeenCalledTimes(4);

      // the manual retry still works and re-submits the same round-bound action
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /retry/i }));
      });
      expect(onSubmit).toHaveBeenCalledTimes(5);
      expect(onSubmit).toHaveBeenLastCalledWith(
        expect.objectContaining({ decision: "approve", reviewRound: 0 }),
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops auto-retrying once the run leaves the gate (panel unmounts)", async () => {
    vi.useFakeTimers();
    try {
      const onSubmit = vi.fn().mockResolvedValue(undefined);
      const { unmount } = render(
        <ReviewPanel artifactRevision={"a".repeat(64)} carePlan={carePlanView} reviewIteration={0} onSubmit={onSubmit} />,
      );
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /approve/i }));
      });
      expect(onSubmit).toHaveBeenCalledTimes(1);

      unmount();
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      expect(onSubmit).toHaveBeenCalledTimes(1); // timers torn down on unmount
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops auto-retry and surfaces the error when an auto-retry fails", async () => {
    vi.useFakeTimers();
    try {
      const onSubmit = vi
        .fn()
        .mockResolvedValueOnce(undefined) // initial submit -> 202, enters submitted
        .mockRejectedValueOnce(
          new Error("Workflow engine temporarily unavailable — please try again."),
        ); // 10s auto-retry fails
      render(<ReviewPanel artifactRevision={"a".repeat(64)} carePlan={carePlanView} reviewIteration={0} onSubmit={onSubmit} />);
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /approve/i }));
      });

      await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
      // the failed retry drops us back to editing with the error surfaced
      expect(screen.getByText(/temporarily unavailable/i)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /approve/i })).toBeEnabled();
      const calls = onSubmit.mock.calls.length; // 2: initial + failed retry

      // no further auto-retries fire (the 20s/30s timers were torn down)
      await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
      expect(onSubmit).toHaveBeenCalledTimes(calls);
    } finally {
      vi.useRealTimers();
    }
  });

  it("remounts fresh when the round advances (no state bleed across gates)", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(
      <ReviewPanel artifactRevision={"a".repeat(64)} key={0} carePlan={carePlanView} reviewIteration={0} onSubmit={onSubmit} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(await screen.findByText(/review submitted/i)).toBeInTheDocument();

    // Round N+1 gate arms: RunDetailPage keys the panel by reviewIteration, so a
    // new key remounts a fresh idle panel — round N's submitted state is gone.
    rerender(
      <ReviewPanel artifactRevision={"b".repeat(64)} key={1} carePlan={carePlanView} reviewIteration={1} onSubmit={onSubmit} />,
    );
    expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument();
    expect(screen.queryByText(/review submitted/i)).not.toBeInTheDocument();
  });
});

it("binds the review to the displayed revision without accepting a clinician identity", async () => {
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  render(<ReviewPanel carePlan={carePlanView} reviewIteration={2} artifactRevision="synthetic-revision" onSubmit={onSubmit} />);
  await userEvent.click(screen.getByRole("button", { name: /approve/i }));
  expect(onSubmit).toHaveBeenCalledWith({ decision: "approve", reviewRound: 2, artifactRevision: "synthetic-revision" });
  expect(screen.queryByLabelText(/clinician name/i)).not.toBeInTheDocument();
});


it("disables approval until the current artifact binding is available", () => {
  render(<ReviewPanel carePlan={carePlanView} onSubmit={vi.fn()} />);
  expect(screen.getByRole("button", { name: /^approve$/i })).toBeDisabled();
});
