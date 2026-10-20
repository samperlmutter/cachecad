import { describe, expect, it } from "vitest";
import { previewError, sharePendingPreview } from "./preview";

it("shares concurrent preview reads but reads fresh bytes after settlement", async () => {
  let finish!: (bytes: ArrayBuffer) => void;
  let reads = 0;
  const load = sharePendingPreview(() => {
    reads++;
    return new Promise<ArrayBuffer>((resolve) => {
      finish = resolve;
    });
  });
  const first = load();
  const duplicate = load();
  expect(reads).toBe(1);
  finish(new ArrayBuffer(4));
  expect(await duplicate).toBe(await first);
  const reopened = load();
  expect(reads).toBe(2);
  finish(new ArrayBuffer(8));
  expect((await reopened).byteLength).toBe(8);
});

it("retries a preview read after a failed request", async () => {
  let reads = 0;
  const load = sharePendingPreview(async () => {
    if (++reads === 1) throw new Error("Download cancelled");
    return new ArrayBuffer(4);
  });
  await expect(load()).rejects.toThrow("Download cancelled");
  expect((await load()).byteLength).toBe(4);
});

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
