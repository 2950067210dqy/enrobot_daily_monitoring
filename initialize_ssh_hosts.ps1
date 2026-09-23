[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-ConfiguredPath {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$DefaultPath
    )

    $path = $Value.Trim()
    if (-not $path) {
        $path = $DefaultPath
    }
    $path = [Environment]::ExpandEnvironmentVariables($path)
    if ($path -eq "~") {
        return $env:USERPROFILE
    }
    if ($path.StartsWith("~\")) {
        return Join-Path $env:USERPROFILE $path.Substring(2)
    }
    return [IO.Path]::GetFullPath($path)
}

function Set-SystemOnlyFileAcl {
    param([Parameter(Mandatory = $true)][string]$Path)

    $systemSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-18")
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $fullControl = [Security.AccessControl.FileSystemRights]::FullControl
    $fileAcl = New-Object Security.AccessControl.FileSecurity
    $fileAcl.SetOwner($systemSid)
    $fileAcl.SetAccessRuleProtection($true, $false)
    $fileAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($systemSid, $fullControl, $allow)))
    Set-Acl -LiteralPath $Path -AclObject $fileAcl
}

function Get-SecretString {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$Name,
        [string]$DefaultValue = ""
    )

    $escapedName = [regex]::Escape($Name)
    $pattern = "(?m)^\s*$escapedName\s*=\s*[rRuUbB]*[`"'](?<value>.*?)[`"']\s*(?:#.*)?$"
    $match = [regex]::Match($Text, $pattern)
    if (-not $match.Success) {
        return $DefaultValue
    }
    return $match.Groups["value"].Value
}

function Get-SecretPort {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$Name,
        [int]$DefaultValue = 22
    )

    $escapedName = [regex]::Escape($Name)
    $pattern = "(?m)^\s*$escapedName\s*=\s*(?<value>\d+)\s*(?:#.*)?$"
    $match = [regex]::Match($Text, $pattern)
    if (-not $match.Success) {
        return $DefaultValue
    }
    return [int]$match.Groups["value"].Value
}

if (-not (Test-IsAdministrator)) {
    Write-Host "Requesting administrator permission..." -ForegroundColor Yellow
    $arguments = @(
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", ('"{0}"' -f $PSCommandPath)
    )
    $process = Start-Process powershell.exe -Verb RunAs -ArgumentList ($arguments -join " ") -Wait -PassThru
    exit $process.ExitCode
}

$projectRoot = $PSScriptRoot
$secretFile = Join-Path $projectRoot "server_health_report\secret.py"
$sshExecutable = Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe"

foreach ($requiredFile in @($secretFile, $sshExecutable)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required file was not found: $requiredFile"
    }
}

$secretText = Get-Content -LiteralPath $secretFile -Raw -Encoding UTF8
$config = [pscustomobject]@{
    identity = Get-SecretString -Text $secretText -Name "scp_identity_file"
    system_identity = Get-SecretString -Text $secretText -Name "scp_system_identity_file"
    servers = @(
        [pscustomobject]@{
            name = "Feima 1"
            endpoint = Get-SecretString -Text $secretText -Name "scp_feima1"
            port = Get-SecretPort -Text $secretText -Name "scp_port_feima1"
        }
        [pscustomobject]@{
            name = "Feima 2"
            endpoint = Get-SecretString -Text $secretText -Name "scp_feima2"
            port = Get-SecretPort -Text $secretText -Name "scp_port_feima2"
        }
        [pscustomobject]@{
            name = "Feima client"
            endpoint = Get-SecretString -Text $secretText -Name "scp_feima_client"
            port = Get-SecretPort -Text $secretText -Name "scp_port_feima_client"
        }
    )
}

$defaultPrivateKey = Join-Path $env:USERPROFILE ".ssh\id_ed25519"
$userPrivateKey = Resolve-ConfiguredPath -Value ([string]$config.identity) -DefaultPath $defaultPrivateKey
if (-not (Test-Path -LiteralPath $userPrivateKey -PathType Leaf)) {
    throw "SSH private key was not found: $userPrivateKey"
}

$userSshDirectory = Split-Path -Parent $userPrivateKey
$userKnownHosts = Join-Path $userSshDirectory "known_hosts"
New-Item -ItemType Directory -Path $userSshDirectory -Force | Out-Null
if (-not (Test-Path -LiteralPath $userKnownHosts -PathType Leaf)) {
    New-Item -ItemType File -Path $userKnownHosts -Force | Out-Null
}

Write-Host "User private key: $userPrivateKey" -ForegroundColor Cyan
Write-Host "User known_hosts: $userKnownHosts" -ForegroundColor Cyan
Write-Host ""
Write-Host "IMPORTANT: Compare each fingerprint with the server's trusted fingerprint." -ForegroundColor Yellow
Write-Host "Enter yes only after the fingerprint has been verified." -ForegroundColor Yellow

foreach ($server in $config.servers) {
    $endpoint = ([string]$server.endpoint).Trim()
    $port = [int]$server.port
    if (-not $endpoint -or $endpoint -notmatch "^[^@]+@[^@]+$") {
        throw "Invalid endpoint for $($server.name) in secret.py: $endpoint"
    }
    if ($port -lt 1 -or $port -gt 65535) {
        throw "Invalid SSH port for $($server.name): $port"
    }

    Write-Host ""
    Write-Host "[$($server.name)] $endpoint port $port" -ForegroundColor Green
    & $sshExecutable `
        -p $port `
        -i $userPrivateKey `
        -o "ConnectTimeout=30" `
        -o "UserKnownHostsFile=$userKnownHosts" `
        -o "StrictHostKeyChecking=ask" `
        $endpoint "exit"
    if ($LASTEXITCODE -ne 0) {
        throw "SSH verification or key login failed for $endpoint (exit code $LASTEXITCODE)."
    }
    Write-Host "Verified: $endpoint" -ForegroundColor Green
}

$defaultSystemKey = "C:\ProgramData\ServerHealthMonitoring\.ssh\id_ed25519"
$systemPrivateKey = Resolve-ConfiguredPath -Value ([string]$config.system_identity) -DefaultPath $defaultSystemKey
$systemSshDirectory = Split-Path -Parent $systemPrivateKey
$systemKnownHosts = Join-Path $systemSshDirectory "known_hosts"

if ([IO.Path]::GetFullPath($systemKnownHosts) -ne [IO.Path]::GetFullPath($userKnownHosts)) {
    New-Item -ItemType Directory -Path $systemSshDirectory -Force | Out-Null

    if (Test-Path -LiteralPath $systemKnownHosts -PathType Leaf) {
        & takeown.exe /F $systemKnownHosts /A | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to take ownership of SYSTEM known_hosts: $systemKnownHosts"
        }
        & icacls.exe $systemKnownHosts /grant "*S-1-5-32-544:(F)" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to update permissions for SYSTEM known_hosts: $systemKnownHosts"
        }
    }

    Copy-Item -LiteralPath $userKnownHosts -Destination $systemKnownHosts -Force
    Set-SystemOnlyFileAcl -Path $systemKnownHosts
    Write-Host ""
    Write-Host "SYSTEM known_hosts updated: $systemKnownHosts" -ForegroundColor Green
}

Write-Host ""
Write-Host "All three SSH connections were verified successfully." -ForegroundColor Green
Write-Host "You can now run run_daily_monitoring.cmd again."
