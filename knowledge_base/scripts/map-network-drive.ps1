[CmdletBinding(SupportsShouldProcess=$true, ConfirmImpact="Medium")]
param(
    [Parameter(Mandatory=$true)][ValidatePattern("^[D-Zd-z]$")][string]$DriveLetter,
    [Parameter(Mandatory=$true)][ValidatePattern("^\\\\[^\\]+\\[^\\]+")][string]$NetworkPath,
    [switch]$Persist
)
$ErrorActionPreference = "Stop"
$name = $DriveLetter.ToUpperInvariant()
if (Get-PSDrive -Name $name -ErrorAction SilentlyContinue) { throw "Drive $name`: is already in use. Choose another letter." }
if (-not (Test-Path -LiteralPath $NetworkPath)) { throw "The network path is unavailable or this account does not have access: $NetworkPath" }
$description = if ($Persist) { "Create persistent network-drive mapping" } else { "Create network-drive mapping for this session" }
if ($PSCmdlet.ShouldProcess("$name`: -> $NetworkPath", $description)) {
    New-PSDrive -Name $name -PSProvider FileSystem -Root $NetworkPath -Persist:$Persist -Scope Global -ErrorAction Stop | Format-Table Name, Root, Description
    Write-Host "To remove this mapping later, run: Remove-PSDrive -Name $name"
}
