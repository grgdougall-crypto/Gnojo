param([string]$OutputPath = ".\Gnojo-Windows-System-Snapshot.txt")
$ErrorActionPreference = "Continue"
$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add("Gnojo Windows System Snapshot")
$lines.Add("Collected: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')")
$lines.Add("Purpose: Read-only evidence collection; no settings are changed.")
$lines.Add("`n=== WINDOWS AND HARDWARE ===")
$computer = Get-ComputerInfo | Select-Object WindowsProductName, WindowsVersion, OsArchitecture, OsLastBootUpTime, CsName, CsManufacturer, CsModel, CsProcessors, CsTotalPhysicalMemory
$lines.Add(($computer | Format-List | Out-String).Trim())
$lines.Add("`n=== STORAGE ===")
$lines.Add((Get-Volume | Select-Object DriveLetter, FileSystemLabel, HealthStatus, @{N="SizeGB";E={[math]::Round($_.Size/1GB,2)}}, @{N="FreeGB";E={[math]::Round($_.SizeRemaining/1GB,2)}} | Format-Table -AutoSize | Out-String).Trim())
$lines.Add("`n=== NETWORK ADAPTERS ===")
$lines.Add((Get-NetAdapter | Select-Object Name, InterfaceDescription, Status, LinkSpeed, MacAddress | Format-Table -AutoSize | Out-String).Trim())
$lines.Add("`n=== IP CONFIGURATION ===")
$lines.Add((Get-NetIPConfiguration | Select-Object InterfaceAlias, IPv4Address, IPv4DefaultGateway, DNSServer | Format-List | Out-String).Trim())
$lines | Set-Content -LiteralPath $OutputPath -Encoding UTF8
Write-Host "Report saved to $((Resolve-Path -LiteralPath $OutputPath).Path)"
