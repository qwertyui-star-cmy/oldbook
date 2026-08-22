$ErrorActionPreference = "Stop"

$desktop = [Environment]::GetFolderPath("Desktop")
$linkPath = Join-Path $desktop "文本精准定位古籍.lnk"
$targetPath = "E:\Tools\TextLayerRebuilder\start.cmd"
$iconPath = "E:\Tools\TextLayerRebuilder\assets\app-icon-circle-only.ico"

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($linkPath)
$link.TargetPath = $targetPath
$link.WorkingDirectory = "E:\Tools\TextLayerRebuilder"
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
