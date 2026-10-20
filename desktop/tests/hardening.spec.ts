import { expect, test } from "@playwright/test";
import { fileURLToPath } from "node:url";

test("native installer readiness includes actual mesh-worker parsing", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const global = window as any;
    let next = 1;
    global.isTauri = true;
    global.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener() {} };
    global.__TAURI_INTERNALS__ = {
      transformCallback() {
        return next++;
      },
      async invoke(command: string, args: any) {
        if (command === "plugin:event|listen") return args.handler;
        if (command === "bootstrap")
          return {
            projects: [],
            account: null,
            settings: { checkUpdates: false },
            googleConfigured: false,
            updaterConfigured: false,
            activity: [],
            version: "2.0.0-alpha.1",
            accountError: null,
          };
        if (command === "native_ready") global.smokeEvidence = args.evidence;
      },
    };
  });
  await page.goto("/");
  await expect
    .poll(() => page.evaluate(() => (window as any).smokeEvidence?.reader))
    .toBe("mesh-worker");
  await expect(page.locator("[data-preview-ready]")).toBeVisible();
  expect(
    await page.evaluate(() => (window as any).smokeEvidence.modelRendered),
  ).toBe(true);
});

test("connecting a project performs one initial comparison", async ({
  page,
}) => {
  await page.goto("/?demo=1");
  await expect(page.locator(".scan-time")).toContainText("Scanned");
  await page.evaluate(async () => {
    const resource = performance
      .getEntriesByType("resource")
      .find((entry) => new URL(entry.name).pathname === "/src/api.ts");
    const { api } = await import(resource?.name ?? "/src/api.ts");
    const original = api.scan;
    (window as any).connectionScans = [];
    api.scan = async (id: string) => {
      (window as any).connectionScans.push(id);
      await new Promise((resolve) => setTimeout(resolve, 50));
      return original(id);
    };
  });
  await page.getByRole("button", { name: "Add project", exact: true }).click();
  const dialog = page.getByRole("dialog", {
    name: "Connect a project",
    exact: true,
  });
  await dialog
    .getByRole("textbox", { name: "Project name" })
    .fill("One comparison");
  await dialog.getByRole("button", { name: "Browse", exact: true }).click();
  await dialog
    .getByRole("textbox", { name: "Google Drive folder link" })
    .fill("qa-folder");
  await dialog
    .getByRole("button", { name: "Connect project", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "One comparison", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".scan-time")).toContainText("Scanned");
  expect(
    await page.evaluate(() => (window as any).connectionScans.length),
  ).toBe(1);
});

test("replacing a textured GLB disposes its texture resources", async ({
  page,
}) => {
  await page.goto("/?preview-test=1");
  const uri = await page.evaluate(() => {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    canvas.getContext("2d")!.fillRect(0, 0, 1, 1);
    return canvas.toDataURL("image/png");
  });
  const png = Buffer.from(uri.split(",")[1], "base64");
  const geometry = Buffer.alloc(60);
  [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1].forEach((value, i) =>
    geometry.writeFloatLE(value, i * 4),
  );
  const binary = Buffer.concat([geometry, png]);
  const document = {
    asset: { version: "2.0" },
    scene: 0,
    scenes: [{ nodes: [0] }],
    nodes: [{ mesh: 0 }],
    meshes: [
      {
        primitives: [
          { attributes: { POSITION: 0, TEXCOORD_0: 1 }, material: 0 },
        ],
      },
    ],
    buffers: [{ byteLength: binary.length }],
    bufferViews: [
      { buffer: 0, byteOffset: 0, byteLength: 36 },
      { buffer: 0, byteOffset: 36, byteLength: 24 },
      { buffer: 0, byteOffset: 60, byteLength: png.length },
    ],
    accessors: [
      {
        bufferView: 0,
        componentType: 5126,
        count: 3,
        type: "VEC3",
        min: [0, 0, 0],
        max: [1, 1, 0],
      },
      { bufferView: 1, componentType: 5126, count: 3, type: "VEC2" },
    ],
    images: [{ bufferView: 2, mimeType: "image/png" }],
    textures: [{ source: 0 }],
    materials: [
      {
        pbrMetallicRoughness: {
          baseColorTexture: { index: 0 },
          metallicFactor: 0,
        },
        doubleSided: true,
      },
    ],
  };
  const json = Buffer.from(
    JSON.stringify(document).padEnd(
      Math.ceil(Buffer.byteLength(JSON.stringify(document)) / 4) * 4,
      " ",
    ),
  );
  const padded = Buffer.concat([
    binary,
    Buffer.alloc((4 - (binary.length % 4)) % 4),
  ]);
  const container = Buffer.alloc(12 + 8 + json.length + 8 + padded.length);
  container.writeUInt32LE(0x46546c67, 0);
  container.writeUInt32LE(2, 4);
  container.writeUInt32LE(container.length, 8);
  container.writeUInt32LE(json.length, 12);
  container.writeUInt32LE(0x4e4f534a, 16);
  json.copy(container, 20);
  container.writeUInt32LE(padded.length, 20 + json.length);
  container.writeUInt32LE(0x004e4942, 24 + json.length);
  padded.copy(container, 28 + json.length);
  await page.getByLabel("Model fixture").setInputFiles({
    name: "textured.glb",
    mimeType: "model/gltf-binary",
    buffer: container,
  });
  await expect(page.locator("[data-preview-ready]")).toBeVisible();
  await page.evaluate(async () => {
    const resource = performance
      .getEntriesByType("resource")
      .find((entry) => new URL(entry.name).pathname.endsWith("/three.js"));
    if (!resource) throw Error("The production Three.js module was not loaded");
    const three = await import(resource.name);
    const dispose = three.Texture.prototype.dispose;
    (window as any).disposedTextures = 0;
    three.Texture.prototype.dispose = function () {
      (window as any).disposedTextures++;
      return dispose.call(this);
    };
  });
  await page
    .getByLabel("Model fixture")
    .setInputFiles(
      fileURLToPath(new URL("./fixtures/cube.step", import.meta.url)),
    );
  await expect(page.locator(".preview-caption")).toContainText("12 triangles");
  await expect
    .poll(() => page.evaluate(() => (window as any).disposedTextures))
    .toBeGreaterThan(0);
});

test("seeded navigation chaos stays usable through failed refreshes and offline events", async ({
  page,
}, info) => {
  test.setTimeout(180000);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/?demo=1");
  await expect(page.locator(".scan-time")).toContainText("Scanned");
  await page.evaluate(async () => {
    const resource = performance
      .getEntriesByType("resource")
      .find((entry) => new URL(entry.name).pathname === "/src/api.ts");
    const { api } = await import(resource?.name ?? "/src/api.ts");
    const scan = api.scan;
    let count = 0;
    api.scan = async (id: string) => {
      count++;
      await new Promise((resolve) =>
        setTimeout(resolve, 20 + (count % 4) * 10),
      );
      if (count % 5 === 0) throw new Error("Injected temporary Drive failure");
      return scan(id);
    };
  });
  let seed = 0x5045524d;
  const sequence: number[] = [];
  for (let step = 0; step < 60; step++) {
    seed ^= seed << 13;
    seed ^= seed >>> 17;
    seed ^= seed << 5;
    const action = (seed >>> 0) % 7;
    sequence.push(action);
    await page
      .locator('nav[aria-label="Main navigation"]')
      .getByRole("button", { name: /^Projects/ })
      .click();
    switch (action) {
      case 0:
        await page
          .getByRole("button", { name: "Settings", exact: true })
          .click();
        await page
          .getByRole("checkbox", { name: /Keep uploaded revisions/ })
          .setChecked(step % 2 === 0);
        await page
          .getByRole("button", { name: "Save preferences", exact: true })
          .click();
        break;
      case 1:
        await page
          .getByRole("button", { name: "Activity", exact: true })
          .click();
        break;
      case 2:
        await page
          .locator(".project-nav > button")
          .nth((seed >>> 4) % 3)
          .click();
        await page.getByRole("tab", { name: /All files/ }).click();
        break;
      case 3:
        await page
          .getByRole("button", { name: "Project options", exact: true })
          .click();
        await expect(
          page.getByRole("dialog", { name: "Project settings", exact: true }),
        ).toBeVisible();
        await page.keyboard.press("Escape");
        await expect(page.getByRole("dialog")).toHaveCount(0);
        break;
      case 4:
        await page
          .getByRole("textbox", { name: "Find a file", exact: true })
          .fill(["bracket", "零件", '"<>&', ""][step % 4]);
        break;
      case 5:
        await page.evaluate(() => window.dispatchEvent(new Event("offline")));
        await page
          .getByRole("button", { name: "Scan for changes", exact: true })
          .click();
        break;
      case 6:
        await page.getByRole("tab", { name: /Changes/ }).click();
        await page
          .locator(".summary-card")
          .nth(step % 3)
          .click();
        break;
    }
    await expect(page.locator(".app-shell")).toBeVisible();
    expect(errors, `seeded sequence: ${sequence.join(",")}`).toEqual([]);
  }
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Settings", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".project-nav > button")).toHaveCount(3);
  await expect(page.locator("canvas")).toHaveCount(0);
  await page.screenshot({
    path: info.outputPath("after-navigation-chaos.png"),
  });
});

for (const extension of ["stl", "obj"]) {
  test(`${extension} parsing runs in a disposable worker and rapid replacements leave no workers behind`, async ({
    page,
  }) => {
    await page.addInitScript(() => {
      const WorkerClass = window.Worker;
      const state = { created: 0, active: 0 };
      (window as any).meshWorkers = state;
      window.Worker = class extends WorkerClass {
        private ended = false;
        constructor(url: string | URL, options?: WorkerOptions) {
          super(url, options);
          state.created++;
          state.active++;
        }
        terminate() {
          if (!this.ended) {
            this.ended = true;
            state.active--;
          }
          super.terminate();
        }
      };
    });
    await page.goto("/?preview-test=1");
    const buffer = Buffer.from(
      extension === "obj"
        ? "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"
        : "solid triangle\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid triangle\n",
    );
    for (let i = 0; i < 20; i++)
      await page.getByLabel("Model fixture").setInputFiles({
        name: `replacement-${i}.${extension}`,
        mimeType: "application/octet-stream",
        buffer,
      });
    await expect(page.locator("[data-preview-ready]")).toBeVisible();
    await expect
      .poll(() => page.evaluate(() => (window as any).meshWorkers.created))
      .toBeGreaterThan(0);
    await expect
      .poll(() => page.evaluate(() => (window as any).meshWorkers.active))
      .toBe(0);
    await expect(page.locator(".preview-caption")).toContainText(
      "1 body · 1 triangle",
    );
  });
}

test("project options edits the connection without duplicating or removing it", async ({
  page,
}, info) => {
  await page.goto("/?demo=1");
  await expect(page.locator(".scan-time")).toContainText("Scanned");
  const count = await page.locator(".project-nav > button").count();
  await page
    .getByRole("button", { name: "Project options", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Project settings",
    exact: true,
  });
  await expect(dialog).toBeVisible();
  await dialog
    .getByRole("textbox", { name: "Project name", exact: true })
    .fill("Read-only review");
  await dialog
    .getByRole("checkbox", { name: /Make this project read-only/ })
    .check();
  await expect(
    dialog.getByRole("button", { name: "Browse", exact: true }),
  ).toBeDisabled();
  await page.screenshot({ path: info.outputPath("project-settings.png") });
  await dialog
    .getByRole("button", { name: "Save project", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: /Read-only review/ }),
  ).toBeVisible();
  await expect(page.locator(".project-nav > button")).toHaveCount(count);
  await page
    .getByRole("button", { name: "Project options", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Remove project", exact: true })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Remove this project?" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Keep project", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: /Read-only review/ }),
  ).toBeVisible();
});

for (const kind of ["empty", "non-finite"] as const) {
  test(`malformed ${kind} mesh is rejected and a valid preview still opens`, async ({
    page,
  }) => {
    await page.goto("/?preview-test=1");
    let buffer: Buffer;
    if (kind === "empty") buffer = Buffer.from("# No mesh geometry\n");
    else {
      buffer = Buffer.alloc(134);
      buffer.writeUInt32LE(1, 80);
      buffer.writeFloatLE(1, 92);
      buffer.writeFloatLE(Number.NaN, 96);
      buffer.writeFloatLE(1, 108);
      buffer.writeFloatLE(1, 124);
    }
    await page.getByLabel("Model fixture").setInputFiles({
      name: kind === "empty" ? "empty.obj" : "invalid.stl",
      mimeType: "application/octet-stream",
      buffer,
    });
    await expect(page.getByRole("alert")).toContainText(
      /no renderable triangles|invalid coordinates/i,
    );
    await expect(page.locator("[data-preview-ready]")).toHaveCount(0);
    await page
      .getByLabel("Model fixture")
      .setInputFiles(
        fileURLToPath(new URL("./fixtures/cube.step", import.meta.url)),
      );
    await expect(page.locator("[data-preview-ready]")).toBeVisible();
    await expect(page.locator(".preview-caption")).toContainText(
      "12 triangles",
    );
  });
}

test("an unexpected preview exception offers workspace recovery", async ({
  page,
}, info) => {
  await page.addInitScript(() => {
    const Observer = window.ResizeObserver;
    let failed = false;
    window.ResizeObserver = class extends Observer {
      constructor(callback: ResizeObserverCallback) {
        if (!failed) {
          failed = true;
          throw new Error("Injected graphics resource failure");
        }
        super(callback);
      }
    };
  });
  await page.goto("/?preview-test=1");
  await page
    .getByLabel("Model fixture")
    .setInputFiles(
      fileURLToPath(new URL("./fixtures/cube.step", import.meta.url)),
    );
  await expect(
    page.getByRole("heading", { name: "Let’s reopen your workspace" }),
  ).toBeVisible();
  await page.screenshot({ path: info.outputPath("workspace-recovery.png") });
  await page
    .getByRole("button", { name: "Reopen workspace", exact: true })
    .click();
  await expect(page.getByLabel("Model fixture")).toBeVisible();
  await page
    .getByLabel("Model fixture")
    .setInputFiles(
      fileURLToPath(new URL("./fixtures/cube.step", import.meta.url)),
    );
  await expect(page.locator("[data-preview-ready]")).toBeVisible();
});
