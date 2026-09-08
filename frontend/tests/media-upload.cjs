/* Behavioral browser checks with real File, Worker, crypto and IndexedDB.
   Run: node frontend/tests/media-upload.cjs (Playwright available on NODE_PATH). */
const {chromium} = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const source = path.resolve(__dirname, "../provider");
const sha = b => crypto.createHash("sha256").update(b).digest("base64");

(async () => {
  const browser = await chromium.launch(process.env.MEDIA_BROWSER_EXECUTABLE ? {executablePath: process.env.MEDIA_BROWSER_EXECUTABLE} : {});
  try {
    for (const scenario of ["retry", "resume", "changed", "cancel"]) {
      const context = await browser.newContext();
      const rows = new Map(), parts = new Map(), signatures = [];
      let completed = 0, uploads = 0, failSecond = scenario === "resume", failed = false;
      await context.route("**/*", async route => {
        const req = route.request(), url = new URL(req.url());
        const cors = {"access-control-allow-origin": "*", "access-control-allow-methods": "PUT,GET,POST,DELETE,OPTIONS", "access-control-allow-headers": "x-amz-checksum-sha256,content-type"};
        if (req.method() === "OPTIONS") return route.fulfill({status: 204, headers: cors});
        if (url.hostname === "storage.test") {
          const number = Number(url.pathname.split("/").pop());
          uploads++;
          if ((scenario === "retry" && !failed) || (failSecond && number === 2)) {
            failed = true;
            return route.fulfill({status: 403, headers: cors});
          }
          const bytes = req.postDataBuffer();
          assert.equal(req.headers()["x-amz-checksum-sha256"], sha(bytes));
          parts.set(number, {number, checksum: sha(bytes), size: bytes.length});
          return route.fulfill({status: 200, headers: cors});
        }
        if (url.pathname.endsWith(".js")) {
          return route.fulfill({contentType: "application/javascript", body: fs.readFileSync(path.join(source, path.basename(url.pathname)), "utf8")});
        }
        if (!url.pathname.startsWith("/api")) return route.fulfill({contentType: "text/html", body: '<div id="root"></div><script src="/static/provider/media-upload.js"></script>'});
        const body = req.postDataJSON();
        let result = {};
        if (url.pathname.endsWith("capabilities")) result = {enabled: true};
        else if (url.pathname.endsWith("collections")) result = {id: "collection"};
        else if (url.pathname.endsWith("/files") && req.method() === "POST") {
          if (!rows.has(body.token)) rows.set(body.token, {id: "file", state: "uploading", path: body.path, size: body.size, chunk_size: 3, part_count: 2});
          result = rows.get(body.token);
        } else if (url.pathname.endsWith("/sign")) {
          signatures.push(body.number);
          result = {url: "https://storage.test/file/" + body.number};
        } else if (url.pathname.endsWith("/complete")) { completed++; result = {state: "completing"}; }
        else if (req.method() === "DELETE") result = {state: "cancelling"};
        else result = {id: "file", state: "uploading", chunk_size: 3, part_count: 2, parts: [...parts.values()]};
        return route.fulfill({contentType: "application/json", body: JSON.stringify(result)});
      });
      let page = await context.newPage();
      async function mount() {
        await page.goto("https://portal.test");
        await page.evaluate(async () => {
          const nativeTimeout = window.setTimeout;
          window.setTimeout = (fn, ms) => nativeTimeout(fn, Math.min(ms, 20));
          await window.ArchangelMedia.mount(document.querySelector("#root"), async (method, path, body) => {
            const res = await fetch("/api"+path, {method, body: body === undefined ? undefined : JSON.stringify(body), headers: {"content-type": "application/json"}});
            if (!res.ok) throw new Error("API error");
            return res.json();
          }, "tester");
        });
      }
      async function choose(contents) {
        await page.evaluate(contents => {
          const transfer = new DataTransfer();
          transfer.items.add(new File([contents], "video.mp4", {type: "video/mp4", lastModified: 123}));
          const input = document.querySelector("[data-files]");
          input.files = transfer.files;
          input.dispatchEvent(new Event("change"));
        }, contents);
      }
      await mount();
      if (scenario === "changed") parts.set(1, {number: 1, checksum: sha("abc"), size: 3});
      if (scenario === "cancel") await page.locator("[data-pause]").click();
      await choose(scenario === "changed" ? "xbcdef" : "abcdef");
      if (scenario === "cancel") {
        await page.getByRole("button", {name: "Cancel", exact: true}).click();
        await page.waitForFunction(() => document.querySelector("ul").textContent.includes("cancellation requested"));
        assert.equal(uploads, 0); assert.equal(completed, 0);
      } else if (scenario === "changed") {
        await page.waitForFunction(() => document.querySelector("ul").textContent.includes("File changed"));
        assert.equal(uploads, 0); assert.equal(completed, 0);
      } else {
        if (scenario === "resume") {
          await page.waitForFunction(() => document.querySelector("ul").textContent.includes("Part upload failed"));
          assert.equal(parts.size, 1);
          const originalToken = [...rows.keys()][0];
          failSecond = false;
          await page.close(); page = await context.newPage();
          await mount(); await choose("abcdef");
          await page.waitForFunction(() => document.querySelector("ul").textContent.includes("verification queued"));
          assert.equal(rows.size, 1); assert.equal([...rows.keys()][0], originalToken);
          assert.equal(signatures.filter(n => n === 1).length, 1);
        } else {
          await page.waitForFunction(() => document.querySelector("ul").textContent.includes("verification queued"));
          assert.equal(signatures.filter(n => n === 1).length, 2);
        }
        assert.equal(completed, 1); assert.equal(parts.size, 2);
      }
      await page.waitForFunction(() => !document.querySelector("[data-files]").disabled);
      assert.equal(await page.locator("[data-files]").inputValue(), "");
      console.log("PASS", scenario);
      await context.close();
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
