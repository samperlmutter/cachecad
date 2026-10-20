import { expect, test } from "@playwright/test";

test("native STEP previews render returned GLB, reload versions, and cancel closed jobs", async ({
  page,
}) => {
  await page.addInitScript((glb) => {
    const global = window as any;
    const fixture = {
      reads: 0,
      version: "1",
      active: false,
      cancelled: [] as string[],
      pending: false,
    };
    global.remotePreviewFixture = fixture;
    global.isTauri = true;
    global.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener() {} };
    global.__TAURI_INTERNALS__ = {
      transformCallback() {
        return 1;
      },
      async invoke(command: string, args: any) {
        if (command === "plugin:event|listen") return 1;
        if (command === "bootstrap")
          return {
            projects: [
              {
                id: "qa",
                name: "Remote preview",
                localRoot: "/qa",
                driveFolderId: "qa",
                readOnly: false,
              },
            ],
            settings: { checkUpdates: false },
            account: { subject: "qa", name: "QA", email: "qa@example.test" },
            googleConfigured: true,
            updaterConfigured: false,
            activity: [],
            version: "2.0.0-alpha.1",
            accountError: null,
            previewCacheError: "Could not clean preview cache for QA: fixture",
          };
        if (command === "list_local_files") return {};
        if (command === "scan_project")
          return {
            id: fixture.version,
            projectId: "qa",
            createdAt: Date.now() / 1000,
            actions: [],
            local: {},
            folders: { "": "qa" },
            remote: {
              "Remote.step": {
                path: "Remote.step",
                size: 128,
                md5: fixture.version,
                modified: "same",
                remoteId: "remote",
                version: fixture.version,
                modifiedBy: null,
              },
            },
          };
        if (command === "read_preview")
          throw Error("Native STEP must bypass the WASM reader");
        if (command === "cancel_native_preview") {
          fixture.cancelled.push(args.jobId);
          return;
        }
        if (command === "read_native_preview") {
          if (fixture.active)
            throw new Error("Another operation is still running.");
          fixture.active = true;
          fixture.reads++;
          await new Promise((resolve) =>
            setTimeout(resolve, fixture.pending ? 10000 : 100),
          );
          fixture.active = false;
          return Uint8Array.from(atob(glb), (c) => c.charCodeAt(0)).buffer;
        }
      },
    };
  }, triangleGlb());
  await page.goto("/");
  await expect(page.locator(".scan-time")).toContainText("Scanned");
  await expect(page.getByRole("alert")).toContainText(
    "Could not clean preview cache for QA: fixture",
  );
  await page.getByRole("button", { name: "Dismiss message" }).click();
  await page.getByRole("tab", { name: /All files/ }).click();
  await page.getByRole("button", { name: "Remote.step Project root" }).click();
  await expect(page.locator("canvas[data-preview-ready]")).toBeVisible();
  expect(
    await page.evaluate(() => (window as any).remotePreviewFixture.reads),
  ).toBe(1);
  await page.evaluate(() => {
    (window as any).remotePreviewFixture.version = "2";
  });
  await page
    .getByRole("button", { name: "Scan for changes", exact: true })
    .click();
  await expect
    .poll(() => page.evaluate(() => (window as any).remotePreviewFixture.reads))
    .toBe(2);
  await expect(page.locator("canvas[data-preview-ready]")).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.locator("canvas[data-preview-ready]")).toHaveAttribute(
    "data-preview-reader",
    "native-opencascade",
  );
  await page.evaluate(() => {
    (window as any).remotePreviewFixture.pending = true;
    (window as any).remotePreviewFixture.version = "3";
  });
  await page
    .getByRole("button", { name: "Scan for changes", exact: true })
    .click();
  await expect
    .poll(() => page.evaluate(() => (window as any).remotePreviewFixture.reads))
    .toBe(3);
  const before = await page.evaluate(
    () => (window as any).remotePreviewFixture.cancelled.length,
  );
  await page.getByRole("button", { name: "Close preview" }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () => (window as any).remotePreviewFixture.cancelled.length,
      ),
    )
    .toBeGreaterThan(before);
  await expect(page.locator("canvas[data-preview-ready]")).toHaveCount(0);
});

function triangleGlb(): string {
  const geometry = Buffer.alloc(36);
  [0, 0, 0, 1, 0, 0, 0, 1, 0].forEach((n, i) =>
    geometry.writeFloatLE(n, i * 4),
  );
  const document = {
    asset: { version: "2.0" },
    scene: 0,
    scenes: [{ nodes: [0] }],
    nodes: [{ mesh: 0 }],
    meshes: [{ primitives: [{ attributes: { POSITION: 0 } }] }],
    buffers: [{ byteLength: 36 }],
    bufferViews: [{ buffer: 0, byteOffset: 0, byteLength: 36 }],
    accessors: [
      {
        bufferView: 0,
        componentType: 5126,
        count: 3,
        type: "VEC3",
        min: [0, 0, 0],
        max: [1, 1, 0],
      },
    ],
  };
  const raw = JSON.stringify(document);
  const json = Buffer.from(
    raw.padEnd(Math.ceil(Buffer.byteLength(raw) / 4) * 4, " "),
  );
  const data = Buffer.alloc(28 + json.length + geometry.length);
  data.writeUInt32LE(0x46546c67, 0);
  data.writeUInt32LE(2, 4);
  data.writeUInt32LE(data.length, 8);
  data.writeUInt32LE(json.length, 12);
  data.writeUInt32LE(0x4e4f534a, 16);
  json.copy(data, 20);
  data.writeUInt32LE(geometry.length, 20 + json.length);
  data.writeUInt32LE(0x004e4942, 24 + json.length);
  geometry.copy(data, 28 + json.length);
  return data.toString("base64");
}
