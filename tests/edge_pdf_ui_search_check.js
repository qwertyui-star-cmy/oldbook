const { chromium } = require("playwright");
const { spawnSync } = require("child_process");
const os = require("os");
const path = require("path");

const edgePath = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const pdfUrl = process.argv[2];
const query = process.argv[3] || "失时";
const marker = `edge-pdf-search-${process.pid}-${Date.now()}`;
const userDataDir = path.join(os.tmpdir(), marker);

if (!pdfUrl) throw new Error("Usage: node edge_pdf_ui_search_check.js <pdf-url> [query]");

function readEdgeWindow(query) {
  const queryBase64 = Buffer.from(query, "utf8").toString("base64");
  const script = `
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName System.Windows.Forms
    $query = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${queryBase64}'))
    $windows = Get-CimInstance Win32_Process | Where-Object {
      $_.Name -eq 'msedge.exe' -and $_.CommandLine -like '*${marker}*'
    } | ForEach-Object {
      $process = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
      if ($process -and $process.MainWindowHandle -ne 0) {
        $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$process.MainWindowHandle)
        $findCondition = New-Object System.Windows.Automation.PropertyCondition(
          [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'find'
        )
        $findButton = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $findCondition)
        if ($findButton) {
          $invoke = $findButton.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
          $invoke.Invoke()
          Start-Sleep -Milliseconds 400
        }
        $edits = $root.FindAll(
          [System.Windows.Automation.TreeScope]::Descendants,
          (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Edit
          ))
        )
        foreach ($edit in $edits) {
          if ($edit.Current.AutomationId -notin @('pageselector', 'view_1021')) {
            try {
              $edit.SetFocus()
              [System.Windows.Forms.SendKeys]::SendWait('^a')
              [System.Windows.Forms.SendKeys]::SendWait('{BACKSPACE}')
              [System.Windows.Forms.SendKeys]::SendWait($query)
            } catch {}
          }
        }
        Start-Sleep -Milliseconds 1000
        $nodes = $root.FindAll(
          [System.Windows.Automation.TreeScope]::Descendants,
          [System.Windows.Automation.Condition]::TrueCondition
        )
        foreach ($node in $nodes) {
          [PSCustomObject]@{
            Name = $node.Current.Name
            Type = $node.Current.ControlType.ProgrammaticName
            Id = $node.Current.AutomationId
            Enabled = $node.Current.IsEnabled
            Value = try {
              $node.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value
            } catch { $null }
          }
        }
      }
    }
    $windows | Where-Object { $_.Name -or $_.Id } | ConvertTo-Json -Compress
  `;
  const result = spawnSync("powershell.exe", ["-NoProfile", "-Command", script], {
    encoding: "utf8",
    timeout: 15000,
  });
  if (result.status !== 0) throw new Error(result.stderr || "UI Automation failed");
  return JSON.parse(result.stdout || "[]");
}

(async () => {
  const context = await chromium.launchPersistentContext(userDataDir, {
    executablePath: edgePath,
    headless: false,
    viewport: { width: 1100, height: 800 },
    args: ["--window-position=-30000,-30000", "--no-first-run", "--force-renderer-accessibility=complete"],
  });
  try {
    const page = context.pages()[0] || await context.newPage();
    await page.goto(pdfUrl, { waitUntil: "load", timeout: 30000 });
    await page.waitForTimeout(2500);
    await page.bringToFront();
    const nodes = readEdgeWindow(query);
    const relevant = nodes.filter((node) =>
      node.Name === query || /\d+\s*\/\s*\d+/.test(node.Name || "") ||
      /find|查找|search|搜索/i.test(`${node.Name || ""} ${node.Id || ""}`)
    );
    console.log(JSON.stringify({
      query,
      relevant,
      controls: nodes.filter((node) => node.Name).slice(0, 160),
    }, null, 2));
  } finally {
    await context.close();
  }
})().catch((error) => {
  console.error(error.stack || String(error));
  process.exitCode = 1;
});
