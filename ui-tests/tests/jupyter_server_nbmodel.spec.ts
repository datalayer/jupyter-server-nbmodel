import { expect, test } from '@playwright/test';

test('should emit an activation console message', async ({ page }) => {
  const logs: string[] = [];

  page.on('console', message => {
    logs.push(message.text());
  });

  await page.goto('http://localhost:8888/lab');

  await expect
    .poll(
      () =>
        logs.filter(
          s => s === 'JupyterLab extension jupyter-server-nbmodel is activated!'
        ).length,
      { timeout: 30_000 }
    )
    .toBe(1);

  expect(
    logs.filter(
      s => s === 'JupyterLab extension jupyter-server-nbmodel is activated!'
    )
  ).toHaveLength(1);
});
