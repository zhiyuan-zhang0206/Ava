import type { AgentMessageEnqueued, ContentBlock } from "./types";

const MESSAGE_POST_TIMEOUT_MS = 10_000;
const MESSAGE_RECONCILE_TIMEOUT_MS = 3_000;
const MESSAGE_RECONCILE_ATTEMPTS = 3;
const MESSAGE_RECONCILE_BASE_DELAY_MS = 500;

export type TimedJsonRequest = <T>(
  path: string,
  init: RequestInit,
  timeoutMs: number,
) => Promise<T>;

export class MessageDeliveryUnknownError extends Error {
  readonly clientMessageId: string;

  constructor(clientMessageId: string) {
    super(
      `Message delivery is still unconfirmed (id ${clientMessageId}); ` +
        "retry the same message to reconcile it, or explicitly send another.",
    );
    this.name = "MessageDeliveryUnknownError";
    this.clientMessageId = clientMessageId;
  }
}

const messageInit = (content: string | ContentBlock[], clientMessageId: string): RequestInit => ({
  method: "POST",
  headers: {
    "content-type": "application/json",
    "Idempotency-Key": clientMessageId,
  },
  body: JSON.stringify({ content, source: "user" }),
});

const wait = (milliseconds: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));

export async function sendMessageWithReconciliation(
  agentId: number,
  content: string | ContentBlock[],
  clientMessageId: string,
  request: TimedJsonRequest,
  isAmbiguous: (error: unknown) => boolean,
): Promise<AgentMessageEnqueued> {
  const path = `/api/agents/${agentId}/messages`;
  const init = messageInit(content, clientMessageId);
  try {
    return await request<AgentMessageEnqueued>(path, init, MESSAGE_POST_TIMEOUT_MS);
  } catch (error: unknown) {
    if (!isAmbiguous(error)) throw error;
  }

  let resent = false;
  for (let attempt = 0; attempt < MESSAGE_RECONCILE_ATTEMPTS; attempt += 1) {
    try {
      return await request<AgentMessageEnqueued>(
        `${path}/reconcile`,
        init,
        MESSAGE_RECONCILE_TIMEOUT_MS,
      );
    } catch (error: unknown) {
      if (hasHttpStatus(error, 404)) {
        if (!resent) {
          // The first POST may not have reached its INSERT. Re-submit exactly
          // once with the SAME key; if this request is also ambiguous, every
          // later 404 remains ambiguous until the reconciliation deadline.
          resent = true;
          try {
            return await request<AgentMessageEnqueued>(path, init, MESSAGE_POST_TIMEOUT_MS);
          } catch (resendError: unknown) {
            if (!isAmbiguous(resendError)) throw resendError;
          }
        }
      } else if (!isAmbiguous(error)) {
        throw error;
      }
    }
    if (attempt < MESSAGE_RECONCILE_ATTEMPTS - 1) {
      await wait(MESSAGE_RECONCILE_BASE_DELAY_MS * 2 ** attempt);
    }
  }
  throw new MessageDeliveryUnknownError(clientMessageId);
}

function hasHttpStatus(error: unknown, status: number): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    error.status === status
  );
}
