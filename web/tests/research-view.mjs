/** Legacy research UI tests deliberately expose their labelled acceptance fixtures.
 * Production listings remain filtered; paper-workspace.mjs tests that boundary.
 */
export async function researchFixtures(page) {
  await page.route("**/api/runs", async (route) => {
    const response = await route.fetch();
    const rows = await response.json();
    await route.fulfill({
      response,
      json: [...rows].reverse().map((r) => ({ ...r, synthetic: false })),
    });
  });
}
export async function simulationView(page) {
  await page
    .getByRole("button", { name: "Data & Alpaca ↗", exact: true })
    .click();
  await page.getByRole("button", { name: "Simulation", exact: true }).click();
  await page.getByRole("button", { name: "Close panel", exact: true }).click();
}
