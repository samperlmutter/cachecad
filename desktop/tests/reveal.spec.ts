import { expect, test } from "@playwright/test";

for (const [platform, manager] of [
  ["macos", "Finder"],
  ["windows", "Explorer"],
]) {
  test(`local files reveal in ${manager}; Drive-only files require download`, async ({
    page,
  }) => {
    await page.addInitScript(
      ({ platform }) => {
        const global = window as any;
        const file = (path: string, remote = false) => ({
          path,
          size: 100,
          md5: "abc",
          modified: "now",
          remoteId: remote ? "remote" : null,
          version: remote ? "1" : null,
          modifiedBy: null,
        });
        const fixture = { calls: [] as unknown[], fail: false };
        global.revealFixture = fixture;
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
                platform,
                projects: [
                  {
                    id: "qa",
                    name: "Reveal QA",
                    localRoot: "/qa",
                    driveFolderId: "qa",
                    readOnly: true,
                  },
                ],
                settings: { checkUpdates: false },
                account: {
                  subject: "qa",
                  name: "QA",
                  email: "qa@example.test",
                },
                googleConfigured: true,
                updaterConfigured: false,
                activity: [],
                version: "2.0.0-alpha.1",
                accountError: null,
              };
            if (command === "list_local_files")
              return {
                "Parts 零件/Fourbar intake 2026.STEP": file(
                  "Parts 零件/Fourbar intake 2026.STEP",
                ),
              };
            if (command === "scan_project")
              return {
                id: "plan",
                projectId: "qa",
                createdAt: 1,
                actions: [],
                folders: {},
                local: {
                  "Parts 零件/Fourbar intake 2026.STEP": file(
                    "Parts 零件/Fourbar intake 2026.STEP",
                  ),
                },
                remote: {
                  "Drive only.sldprt": file("Drive only.sldprt", true),
                },
              };
            if (command === "reveal_file") {
              fixture.calls.push(args);
              if (fixture.fail)
                throw "This file is not on your computer. Download it before revealing it.";
            }
          },
        };
      },
      { platform },
    );
    await page.goto("/");
    await page.getByRole("tab", { name: /All files/ }).click();
    const local = page.getByRole("button", {
      name: `Reveal in ${manager}: Parts 零件/Fourbar intake 2026.STEP`,
      exact: true,
    });
    await expect(local).toBeEnabled();
    await local.click();
    await expect
      .poll(() => page.evaluate(() => (window as any).revealFixture.calls))
      .toEqual([
        { projectId: "qa", path: "Parts 零件/Fourbar intake 2026.STEP" },
      ]);
    await expect(page.getByRole("status")).toContainText(`Shown in ${manager}`);
    await expect(
      page.getByRole("button", {
        name: `Reveal in ${manager}: Drive only.sldprt`,
        exact: true,
      }),
    ).toBeDisabled();
    await expect(page.getByText(/Download to reveal locally/)).toBeVisible();
    await page.evaluate(() => {
      (window as any).revealFixture.fail = true;
    });
    await local.click();
    await expect(page.getByRole("alert")).toContainText(
      "Download it before revealing it",
    );
    await expect(page.locator("canvas, [data-preview-ready]")).toHaveCount(0);
  });
}

test("installed workspace readiness includes local files and the reveal action", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const global = window as any;
    global.isTauri = true;
    global.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener() {} };
    global.__TAURI_INTERNALS__ = {
      transformCallback() {
        return 1;
      },
      async invoke(command: string) {
        if (command === "bootstrap")
          return {
            installerSmoke: true,
            platform: "macos",
            projects: [
              {
                id: "installer",
                name: "Installer verification",
                localRoot: "/qa",
                driveFolderId: "fixture",
                readOnly: true,
              },
            ],
            settings: { checkUpdates: false },
            account: null,
            googleConfigured: false,
            updaterConfigured: false,
            activity: [],
            version: "2.0.0-alpha.1",
            accountError: null,
          };
        if (command === "list_local_files")
          return {
            "Installer verification.txt": {
              path: "Installer verification.txt",
              size: 10,
              md5: "",
              modified: "now",
            },
          };
      },
    };
    const invoke = global.__TAURI_INTERNALS__.invoke;
    global.__TAURI_INTERNALS__.invoke = async (command: string, args: any) => {
      if (command === "native_ready") global.smokeEvidence = args.evidence;
      return invoke(command, args);
    };
  });
  await page.goto("/");
  await expect
    .poll(() => page.evaluate(() => (window as any).smokeEvidence))
    .toMatchObject({
      workspaceVisible: true,
      fileListVisible: true,
      revealButtonVisible: true,
      title: "Permafrost",
    });
});
