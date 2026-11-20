import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, screen } from "@testing-library/react";
import { renderWithRouter } from "@app/test/renderWithRouter";
import { runAwaitingReview } from "@app/mocks/fixtures";
import { ApiError, getRunDetail } from "@app/services/api";
import { RunDetailPage } from "../RunDetailPage";

vi.mock("@app/services/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@app/services/api")>()),
  getRunDetail: vi.fn(),
}));

// The review buttons are enabled only for a run that names its artifact revision.
const atReviewGate = { ...runAwaitingReview, artifactRevision: "a".repeat(64) };
const poll = vi.mocked(getRunDetail);
const nextPoll = () => act(async () => { await vi.advanceTimersByTimeAsync(2000); });

async function renderAtReviewGate() {
  poll.mockResolvedValue(atReviewGate);
  renderWithRouter(<RunDetailPage />, { routePath: "/runs/:runId", initialPath: "/runs/run-review" });
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  fireEvent.click(screen.getByRole("button", { name: /request changes/i }));
  fireEvent.change(screen.getByLabelText("Overall comment"), { target: { value: "Reduce the starting dose." } });
}

describe("RunDetailPage polling errors", () => {
  beforeEach(() => { vi.useFakeTimers(); poll.mockReset(); });
  afterEach(() => { vi.useRealTimers(); });

  it("keeps the review form and the unsent comment through a retryable error", async () => {
    await renderAtReviewGate();

    poll.mockRejectedValueOnce(new ApiError(500, "Service unavailable"));
    await nextPoll();
    expect(screen.getByText("Connection problem. Retrying…")).toBeInTheDocument();
    expect(screen.getByLabelText("Overall comment")).toHaveValue("Reduce the starting dose.");
    expect(screen.getByText("Achieve HbA1c < 7%")).toBeInTheDocument();
    expect(screen.queryByText("Service unavailable")).not.toBeInTheDocument();

    poll.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await nextPoll();
    expect(screen.getByLabelText("Overall comment")).toHaveValue("Reduce the starting dose.");

    await nextPoll();
    expect(screen.queryByText("Connection problem. Retrying…")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Overall comment")).toHaveValue("Reduce the starting dose.");
  });

  it.each([
    [401, "Session expired", "Session expired"],
    [403, "Forbidden", "This run is not part of your current patient session."],
    [404, "Not found", "This run is not part of your current patient session."],
  ])("replaces the page on a terminal %i even with run data", async (status, message, shown) => {
    await renderAtReviewGate();
    const calls = poll.mock.calls.length;

    poll.mockRejectedValueOnce(new ApiError(status, message));
    await nextPoll();
    expect(screen.getByRole("alert")).toHaveTextContent(shown);
    expect(screen.queryByLabelText("Overall comment")).not.toBeInTheDocument();

    // Terminal: polling stops.
    await nextPoll();
    expect(poll.mock.calls.length).toBe(calls + 1);
  });

  it("shows the error when no run has loaded yet, then recovers", async () => {
    poll.mockRejectedValueOnce(new ApiError(502, "Bad gateway"));
    poll.mockResolvedValue(atReviewGate);
    renderWithRouter(<RunDetailPage />, { routePath: "/runs/:runId", initialPath: "/runs/run-review" });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByRole("alert")).toHaveTextContent("Bad gateway");

    await nextPoll();
    expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument();
    expect(screen.queryByText("Bad gateway")).not.toBeInTheDocument();
  });
});
