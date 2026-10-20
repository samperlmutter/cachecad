import { describe, expect, it } from "vitest";
import { previewError } from "./preview";

describe("Preview failure reasons", () => {
  it("preserves native IPC error strings instead of replacing their reason", () => {
    const reason =
      "This file exceeds the 512 MiB preview limit. Export a lighter model.";
    expect(previewError(reason)).toBe(reason);
    expect(
      previewError(
        "This file is not on your computer yet. Download it before previewing.",
      ),
    ).toBe(
      "This file is not on your computer yet. Download it before previewing.",
    );
  });

  it("preserves converter errors and gives a fallback for unknown failures", () => {
    expect(
      previewError(new Error("OpenCascade could not read this model.")),
    ).toBe("OpenCascade could not read this model.");
    expect(previewError(null)).toBe("Could not read this model.");
    expect(previewError("")).toBe("Could not read this model.");
  });
});
