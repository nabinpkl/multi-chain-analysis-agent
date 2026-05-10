/**
 * Minimal Server-Sent Events parser for `fetch()` streaming responses.
 *
 * The browser's native `EventSource` API is GET-only, which forced the
 * agent service into a two-step `POST /agent/ask` + `GET /agent/stream/{id}`
 * handoff (the per-POST `session_id` token bridged the two). The cleanup
 * pass collapses that into one streaming POST; this parser consumes the
 * POST response body the same way EventSource consumed a GET.
 *
 * Spec reference: https://html.spec.whatwg.org/multipage/server-sent-events.html
 *
 * Supported subset (everything the agent-service actually emits):
 *   - `event: <name>` sets the dispatched event name; default is "message".
 *   - `data: <payload>` accumulates into the event's data. Multiple
 *     `data:` lines are joined with `\n` per the spec.
 *   - Empty line dispatches the event.
 *   - Lines starting with `:` are comments (keep-alives); ignored.
 *
 * Not supported (we don't emit these on the agent path):
 *   - `id:` (event id; for reconnect/Last-Event-ID)
 *   - `retry:` (retry interval hint)
 *
 * Line endings: handles `\n`, `\r\n`, and `\r` per the spec.
 */

export interface SseEvent {
  /** Event name from the `event:` field, or "message" if absent. */
  event: string;
  /** Joined payload from `data:` field(s). */
  data: string;
}

/**
 * Read SSE frames from a `ReadableStream<Uint8Array>` (typically a
 * `fetch()` response's `body`). Yields one `SseEvent` per dispatched
 * event. Returns when the stream closes; callers should signal
 * cancellation via the upstream `AbortController.signal` passed to
 * `fetch()`.
 */
export async function* parseSseStream(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<SseEvent, void, undefined> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let eventName = "";
  let dataLines: string[] = [];

  function flush(): SseEvent | null {
    if (dataLines.length === 0 && eventName === "") return null;
    const event = eventName || "message";
    const data = dataLines.join("\n");
    eventName = "";
    dataLines = [];
    return { event, data };
  }

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // Normalize line endings (\r\n -> \n, lone \r -> \n) before
      // splitting so multi-line `data:` payloads round-trip cleanly
      // regardless of which line terminator the server emits.
      buffer = buffer.replace(/\r\n?/g, "\n");

      let newlineIdx: number;
      while ((newlineIdx = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, newlineIdx);
        buffer = buffer.slice(newlineIdx + 1);

        if (line === "") {
          // Empty line dispatches.
          const evt = flush();
          if (evt) yield evt;
          continue;
        }
        if (line.startsWith(":")) {
          // Comment / keep-alive; ignore.
          continue;
        }

        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        // Per spec, a single leading space after the colon is stripped.
        const value =
          colon === -1
            ? ""
            : line.slice(colon + 1).replace(/^ /, "");

        if (field === "event") {
          eventName = value;
        } else if (field === "data") {
          dataLines.push(value);
        }
        // Other fields (id, retry) and unknown fields: ignore.
      }
    }
    // Stream closed; emit any pending event without trailing blank line.
    const evt = flush();
    if (evt) yield evt;
  } finally {
    reader.releaseLock();
  }
}
