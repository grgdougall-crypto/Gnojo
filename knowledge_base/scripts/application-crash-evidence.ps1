param([Parameter(Mandatory=$true)][string]$ApplicationName, [int]$Days = 3, [string]$OutputPath = ".\Gnojo-Application-Crash-Evidence.txt")
$ErrorActionPreference = "Continue"
$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add("Gnojo Application Crash Evidence`nApplication filter: $ApplicationName`nCollected: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')`nPurpose: Read-only event collection; the application is not stopped or changed.")
$lines.Add("`n=== WINDOWS ===")
$lines.Add((Get-ComputerInfo | Select-Object WindowsProductName, WindowsVersion, OsArchitecture | Format-List | Out-String).Trim())
$events = Get-WinEvent -FilterHashtable @{LogName="Application"; Level=2; StartTime=(Get-Date).AddDays(-$Days)} -ErrorAction SilentlyContinue | Where-Object { $_.Message -like "*$ApplicationName*" -or $_.ProviderName -like "*$ApplicationName*" } | Select-Object -First 50
$lines.Add("`n=== MATCHING APPLICATION ERRORS ===")
if ($events) { $lines.Add(($events | Select-Object TimeCreated, Id, ProviderName, Message | Format-List | Out-String).Trim()) } else { $lines.Add("No matching error events were found in the selected time range.") }
$lines | Set-Content -LiteralPath $OutputPath -Encoding UTF8
Write-Host "Report saved to $((Resolve-Path -LiteralPath $OutputPath).Path)"
