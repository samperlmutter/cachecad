import { expect, test } from "@playwright/test";
import { fileURLToPath } from "node:url";
test("large projects keep rows bounded while search and selections span pages", async ({
  page,
}) => {
  await page.goto("/?demo=1");
  await expect(page.locator(".scan-time")).toContainText("Scanned");
  await page.evaluate(async () => {
    const { api } = await import("/src/api.ts");
    const plan = await api.scan("field-kit");
    plan.actions = [];
    plan.local = {};
    plan.remote = {};
    for (let i = 0; i < 350; i++) {
      const path = `Part-${String(i).padStart(3, "0")}.step`;
      const entry = {
        path,
        size: 128,
        md5: `fixture-${i}`,
        modified: "2026-10-20T04:00:00Z",
        remoteId: null,
        version: null,
        modifiedBy: null,
      };
      plan.local[path] = entry;
      plan.actions.push({
        id: `fixture-${i}`,
        path,
        size: 128,
        local: entry,
        remote: null,
        kind: "upload_new",
        reason: "New local file",
      });
    }
    api.scan = async () => plan;
  });
  await page
    .getByRole("button", { name: "Scan for changes", exact: true })
    .click();
  await expect(page.locator(".file-row-wrap")).toHaveCount(100);
  await page
    .getByRole("checkbox", { name: "Select Part-000.step", exact: true })
    .check();
  await page.getByRole("button", { name: "Next file page" }).click();
  await expect(
    page.getByRole("button", { name: "Part-100.step Project root" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Previous file page" }).click();
  await expect(
    page.getByRole("checkbox", { name: "Select Part-000.step", exact: true }),
  ).toBeChecked();
  await page.getByRole("textbox", { name: "Find a file" }).fill("Part-349");
  await expect(page.locator(".file-row-wrap")).toHaveCount(1);
  await expect(
    page.getByRole("button", { name: "Part-349.step Project root" }),
  ).toBeVisible();
  await expect(page.locator(".table-footer")).toContainText("1 selected");
});
test("workspace renders, filters files, resolves conflict directions, and requires destructive consent", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/?demo=1");
  await expect(
    page.getByRole("heading", { name: "Field kit", exact: true }),
  ).toBeVisible();
  await expect(page.locator("canvas[data-preview-ready]")).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("workspace.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "Google Drive", exact: true }).click();
  await page.getByRole("button", { name: "Local", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Local", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.getByRole("button", { name: "Google Drive", exact: true }),
  ).toHaveAttribute("aria-pressed", "false");
  await page.getByRole("button", { name: /Review & sync/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("heading", { name: "One last look" }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Sync 1 change", exact: true }),
  ).toBeDisabled();
  await dialog.getByRole("checkbox").check();
  await expect(
    dialog.getByRole("button", { name: "Sync 1 change", exact: true }),
  ).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath("review.png") });
  await page.keyboard.press("Escape");
  await page.getByRole("textbox", { name: "Find a file" }).fill("mounting");
  await expect(page.locator(".file-row-wrap")).toHaveCount(1);
  await page.getByRole("button", { name: "Clear search" }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Settings", exact: true }),
  ).toBeVisible();
  await page.getByRole("checkbox", { name: /Keep uploaded revisions/ }).check();
  await page.getByRole("button", { name: "Save preferences" }).click();
  await expect(page.getByRole("status")).toContainText("Preferences saved");
  expect(errors).toEqual([]);
});
test("new users can connect a project with validated folder inputs", async ({
  page,
}) => {
  await page.goto("/?demo=1");
  await page.getByRole("button", { name: "Add project" }).click();
  await page
    .getByRole("textbox", { name: "Project name" })
    .fill("New assembly");
  await page.getByRole("button", { name: "Browse", exact: true }).click();
  await page
    .getByRole("textbox", { name: "Google Drive folder link" })
    .fill("https://drive.google.com/drive/folders/new-folder");
  await page
    .getByRole("button", { name: "Connect project", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "New assembly", exact: true }),
  ).toBeVisible();
});
test("actual STEP import runs OpenCascade in a worker and renders a mesh", async ({
  page,
}, testInfo) => {
  await page.goto("/?preview-test=1");
  await page
    .getByLabel("Model fixture")
    .setInputFiles(
      fileURLToPath(new URL("./fixtures/cube.step", import.meta.url)),
    );
  await expect(page.locator("canvas[data-preview-ready]")).toBeVisible({
    timeout: 60000,
  });
  await expect(page.locator(".preview-caption")).toContainText("triangles");
  await page.getByRole("button", { name: "Toggle wireframe" }).click();
  await expect(
    page.getByRole("button", { name: "Toggle wireframe" }),
  ).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Reset camera" }).click();
  await page.screenshot({ path: testInfo.outputPath("step-preview.png") });
});
test("native SolidWorks files explain supported export formats", async ({
  page,
}) => {
  await page.goto("/?demo=1");
  await page.getByRole("button", { name: /Top housing.sldprt/ }).click();
  await expect(page.getByText("Sync it. Preview an export.")).toBeVisible();
  await expect(
    page.getByText(/Native SolidWorks files sync in full/),
  ).toBeVisible();
});

test("periodic refresh preserves unchanged choices and offline results remain visible", async ({
  page,
}) => {
  await page.clock.install({ time: new Date("2026-10-20T04:00:00Z") });
  await page.goto("/?demo=1");
  await expect(page.locator(".scan-time")).toContainText("Scanned");
  await page
    .getByRole("checkbox", {
      name: "Select 02 Parts/Mounting bracket.stl",
      exact: true,
    })
    .check();
  const before = await page.locator(".scan-time").innerText();
  await page.clock.fastForward(61000);
  await expect(page.locator(".scan-time")).not.toHaveText(before);
  await expect(
    page.getByRole("checkbox", {
      name: "Select 02 Parts/Mounting bracket.stl",
      exact: true,
    }),
  ).toBeChecked();
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await expect(page.locator(".scan-time")).toContainText("last known results");
  await expect(
    page.getByRole("button", { name: /Review & sync/ }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: /Mounting bracket.stl/ }),
  ).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect(page.locator(".scan-time")).toContainText("Scanned");
  await expect(
    page.getByRole("button", { name: /Review & sync/ }),
  ).toBeEnabled();
});
