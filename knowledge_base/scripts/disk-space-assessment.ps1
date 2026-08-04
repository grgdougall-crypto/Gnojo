param([string]$OutputPath = ".\Gnojo-Disk-Space-Assessment.txt")
$ErrorActionPreference = "Continue"
$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add("Gnojo Disk Space Assessment`nCollected: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')`nPurpose: Read-only evidence collection; no files are deleted.")
$lines.Add("`n=== VOLUMES ===")
$lines.Add((Get-Volume | Select-Object DriveLetter, FileSystemLabel, HealthStatus, @{N="SizeGB";E={[math]::Round($_.Size/1GB,2)}}, @{N="FreeGB";E={[math]::Round($_.SizeRemaining/1GB,2)}} | Format-Table -AutoSize | Out-String).Trim())
$lines.Add("`n=== CURRENT USER TOP-LEVEL FOLDERS ===")
$folders = Get-ChildItem -LiteralPath $env:USERPROFILE -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object {
  $size = (Get-ChildItem -LiteralPath $_.FullName -File -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
  [pscustomobject]@{Folder=$_.FullName; SizeGB=[math]::Round(($size/1GB),2)}
} | Sort-Object SizeGB -Descending
$lines.Add(($folders | Format-Table -AutoSize | Out-String).Trim())
$tempSize = (Get-ChildItem -LiteralPath $env:TEMP -File -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
$lines.Add("`nWindows temporary-folder size (GB): $([math]::Round(($tempSize/1GB),2))`nNothing was removed.")
$lines | Set-Content -LiteralPath $OutputPath -Encoding UTF8
Write-Host "Report saved to $((Resolve-Path -LiteralPath $OutputPath).Path)"
