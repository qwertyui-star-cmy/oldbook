$ErrorActionPreference = "Stop"

$desktop = [Environment]::GetFolderPath("Desktop")
$linkPath = Join-Path $desktop "文本精准定位古籍.lnk"
$projectRoot = $PSScriptRoot
$targetPath = Join-Path $projectRoot "start.cmd"
$iconPath = Join-Path $projectRoot "assets\app-icon-circle-only.ico"

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($linkPath)
$link.TargetPath = $targetPath
$link.WorkingDirectory = $projectRoot
$link.IconLocation = "$iconPath,0"
$link.Description = "启动文本精准定位古籍"
$link.Save()

$saved = $shell.CreateShortcut($linkPath)
[pscustomobject]@{
  Shortcut = $linkPath
  Target = $saved.TargetPath
  Icon = $saved.IconLocation
  Updated = (Get-Item -LiteralPath $linkPath).LastWriteTime
}
