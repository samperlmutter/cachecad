import { expect, test } from "@playwright/test";
import { statSync } from "node:fs";
import { basename } from "node:path";
// Opt-in: user-owned CAD stays outside the repository and CI artifacts.
const fixture = process.env.PERMAFROST_STEP_FIXTURE;
test("measure a representative private CAD preview", async ({ page }, info) => {
  test.skip(!fixture, "Set PERMAFROST_STEP_FIXTURE to a local STEP file.");
  test.setTimeout(240000);
  await page.addInitScript(() => {
    const state = { current: null as any, reports: [] as any[] };
    (window as any).cadBenchmark = state;
    const record = (name: string, extra = {}) => {
      if (state.current)
        state.current.events.push({
          name,
          ms: performance.now() - state.current.started,
          ...extra,
        });
    };
    document.addEventListener(
      "change",
      (event) => {
        if (
          !(event.target instanceof HTMLInputElement) ||
          !event.target.files?.length
        )
          return;
        state.current = {
          started: performance.now(),
          events: [],
          maxFrameGapMs: 0,
          longTasks: [],
        };
        state.reports.push(state.current);
        record("selected");
        let previous = performance.now();
        const current = state.current;
        const heartbeat = (now: number) => {
          if (state.current !== current || current.finished) return;
          current.maxFrameGapMs = Math.max(
            current.maxFrameGapMs,
            now - previous,
          );
          previous = now;
          requestAnimationFrame(heartbeat);
        };
        requestAnimationFrame(heartbeat);
      },
      true,
    );
    const read = File.prototype.arrayBuffer;
    File.prototype.arrayBuffer = async function () {
      record("file-read-start");
      const bytes = await read.call(this);
      record("file-read-end", { bytes: bytes.byteLength });
      return bytes;
    };
    const digest = SubtleCrypto.prototype.digest;
    SubtleCrypto.prototype.digest = async function (...args) {
      record("hash-start");
      const result = await digest.apply(this, args);
      record("hash-end");
      return result;
    };
    const WorkerClass = window.Worker;
    window.Worker = class extends WorkerClass {
      constructor(url: string | URL, options?: WorkerOptions) {
        super(url, options);
        if (!String(url).includes("occt-worker")) return;
        record("worker-start");
        this.addEventListener("message", ({ data }) => {
          if (data.phase) record(`worker-${data.phase}`);
          else if (data.error) record("worker-error", { error: data.error });
          else if (data.meshes) {
            record("worker-result", {
              bodies: data.meshes.length,
              triangles: data.meshes.reduce(
                (n: number, mesh: any) => n + mesh.index.length / 3,
                0,
              ),
              meshBytes: data.meshes.reduce(
                (n: number, mesh: any) =>
                  n +
                  mesh.position.byteLength +
                  mesh.index.byteLength +
                  (mesh.normal?.byteLength ?? 0),
                0,
              ),
            });
          }
        });
      }
    };
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries())
        if (
          state.current &&
          !state.current.finished &&
          entry.startTime >= state.current.started
        )
          state.current.longTasks.push({
            ms: entry.startTime - state.current.started,
            durationMs: entry.duration,
          });
    }).observe({ type: "longtask", buffered: true });
    new MutationObserver(() => {
      const current = state.current;
      if (!current || current.finished || current.ready) return;
      const ready = document.querySelector("[data-preview-ready]");
      if (!ready) return;
      current.ready = true;
      record("render-ready");
      current.renderer = ready.getAttribute("data-preview-renderer");
      requestAnimationFrame(() =>
        requestAnimationFrame(() => {
          record("painted");
          current.finished = true;
        }),
      );
    }).observe(document, { subtree: true, attributes: true, childList: true });
  });
  await page.goto("/?preview-test=1");
  await expect(page.getByLabel("Model fixture")).toBeVisible();
  let failure: unknown;
  try {
    for (let opening = 0; opening < 3; opening++) {
      await page.getByLabel("Model fixture").setInputFiles(fixture!);
      await expect(page.locator("[data-preview-ready]")).toBeVisible({
        timeout: 65000,
      });
      await expect
        .poll(() =>
          page.evaluate(() => (window as any).cadBenchmark.current.finished),
        )
        .toBe(true);
      await expect(page.getByRole("alert")).toHaveCount(0);
      await page.getByLabel("Model fixture").setInputFiles([]);
      await expect(page.locator("[data-preview-ready]")).toHaveCount(0);
    }
  } catch (error) {
    failure = error;
  }
  const report = {
    file: basename(fixture!),
    bytes: statSync(fixture!).size,
    environment:
      "Chromium, local file, software WebGL; excludes Drive download and screenshots",
    runs: await page.evaluate(() => (window as any).cadBenchmark.reports),
    error: failure ? String(failure) : null,
  };
  await info.attach("cad-benchmark", {
    body: JSON.stringify(report, null, 2),
    contentType: "application/json",
  });
  console.log(JSON.stringify(report));
  if (failure) throw failure;
});
