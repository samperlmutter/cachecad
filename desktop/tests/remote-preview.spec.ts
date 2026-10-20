import { expect, test } from "@playwright/test";

test("Drive-only preview shares pending reads and reloads after a remote version changes", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const global = window as any;
    const fixture = { reads: 0, version: "1", active: false };
    global.remotePreviewFixture = fixture;
    global.isTauri = true;
    global.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener() {} };
    global.__TAURI_INTERNALS__ = {
      transformCallback() {
        return 1;
      },
      async invoke(command: string) {
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
              "Remote.obj": {
                path: "Remote.obj",
                size: 128,
                md5: fixture.version,
                modified: "same",
                remoteId: "remote",
                version: fixture.version,
                modifiedBy: null,
              },
            },
          };
        if (command === "read_preview") {
          if (fixture.active)
            throw new Error("Another operation is still running.");
          fixture.active = true;
          fixture.reads++;
          await new Promise((resolve) => setTimeout(resolve, 100));
          fixture.active = false;
          return new TextEncoder().encode(
            "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
          ).buffer;
        }
      },
    };
  });
  await page.goto("/");
  await expect(page.locator(".scan-time")).toContainText("Scanned");
  await expect(page.getByRole("alert")).toContainText(
    "Could not clean preview cache for QA: fixture",
  );
  await page.getByRole("button", { name: "Dismiss message" }).click();
  await page.getByRole("tab", { name: /All files/ }).click();
  await page.getByRole("button", { name: "Remote.obj Project root" }).click();
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
});
