param([string]$OutputPath = ".\Gnojo-Printer-Diagnostic-Report.txt")
$ErrorActionPreference = "Continue"
$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add("Gnojo Printer Diagnostic Report`nCollected: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')`nPurpose: Read-only evidence collection; queues and services are not changed.")
$lines.Add("`n=== INSTALLED PRINTERS ===")
$printers = Get-Printer -ErrorAction SilentlyContinue
$lines.Add(($printers | Select-Object Name, PrinterStatus, DriverName, PortName, Shared | Format-Table -AutoSize | Out-String).Trim())
$lines.Add("`n=== QUEUED JOBS ===")
foreach ($printer in $printers) {
  $jobs = Get-PrintJob -PrinterName $printer.Name -ErrorAction SilentlyContinue
  if ($jobs) { $lines.Add("`nPrinter: $($printer.Name)"); $lines.Add(($jobs | Select-Object Id, DocumentName, JobStatus, SubmittedTime | Format-Table -AutoSize | Out-String).Trim()) }
}
$lines.Add("`n=== PRINT SPOOLER ===")
$lines.Add((Get-Service -Name Spooler | Select-Object Status, Name, DisplayName | Format-List | Out-String).Trim())
$lines.Add("`n=== RECENT PRINT SERVICE ERRORS ===")
$lines.Add((Get-WinEvent -FilterHashtable @{LogName="Microsoft-Windows-PrintService/Admin"; Level=1,2; StartTime=(Get-Date).AddDays(-3)} -MaxEvents 20 -ErrorAction SilentlyContinue | Select-Object TimeCreated, Id, Message | Format-List | Out-String).Trim())
$lines | Set-Content -LiteralPath $OutputPath -Encoding UTF8
Write-Host "Report saved to $((Resolve-Path -LiteralPath $OutputPath).Path)"
