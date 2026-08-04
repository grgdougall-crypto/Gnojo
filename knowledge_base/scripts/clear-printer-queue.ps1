[CmdletBinding(SupportsShouldProcess=$true, ConfirmImpact="High")]
param([Parameter(Mandatory=$true)][ValidateNotNullOrEmpty()][string]$PrinterName)
$ErrorActionPreference = "Stop"
$printer = Get-Printer -Name $PrinterName -ErrorAction Stop
$jobs = @(Get-PrintJob -PrinterName $printer.Name -ErrorAction SilentlyContinue)
if ($jobs.Count -eq 0) { Write-Host "The '$($printer.Name)' queue is already empty."; return }
Write-Host "Jobs currently queued for '$($printer.Name)':"
$jobs | Select-Object Id, DocumentName, JobStatus, SubmittedTime | Format-Table -AutoSize
$target = "$($jobs.Count) job(s) in printer queue '$($printer.Name)'"
if ($PSCmdlet.ShouldProcess($target, "Permanently remove queued print jobs")) {
    foreach ($job in $jobs) { Remove-PrintJob -PrinterName $printer.Name -ID $job.Id -Confirm:$false -ErrorAction Stop }
    Write-Host "Removed $($jobs.Count) queued job(s). The printer, driver, and port were not changed."
}
