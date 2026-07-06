import { chromium } from "playwright-core";

const APP = "http://localhost:3000";
const CLAIM = `1. A system for generating vector graphics, comprising:
a processor (102);
a memory module coupled to the processor;
an extraction engine configured to convert unstructured text into a structured dependency graph;
a layout engine that calculates deterministic coordinates; and
a rendering engine that draws each component as a PCT-compliant geometric primitive.`;

const results = [];
const consoleErrors = [];
const step = (name, ok, detail = "") => {
  results.push(`${ok ? "PASS" : "FAIL"} | ${name}${detail ? " | " + detail : ""}`);
  if (!ok) process.exitCode = 1;
};

const browser = await chromium.launch({ channel: "msedge", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.on("console", (msg) => {
  if (msg.type() === "error") consoleErrors.push(msg.text());
});
page.on("pageerror", (err) => consoleErrors.push(String(err)));

// 1. load + empty state
await page.goto(APP, { waitUntil: "networkidle" });
const emptyVisible = await page
  .getByText("awaiting structural input")
  .isVisible();
step("page loads with Blueprint empty state", emptyVisible);
await page.screenshot({ path: "shot_1_empty.png" });

// 2. generate disabled with no input
const genButton = page.getByRole("button", { name: /GENERATE/ });
step("GENERATE disabled when editor empty", await genButton.isDisabled());

// 3. type claim, generate, sheet renders
await page.locator("textarea").fill(CLAIM);
await genButton.click();
const loadingSeen = await page
  .getByText("> Parsing hierarchical dependencies...")
  .isVisible()
  .catch(() => false);
await page.waitForSelector(".sheet svg", { timeout: 45000 });
step("generate returns figures and SVG renders", true,
  `terminal log visible: ${loadingSeen}`);
await page.screenshot({ path: "shot_2_fig1.png" });

// 4. tabs: two sheets, switching works
const tabs = page.locator("nav button");
const tabCount = await tabs.count();
step("multi-sheet tabs present", tabCount === 2, `count=${tabCount}`);
const fig1Svg = await page.locator(".sheet").innerHTML();
await tabs.nth(1).click();
await page.waitForTimeout(300);
const fig2Svg = await page.locator(".sheet").innerHTML();
step("FIG. 2 tab shows a different sheet", fig1Svg !== fig2Svg);
await page.screenshot({ path: "shot_3_fig2.png" });

// 5. downloads enabled and produce files
const [download] = await Promise.all([
  page.waitForEvent("download", { timeout: 10000 }),
  page.getByRole("button", { name: /PDF/ }).click(),
]);
step("PDF download fires", !!download, download && download.suggestedFilename());

// 6. probe: split-pane divider drags smoothly
const divider = page.locator('[role="separator"]');
const before = (await page.locator("section").first().boundingBox()).width;
const box = await divider.boundingBox();
await page.mouse.move(box.x + 1, box.y + 300);
await page.mouse.down();
await page.mouse.move(box.x + 180, box.y + 300, { steps: 12 });
await page.mouse.up();
const after = (await page.locator("section").first().boundingBox()).width;
step("split-pane divider drags", Math.abs(after - before) > 100,
  `${Math.round(before)}px -> ${Math.round(after)}px`);

// 7. probe: zoom reset control reports scale
const resetButton = page.getByRole("button", { name: /RESET/ });
step("zoom/reset control present", await resetButton.isVisible(),
  await resetButton.textContent());

// 8. probe: garbage input -> red error toast (backend 400 path)
await page.locator("textarea").fill("abc");
await genButton.click();
const toast = page.getByText("ERROR:");
await toast.waitFor({ timeout: 20000 });
step("unparseable input surfaces red error toast", await toast.isVisible());
await page.screenshot({ path: "shot_4_error.png" });

await browser.close();

console.log(results.join("\n"));
console.log("console errors:", consoleErrors.length ? consoleErrors : "none");
