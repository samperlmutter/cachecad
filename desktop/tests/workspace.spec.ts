import { expect,test } from '@playwright/test';
import { fileURLToPath } from 'node:url';
test('workspace renders, filters files, resolves conflict directions, and requires destructive consent',async({page},testInfo)=>{
  const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto('/?demo=1');
  await expect(page.getByRole('heading',{name:'Field kit',exact:true})).toBeVisible();
  await expect(page.locator('canvas[data-preview-ready]')).toBeVisible();
  await page.screenshot({path:testInfo.outputPath('workspace.png'),fullPage:true});
  await page.getByRole('button',{name:'Google Drive',exact:true}).click();
  await page.getByRole('button',{name:'Local',exact:true}).click();
  await expect(page.getByRole('button',{name:'Local',exact:true})).toHaveAttribute('aria-pressed','true');
  await expect(page.getByRole('button',{name:'Google Drive',exact:true})).toHaveAttribute('aria-pressed','false');
  await page.getByRole('button',{name:/Review & sync/}).click();
  const dialog=page.getByRole('dialog');await expect(dialog.getByRole('heading',{name:'One last look'})).toBeVisible();
  await expect(dialog.getByRole('button',{name:'Sync 1 change',exact:true})).toBeDisabled();
  await dialog.getByRole('checkbox').check();await expect(dialog.getByRole('button',{name:'Sync 1 change',exact:true})).toBeEnabled();
  await page.screenshot({path:testInfo.outputPath('review.png')});await page.keyboard.press('Escape');
  await page.getByRole('textbox',{name:'Find a file'}).fill('mounting');await expect(page.locator('.file-row-wrap')).toHaveCount(1);
  await page.getByRole('button',{name:'Clear search'}).click();await page.getByRole('button',{name:'Settings',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Settings',exact:true})).toBeVisible();
  await page.getByRole('checkbox',{name:/Keep uploaded revisions/}).check();await page.getByRole('button',{name:'Save preferences'}).click();
  await expect(page.getByRole('status')).toContainText('Preferences saved');expect(errors).toEqual([]);
});
test('new users can connect a project with validated folder inputs',async({page})=>{
  await page.goto('/?demo=1');await page.getByRole('button',{name:'Add project'}).click();
  await page.getByRole('textbox',{name:'Project name'}).fill('New assembly');await page.getByRole('button',{name:'Browse',exact:true}).click();
  await page.getByRole('textbox',{name:'Google Drive folder link'}).fill('https://drive.google.com/drive/folders/new-folder');
  await page.getByRole('button',{name:'Connect project',exact:true}).click();await expect(page.getByRole('heading',{name:'New assembly',exact:true})).toBeVisible();
});
test('actual STEP import runs OpenCascade in a worker and renders a mesh',async({page},testInfo)=>{
  await page.goto('/?preview-test=1');await page.getByLabel('Model fixture').setInputFiles(process.env.PERMAFROST_STEP_FIXTURE ?? fileURLToPath(new URL('./fixtures/cube.step',import.meta.url)));
  await expect(page.locator('canvas[data-preview-ready], .preview-message.error')).toBeVisible({timeout:60000});expect(await page.locator('.preview-message.error').allTextContents()).toEqual([]);await expect(page.locator('.preview-caption')).toContainText('triangles');
  await page.getByRole('button',{name:'Toggle wireframe'}).click();await expect(page.getByRole('button',{name:'Toggle wireframe'})).toHaveAttribute('aria-pressed','true');
  await page.getByRole('button',{name:'Reset camera'}).click();await page.screenshot({path:testInfo.outputPath('step-preview.png')});
});
test('native SolidWorks files explain supported export formats',async({page})=>{
  await page.goto('/?demo=1');await page.getByRole('button',{name:/Top housing.sldprt/}).click();
  await expect(page.getByText('Sync it. Preview an export.')).toBeVisible();
  await expect(page.getByText(/Native SolidWorks files sync in full/)).toBeVisible();
});
test('preview failures show the native file-read reason', async ({page}) => {
  await page.addInitScript(() => {
    File.prototype.arrayBuffer = async function () {
      throw 'This file is not on your computer yet. Download it before previewing.';
    };
  });
  await page.goto('/?preview-test=1');
  await page.getByLabel('Model fixture').setInputFiles(fileURLToPath(new URL('./fixtures/cube.step', import.meta.url)));
  await expect(page.getByRole('alert')).toContainText('This file is not on your computer yet. Download it before previewing.');
  await expect(page.getByRole('alert')).not.toContainText('Could not read this model.');
});
test('unchanged-content renames display and select as one change', async ({page}, testInfo) => {
  await page.goto('/?demo=1');
  const name = 'README.txt → PROJECT.md';
  const rename = page.locator('.file-row-wrap').filter({hasText: name});
  await expect(rename).toHaveCount(1);
  await expect(rename.locator('.status-pill')).toHaveText('Rename in Drive');
  await page.getByRole('textbox', {name: 'Find a file'}).fill('README.txt');
  await expect(page.locator('.file-row-wrap')).toHaveCount(1);
  await rename.getByRole('checkbox').check();
  await expect(page.getByText('1 change ready to sync', {exact: true})).toBeVisible();
  await page.getByRole('button', {name: 'Rescan local and Google Drive folders'}).click();
  await expect(rename.getByRole('checkbox')).toBeChecked();
  await page.getByRole('button', {name: /Review & sync/}).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText(name, {exact: true})).toBeVisible();
  await expect(dialog.getByText('Rename in Drive', {exact: true})).toBeVisible();
  const sync = dialog.getByRole('button', {name: 'Sync 1 change', exact: true});
  await expect(sync).toBeDisabled();
  await dialog.getByRole('checkbox').check();
  await expect(sync).toBeEnabled();
  await page.screenshot({path: testInfo.outputPath('rename-review.png')});
  await page.keyboard.press('Escape');
  await page.getByRole('tab', {name: /All files/}).click();
  await expect(page.locator('.file-row-wrap')).toHaveCount(1);
  await expect(rename).toBeVisible();
});
