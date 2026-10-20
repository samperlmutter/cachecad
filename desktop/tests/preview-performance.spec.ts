import { expect, test } from "@playwright/test";
import { fileURLToPath } from "node:url";

test("reopening unchanged CAD reuses the mesh after disposing the first viewer", async ({
  page,
}, testInfo) => {
  test.setTimeout(120000);
  await page.addInitScript(() => {
    const OriginalWorker = window.Worker;
    const state = window as typeof window & { cadImports: number };
    state.cadImports = 0;
    window.Worker = class extends OriginalWorker {
      constructor(url: string | URL, options?: WorkerOptions) {
        super(url, options);
        if (String(url).endsWith("/occt-worker.js")) state.cadImports++;
      }
    };
  });
  await page.goto("/?preview-test=1");
  const fixture =
    process.env.PERMAFROST_STEP_FIXTURE ??
    fileURLToPath(new URL("./fixtures/cube.step", import.meta.url));
  const input = page.getByLabel("Model fixture");
  let firstCaption = "";
  let firstImage: Buffer | undefined;
  for (let opening = 0; opening < 2; opening++) {
    const started = Date.now();
    await input.setInputFiles(fixture);
    await expect(page.locator("canvas[data-preview-ready]")).toBeVisible({
      timeout: 65000,
    });
    await expect(page.getByRole("alert")).toHaveCount(0);
    const caption = await page.locator(".preview-caption").textContent();
    // Reset-camera clicks leave the pointer over its overlay on reopening.
    // Compare the model with the same toolbar hover state on both openings.
    await page.mouse.move(0, 0);
    const image = await page.locator("canvas").screenshot();
    await testInfo.attach(`opening-${opening + 1}`, {
      body: image,
      contentType: "image/png",
    });
    if (opening === 0) {
      firstCaption = caption!;
      firstImage = image;
    } else {
      expect(caption).toBe(firstCaption);
      expect(image.equals(firstImage!)).toBe(true);
    }
    console.log(
      `CAD opening ${opening + 1}: ${Date.now() - started} ms; ${caption}`,
    );
    await page.getByRole("button", { name: "Toggle wireframe" }).click();
    await expect(
      page.getByRole("button", { name: "Toggle wireframe" }),
    ).toHaveAttribute("aria-pressed", "true");
    await page.getByRole("button", { name: "Reset camera" }).click();
    await input.setInputFiles([]);
    await expect(page.locator("canvas")).toHaveCount(0);
  }
  expect(
    await page.evaluate(
      () => (window as typeof window & { cadImports: number }).cadImports,
    ),
  ).toBe(1);
});

test("shows reader startup and elapsed time while the CAD engine is loading", async ({
  page,
}) => {
  let releaseReader!: () => void;
  const readerGate = new Promise<void>((resolve) => {
    releaseReader = resolve;
  });
  await page.route("**/occt-worker.js", async (route) => {
    await readerGate;
    await route.continue();
  });
  await page.goto("/?preview-test=1");
  await page
    .getByLabel("Model fixture")
    .setInputFiles(
      fileURLToPath(new URL("./fixtures/cube.step", import.meta.url)),
    );
  try {
    await expect(page.locator(".preview-message")).toContainText(
      "Starting OpenCascade…",
    );
    await expect(page.locator(".preview-message")).toContainText(/\ds elapsed/);
  } finally {
    releaseReader();
  }
  await expect(page.locator("canvas[data-preview-ready]")).toBeVisible();
});
