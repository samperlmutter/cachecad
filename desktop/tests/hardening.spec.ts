import { expect, test } from "@playwright/test";

test("read-only local files are not described as synchronized", async ({
  page,
}) => {
  await page.goto("/?demo=1");
  await expect(page.locator(".scan-time")).toContainText("Scanned");
  await page.evaluate(async () => {
    const resource = performance
      .getEntriesByType("resource")
      .find((entry) => new URL(entry.name).pathname === "/src/api.ts");
    const { api } = await import(resource?.name ?? "/src/api.ts");
    const scan = api.scan;
    api.scan = async (id: string) => {
      const plan = await scan(id);
      // Match the native read-only planner, which suppresses Drive writes.
      plan.actions = plan.actions.filter(
        (action: any) =>
          !action.kind.startsWith("upload") && action.kind !== "delete_cloud",
      );
      return plan;
    };
  });
  await page
    .getByRole("button", { name: "Project options", exact: true })
    .click();
  await page
    .getByRole("checkbox", { name: /Make this project read-only/ })
    .check();
  await page.getByRole("button", { name: "Save project", exact: true }).click();
  await expect(page.locator(".scan-time")).toContainText("Scanned");
  await page.getByRole("tab", { name: /All files/ }).click();
  const localFile = page.locator(".file-row").filter({
    has: page.locator(".file-name").filter({ hasText: /Mounting bracket.stl/ }),
  });
  await expect(localFile).toContainText("Local only");
  await expect(localFile.getByRole("checkbox")).toBeDisabled();
  await expect(localFile).not.toContainText("Synced");
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
