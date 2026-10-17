// Run by test_face_overlay_layout.py with Playwright available through NODE_PATH.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const {chromium} = require("playwright");

(async () => {
  const {css, js, fixtures} = JSON.parse(fs.readFileSync(0, "utf8"));
  const browser = await chromium.launch({headless: true});
  try {
    for (const deviceScaleFactor of [1, 1.5]) {
      const context = await browser.newContext({deviceScaleFactor});
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      for (const fixture of fixtures) {
        for (const matches of [false, true]) {
          const button = matches ? "data-open-face-matches data-face-matches-item" : "data-open-faces data-faces-item";
          await page.route("http://bildebank.test/**", route => {
            if (route.request().url().includes("/api/")) {
              const detail = matches ? fixture.matches : fixture.assignment;
              return route.fulfill({json: {ok: true, html: detail + detail}});
            }
            return route.fulfill({contentType: "text/html", body:
              `<html><head><meta name="viewport" content="width=device-width, initial-scale=1"><style>${css}</style></head><body><button ${button}="1">Åpne</button>${fixture.overlay}</body></html>`});
          });
          await page.setViewportSize({width: 1280, height: 800});
          await page.goto("http://bildebank.test/");
          await page.addScriptTag({content: js});
          await page.locator("body > button").click();
          const overlay = matches ? "#faceMatchesOverlay" : "#faceOverlay";
          await page.waitForFunction(selector => {
            const images = [...document.querySelectorAll(`${selector} img`)];
            return images.length && images.every(img => img.complete && img.naturalWidth);
          }, overlay);
          for (const width of [1280, 900, 640]) {
            if (width === 640) await page.locator(`${overlay} .lightbox-close`).click();
            await page.setViewportSize({width, height: 600});
            if (width === 640) await page.locator("body > button").click();
            await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
            const measurements = await page.locator(overlay).evaluate(root => {
              const rect = element => {
                const {left, top, right, bottom, width, height} = element.getBoundingClientRect();
                return {left, top, right, bottom, width, height};
              };
              const stage = root.querySelector(".lightbox-stage");
              return {
                scrollWidth: stage.scrollWidth, clientWidth: stage.clientWidth,
                details: [...root.querySelectorAll(".face-detail")].map(detail => ({
                  image: rect(detail.querySelector("img")),
                  box: rect(detail.querySelector(".face-box")),
                  above: rect(detail.querySelector(".face-suggestion-summary") || detail.querySelector(".face-detail-title")),
                  below: rect(detail.querySelector(".assign-row, .empty")),
                  frame: rect(detail.querySelector(".face-media-frame")),
                })),
              };
            });
            const label = `${fixture.name}, matches=${matches}, width=${width}, scale=${deviceScaleFactor}`;
            assert.ok(measurements.scrollWidth <= measurements.clientWidth + 1, `Horizontal overflow: ${label}`);
            for (const {image, box, above, below, frame} of measurements.details) {
              assert.ok(image.top >= above.bottom, `Image covers text above: ${label}`);
              assert.ok(image.bottom <= below.top, `Image covers controls below: ${label}`);
              assert.ok(Math.abs(image.width - frame.width) < 1, `Wrong frame width: ${label}`);
              assert.ok(Math.abs(image.height - frame.height) < 1, `Wrong frame height: ${label}`);
              const expected = {
                0: [0.2, 0.3, 0.1, 0.15], 90: [0.55, 0.2, 0.15, 0.1],
                180: [0.7, 0.55, 0.1, 0.15], 270: [0.3, 0.7, 0.15, 0.1],
              }[fixture.rotation];
              const actual = [(box.left - image.left) / image.width, (box.top - image.top) / image.height,
                box.width / image.width, box.height / image.height];
              actual.forEach((value, index) => assert.ok(Math.abs(value - expected[index]) < 0.002, `Face box moved: ${label}`));
            }
          }
          assert.deepEqual(errors, [], "Browser script errors");
          await page.unrouteAll();
        }
      }
      await context.close();
    }
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exit(1);});
