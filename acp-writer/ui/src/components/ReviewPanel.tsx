import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  Flex,
  FlexItem,
  Spinner,
  Stack,
  StackItem,
  TextArea,
  TextInput,
} from "@patternfly/react-core";
import type { CarePlanView, ReviewAction } from "@app/api/models";
import { GoalCard } from "./GoalCard";
import { ActivityCard } from "./ActivityCard";
import { ConflictAlert, orderConflicts } from "./ConflictAlert";

// Silent auto-retry cadence for a submitted review while it waits for the engine
// to consume the event (see the loop in ReviewPanel). Tweak freely: round-
// binding (P1) makes every retry either the first-consumed event or an engine-
// discarded duplicate — never a double-apply. After MAX_REVIEW_RETRIES the panel
// stops auto-retrying and hands off to the manual retry affordance.
const REVIEW_RETRY_INTERVAL_MS = 10_000;
const MAX_REVIEW_RETRIES = 3;

interface ReviewPanelProps {
  carePlan: CarePlanView;
  reviewIteration?: number;
  previousFeedback?: ReviewAction | null;
  onSubmit: (action: ReviewAction) => Promise<void>;
}

export function ReviewPanel({
  carePlan,
  reviewIteration,
  previousFeedback,
  onSubmit,
}: ReviewPanelProps) {
  const [mode, setMode] = useState<"idle" | "changes">("idle");
  const [clinician, setClinician] = useState("");
  const [comment, setComment] = useState("");
  // Submission lifecycle: editing -> submitting -> submitted. On a failed submit
  // we return to editing and surface `error` so the clinician can retry
  // deliberately. Once submitted, the buttons are gone so a duplicate (or
  // changed-decision) submit is impossible; the panel unmounts on its own when a
  // later poll sees the run leave the gate, and remounts fresh when the round
  // advances (RunDetailPage keys it by reviewIteration).
  const [phase, setPhase] = useState<"editing" | "submitting" | "submitted">("editing");
  const [error, setError] = useState<string | null>(null);
  // Set once the silent auto-retry loop (P6, below) exhausts MAX_REVIEW_RETRIES
  // with the run still at this same gate: by then the engine either consumed the
  // event or the run genuinely moved on, so we stop retrying and offer a neutral
  // MANUAL retry rather than an alarm. Retry is safe by construction: round-
  // binding makes a duplicate either the first-consumed event or an engine-
  // discarded stale one — never a double-apply, never approval of unseen content.
  const [stalled, setStalled] = useState(false);
  const [lastAction, setLastAction] = useState<ReviewAction | null>(null);

  const goals = carePlan.goals ?? [];
  const activities = carePlan.activities ?? [];
  const conflicts = carePlan.conflicts ?? [];

  const submitting = phase === "submitting";

  const submit = async (action: ReviewAction) => {
    setLastAction(action);
    setError(null);
    setStalled(false);
    setPhase("submitting");
    try {
      await onSubmit(action);
      setPhase("submitted");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to submit review. Please try again.");
      setPhase("editing");
    }
  };

  // P6: silent auto-retry. The UI can render a new round a few seconds before
  // the engine arms its gate (the data-index leads on content, lags on state);
  // a submission in that window is dropped by at-most-once delivery. While still
  // at this same gate/round, a self-rescheduling loop waits
  // REVIEW_RETRY_INTERVAL_MS, silently re-submits the same action, and schedules
  // the next wait — up to MAX_REVIEW_RETRIES. Exactly ONE timer is ever pending.
  // The loop ends when any one of these happens:
  //   - the run leaves the gate: this panel unmounts (or remounts by
  //     reviewIteration), so cleanup cancels the loop — nothing else to do;
  //   - a retry errors: fall back to the manual path (setError + `editing`),
  //     the "only auto-retry while the last attempt returned 202" guard;
  //   - the retries are exhausted: `stalled` hands off to the manual affordance.
  // Each retry is safe by construction (round-binding, P1): consumed-first or
  // engine-discarded, never a double-apply. Deps are [phase] on purpose:
  // onSubmit is a fresh closure on every poll re-render, so depending on it
  // would restart the loop each poll and the retries would never fire.
  useEffect(() => {
    if (phase !== "submitted" || !lastAction) return;
    let cancelled = false;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = () => {
      timer = setTimeout(async () => {
        if (cancelled) return;
        attempts += 1;
        try {
          await onSubmit(lastAction);
        } catch (e) {
          if (!cancelled) {
            setError(e instanceof Error ? e.message : "Failed to submit review. Please try again.");
            setPhase("editing");
          }
          return;
        }
        if (cancelled) return;
        if (attempts >= MAX_REVIEW_RETRIES) {
          setStalled(true);
          return;
        }
        schedule();
      }, REVIEW_RETRY_INTERVAL_MS);
    };
    schedule();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase]);

  return (
    <Card>
      <CardTitle>
        Care-plan review{typeof reviewIteration === "number" ? ` — round ${reviewIteration + 1}` : ""}
      </CardTitle>
      <CardBody>
        <Stack hasGutter>
          {previousFeedback?.comment && (
            <StackItem>
              <em>Previously requested:</em> {previousFeedback.comment}
            </StackItem>
          )}
          {orderConflicts(conflicts).map((c) => (
            <StackItem key={c.id}><ConflictAlert conflict={c} /></StackItem>
          ))}
          <StackItem>
            <strong>Goals ({goals.length})</strong>
            <Stack hasGutter>
              {goals.map((g) => <StackItem key={g.id}><GoalCard goal={g} /></StackItem>)}
            </Stack>
          </StackItem>
          <StackItem>
            <strong>Activities ({activities.length})</strong>
            <Stack hasGutter>
              {activities.map((a) => (
                <StackItem key={a.id}>
                  <ActivityCard activity={a} />
                </StackItem>
              ))}
            </Stack>
          </StackItem>

          {phase === "submitted" ? (
            <StackItem>
              {stalled ? (
                <Alert
                  variant="info"
                  isInline
                  title="Still waiting on the pipeline… you can retry if this persists."
                  actionLinks={
                    <Button
                      variant="link"
                      isInline
                      onClick={() => lastAction && submit(lastAction)}
                    >
                      Retry
                    </Button>
                  }
                />
              ) : (
                <Alert
                  variant="info"
                  isInline
                  title="Review submitted — waiting for the pipeline to pick it up…"
                  customIcon={<Spinner size="md" />}
                />
              )}
            </StackItem>
          ) : (
          <>
          {error && (
            <StackItem>
              <Alert variant="danger" isInline title={error} />
            </StackItem>
          )}

          <StackItem>
            <TextInput
              aria-label="Clinician name"
              placeholder="Clinician name"
              value={clinician}
              isDisabled={submitting}
              onChange={(_e, v) => setClinician(v)}
            />
          </StackItem>

          {mode === "changes" && (
            <StackItem>
              <TextArea
                aria-label="Overall comment"
                placeholder="What should change?"
                value={comment}
                isDisabled={submitting}
                onChange={(_e, v) => setComment(v)}
              />
            </StackItem>
          )}

          <StackItem>
            <Flex gap={{ default: "gapSm" }}>
              {mode === "idle" ? (
                <>
                  <FlexItem>
                    <Button
                      variant="primary"
                      isLoading={submitting}
                      isDisabled={submitting}
                      onClick={() =>
                        submit({
                          decision: "approve",
                          clinician: clinician || undefined,
                          reviewRound: reviewIteration,
                        })
                      }
                    >
                      Approve
                    </Button>
                  </FlexItem>
                  <FlexItem>
                    <Button
                      variant="secondary"
                      isDisabled={submitting}
                      onClick={() => setMode("changes")}
                    >
                      Request changes
                    </Button>
                  </FlexItem>
                </>
              ) : (
                <>
                  <FlexItem>
                    <Button
                      variant="primary"
                      isLoading={submitting}
                      isDisabled={submitting || !comment.trim()}
                      onClick={() =>
                        submit({
                          decision: "request_changes",
                          clinician: clinician || undefined,
                          comment: comment.trim(),
                          reviewRound: reviewIteration,
                        })
                      }
                    >
                      Submit changes
                    </Button>
                  </FlexItem>
                  <FlexItem>
                    <Button variant="link" isDisabled={submitting} onClick={() => setMode("idle")}>
                      Cancel
                    </Button>
                  </FlexItem>
                </>
              )}
            </Flex>
          </StackItem>
          </>
          )}
        </Stack>
      </CardBody>
    </Card>
  );
}
