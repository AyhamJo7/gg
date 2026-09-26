// Browser e2e for operator-critical surfaces against the seeded, fake-only
// backend (backend/tests/helpers/e2e_ui_server.py). Run via `make e2e`.
import { chromium } from "playwright";

const BASE = process.env.GG_E2E_URL ?? "http://127.0.0.1:5174";
const MISSION = "e2e-rr";
const TIMEOUT_MS = 15_000;

const failures = [];
async function check(name, fn) {
  try { await fn(); console.log(`ok   ${name}`); }
  catch (err) { failures.push(name); console.error(`FAIL ${name}\n     ${err.message.split("\n")[0]}`); }
}
function expect(cond, message) { if (!cond) throw new Error(message); }

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const pageErrors = [];
page.on("pageerror", (e) => pageErrors.push(String(e)));
page.setDefaultTimeout(TIMEOUT_MS);

await check("overview routes the caveated completion to attention", async () => {
  await page.goto(`${BASE}/#/`);
  const section = page.locator("section", { has: page.getByRole("heading", { name: /Needs your attention/ }) });
  await section.getByText("E2E RechnungsRadar retry").waitFor();
  const text = await section.innerText();
  expect(text.includes("Completed with caveats"), "verdict label missing");
  expect(text.includes("1 open MEDIUM"), "open finding caveat missing");
  expect(text.includes("review not certified independent"), "review caveat missing");
  expect(text.includes("30 tests skipped in verification"), "skipped-test caveat missing");
});

await check("mission page states the recorded review reason, never a fabricated cause", async () => {
  await page.goto(`${BASE}/#/missions?mission=${MISSION}`);
  const card = page.getByTestId("review-trust-warning");
  await card.waitFor();
  const text = await card.innerText();
  expect(text.includes("Review not certified as independent"), "card title missing");
  expect(text.includes("writer provenance incomplete"), "recorded reason missing");
  expect(!text.includes("also performed the implementation"), "fabricated self-review cause shown");
});

await check("agent relay flags thin evidence and lost work", async () => {
  const relay = page.locator(".relay-section");
  await relay.getByText("thin evidence").waitFor();
  await relay.getByText("lost work").waitFor();
  await relay.locator(".relay-run.flagged summary", { hasText: "agy" }).click();
  await relay.getByText(/GIT_DIFF 131 chars/).first().waitFor();
  await relay.getByText(/codex handed off to/).waitFor();
});

await check("command palette opens with the keyboard and navigates", async () => {
  await page.goto(`${BASE}/#/providers`);
  await page.keyboard.press("Control+k");
  const input = page.getByRole("combobox", { name: "Search commands" });
  await input.fill("E2E Rechn");
  await page.getByRole("option", { name: /E2E RechnungsRadar retry/ }).waitFor();
  await input.press("Enter");
  await page.waitForURL(/mission=e2e-rr/);
});

await check("activity feed shows recorded events", async () => {
  await page.getByRole("button", { name: /Activity/ }).click();
  const panel = page.getByRole("complementary", { name: "Workspace activity" });
  await panel.getByText(/Mission completed/).waitFor();
});

await check("no uncaught page errors", async () => {
  expect(pageErrors.length === 0, pageErrors.join("; "));
});

await browser.close();
if (failures.length) {
  console.error(`\n${failures.length} e2e check(s) failed`);
  process.exit(1);
}
console.log("\nall e2e checks passed");
