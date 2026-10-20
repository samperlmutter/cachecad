export const PREVIEW_BYTE_LIMIT = 512 * 1024 * 1024;

export function previewError(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error.trim()) return error;
  return "Could not read this model.";
}
