import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type {
  AutomationAcknowledgement,
  ParameterEdit,
  ReviewAction,
  ReviewFeedbackItem,
  RunDetail,
} from '../api/types';

type ReviewExtras = {
  parameterEdits?: ParameterEdit[];
  acknowledgements?: AutomationAcknowledgement[];
};

function withReviewExtras(action: ReviewAction['action'], extras?: ReviewExtras): ReviewAction {
  return {
    action,
    ...(extras?.parameterEdits?.length ? { parameterEdits: extras.parameterEdits } : {}),
    ...(extras?.acknowledgements?.length ? { acknowledgements: extras.acknowledgements } : {}),
  };
}

export function useReviewGate(run: RunDetail) {
  const queryClient = useQueryClient();
  const gate = run.awaitingReview;

  const approveMutation = useMutation({
    mutationFn: (extras?: ReviewExtras) => {
      if (!gate) throw new Error('No review gate active');
      const action = withReviewExtras('approve', extras);
      return api.submitReview(run.id, gate, action);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['run', run.id] });
    },
  });

  const requestChangesMutation = useMutation({
    mutationFn: (params: {
      feedback: ReviewFeedbackItem[];
      overallComment?: string;
      parameterEdits?: ParameterEdit[];
      acknowledgements?: AutomationAcknowledgement[];
    }) => {
      if (!gate) throw new Error('No review gate active');
      const action: ReviewAction = {
        action: 'request_changes',
        feedback: params.feedback,
        overallComment: params.overallComment,
        parameterEdits: params.parameterEdits,
        acknowledgements: params.acknowledgements,
      };
      return api.submitReview(run.id, gate, action);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['run', run.id] });
    },
  });

  return {
    gate,
    reviewIteration: run.reviewIteration ?? 1,
    previousFeedback: run.previousFeedback,
    isReviewActive: !!gate,
    approveMutation,
    requestChangesMutation,
  };
}
