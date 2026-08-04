param([string]$TestHost = "", [string]$OutputPath = ".\Gnojo-Network-Report.txt")
$ErrorActionPreference = "Continue"
$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add("Gnojo Network Connectivity Report`nCollected: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')`nPurpose: Read-only evidence collection; network settings are not changed.")
$lines.Add("`n=== ADAPTERS ===")
$lines.Add((Get-NetAdapter | Select-Object Name, Status, LinkSpeed, MacAddress, InterfaceDescription | Format-Table -AutoSize | Out-String).Trim())
$lines.Add("`n=== IP, GATEWAY, AND DNS ===")
$lines.Add((Get-NetIPConfiguration | Select-Object InterfaceAlias, IPv4Address, IPv4DefaultGateway, DNSServer | Format-List | Out-String).Trim())
$lines.Add("`n=== DEFAULT ROUTES ===")
$lines.Add((Get-NetRoute -DestinationPrefix "0.0.0.0/0" | Select-Object InterfaceAlias, NextHop, RouteMetric | Format-Table -AutoSize | Out-String).Trim())
if ($TestHost) {
  $lines.Add("`n=== APPROVED HOST TEST: $TestHost ===")
  $lines.Add((Resolve-DnsName -Name $TestHost -ErrorAction SilentlyContinue | Select-Object Name, Type, IPAddress | Format-Table -AutoSize | Out-String).Trim())
  $lines.Add((Test-NetConnection -ComputerName $TestHost -Port 443 -InformationLevel Detailed | Format-List | Out-String).Trim())
} else { $lines.Add("`nNo external host was tested. Re-run with -TestHost only for an approved destination.") }
$lines | Set-Content -LiteralPath $OutputPath -Encoding UTF8
Write-Host "Report saved to $((Resolve-Path -LiteralPath $OutputPath).Path)"
