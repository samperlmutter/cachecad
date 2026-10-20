export const PREVIEW_BYTE_LIMIT = 512 * 1024 * 1024;

// StrictMode can start the same preview twice. Share only an active read:
// settled buffers may have been transferred to a worker and detached.
export function sharePendingPreview(load: () => Promise<ArrayBuffer>) {
  let pending: Promise<ArrayBuffer> | undefined;
  return () => {
    pending ??= load().finally(() => {
      pending = undefined;
    });
    return pending;
  };
}

export function previewError(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error.trim()) return error;
  return "Could not read this model.";
}
