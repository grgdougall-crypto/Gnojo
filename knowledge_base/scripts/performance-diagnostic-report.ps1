param([string]$OutputPath = ".\Gnojo-Performance-Report.txt")
$ErrorActionPreference = "Continue"
$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add("Gnojo Performance Diagnostic Report`nCollected: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')`nPurpose: Read-only evidence collection; no processes are stopped.")
$os = Get-CimInstance Win32_OperatingSystem
$lines.Add("`n=== SUMMARY ===`nLast boot: $($os.LastBootUpTime)`nFree physical memory (MB): $([math]::Round($os.FreePhysicalMemory/1KB,0))")
$lines.Add("`n=== TOP PROCESSOR TIME ===")
$lines.Add((Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 Name, Id, @{N="CPUSeconds";E={[math]::Round($_.CPU,2)}}, @{N="MemoryMB";E={[math]::Round($_.WorkingSet64/1MB,1)}} | Format-Table -AutoSize | Out-String).Trim())
$lines.Add("`n=== TOP MEMORY ===")
$lines.Add((Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 10 Name, Id, @{N="MemoryMB";E={[math]::Round($_.WorkingSet64/1MB,1)}} | Format-Table -AutoSize | Out-String).Trim())
$lines.Add("`n=== STORAGE ===")
$lines.Add((Get-Volume | Select-Object DriveLetter, HealthStatus, @{N="FreeGB";E={[math]::Round($_.SizeRemaining/1GB,2)}}, @{N="SizeGB";E={[math]::Round($_.Size/1GB,2)}} | Format-Table -AutoSize | Out-String).Trim())
$lines.Add("`n=== RECENT SYSTEM ERRORS ===")
$lines.Add((Get-WinEvent -FilterHashtable @{LogName="System"; Level=1,2; StartTime=(Get-Date).AddDays(-1)} -MaxEvents 20 -ErrorAction SilentlyContinue | Select-Object TimeCreated, Id, ProviderName, Message | Format-List | Out-String).Trim())
$lines | Set-Content -LiteralPath $OutputPath -Encoding UTF8
Write-Host "Report saved to $((Resolve-Path -LiteralPath $OutputPath).Path)"
