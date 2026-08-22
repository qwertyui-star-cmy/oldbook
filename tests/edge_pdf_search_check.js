const { chromium } = require("playwright");

const edgePath = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const pdfUrl = process.argv[2];
const query = process.argv[3] || "失时";

if (!pdfUrl) {
  throw new Error("Usage: node edge_pdf_search_check.js <pdf-url> [query]");
}

async function attachToTarget(browserSession, targetId) {
  const attached = await browserSession.send("Target.attachToTarget", {
    targetId,
    flatten: false,
  });
  let messageId = 0;
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++messageId;
    const onMessage = (event) => {
      if (event.sessionId !== attached.sessionId) return;
      const message = JSON.parse(event.message);
      if (message.id !== id) return;
      browserSession.off("Target.receivedMessageFromTarget", onMessage);
      if (message.error) reject(new Error(message.error.message));
      else resolve(message.result);
    };
    browserSession.on("Target.receivedMessageFromTarget", onMessage);
    browserSession.send("Target.sendMessageToTarget", {
      sessionId: attached.sessionId,
      message: JSON.stringify({ id, method, params }),
    }).catch(reject);
  });
  return send;
}

async function evaluate(send, expression) {
  const result = await send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  return result.result.value;
}

(async () => {
  const browser = await chromium.launch({
    executablePath: edgePath,
    headless: true,
    args: ["--disable-extensions-except=mhjfbmdgcfjbbpaeojofohoefgiehjai"],
  });
  try {
    const page = await browser.newPage();
    await page.goto(pdfUrl, { waitUntil: "load", timeout: 30000 });
    await page.waitForTimeout(2500);

    const browserSession = await browser.newBrowserCDPSession();
    const targets = await browserSession.send("Target.getTargets");
    const viewer = targets.targetInfos.find((target) =>
      target.type === "webview" && target.url.includes("edge_pdf/index.html")
    );
    if (!viewer) throw new Error("Edge PDF viewer target was not found");

    const send = await attachToTarget(browserSession, viewer.targetId);
    await send("Runtime.enable");

    await send("Input.dispatchKeyEvent", {
      type: "keyDown", key: "f", code: "KeyF", windowsVirtualKeyCode: 70, modifiers: 2,
    });
    await send("Input.dispatchKeyEvent", {
      type: "keyUp", key: "f", code: "KeyF", windowsVirtualKeyCode: 70, modifiers: 2,
    });
    await new Promise((resolve) => setTimeout(resolve, 300));

    const controls = await evaluate(send, `JSON.stringify(
      Array.from(document.querySelectorAll("input,button,[role=button]")).map((node) => ({
        tag: node.tagName,
        id: node.id,
        type: node.getAttribute("type"),
        role: node.getAttribute("role"),
        aria: node.getAttribute("aria-label"),
        title: node.getAttribute("title"),
        text: (node.innerText || "").trim(),
      }))
    )`);
    const findInputs = await evaluate(send, `JSON.stringify(
      Array.from(document.querySelectorAll("input")).map((node) => ({
        id: node.id,
        aria: node.getAttribute("aria-label"),
        placeholder: node.getAttribute("placeholder"),
      }))
    )`);
    const accessibility = await send("Accessibility.getFullAXTree");
    const axText = accessibility.nodes.map((node) => node.name && node.name.value).filter(Boolean);
    const pdfFrame = targets.targetInfos.find((target) => target.type === "iframe" && target.url.startsWith(pdfUrl.split("?")[0]));
    let pdfAccessibility = [];
    if (pdfFrame) {
      const frameSend = await attachToTarget(browserSession, pdfFrame.targetId);
      await frameSend("Runtime.enable");
      const frameTree = await frameSend("Accessibility.getFullAXTree");
      pdfAccessibility = frameTree.nodes.map((node) => node.name && node.name.value).filter(Boolean);
    }
    console.log(JSON.stringify({
      query,
      controls: JSON.parse(controls),
      findInputs: JSON.parse(findInputs),
      accessibility: axText.filter((value) => value.includes(query[0])).slice(0, 20),
      pdfAccessibility: pdfAccessibility.filter((value) => value.includes(query[0])).slice(0, 20),
    }, null, 2));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.stack || String(error));
  process.exitCode = 1;
});
