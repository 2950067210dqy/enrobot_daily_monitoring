[CmdletBinding()]
param(
    [string]$UserPrivateKey = (Join-Path $env:USERPROFILE ".ssh\id_ed25519"),
    [string]$TaskName = "ServerHealthDailyMonitoring"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')"
    }
}

if (-not (Test-IsAdministrator)) {
    Write-Host "Requesting administrator permission..." -ForegroundColor Yellow
    $elevatedArguments = @(
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", ('"{0}"' -f $PSCommandPath),
        "-UserPrivateKey", ('"{0}"' -f $UserPrivateKey),
        "-TaskName", ('"{0}"' -f $TaskName)
    )
    $process = Start-Process powershell.exe -Verb RunAs -ArgumentList ($elevatedArguments -join " ") -Wait -PassThru
    exit $process.ExitCode
}

$projectRoot = $PSScriptRoot
$requirements = Join-Path $projectRoot "requirements.txt"
$runner = Join-Path $projectRoot "run_daily_monitoring.cmd"
$secretFile = Join-Path $projectRoot "server_health_report\secret.py"
$venvDirectory = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvDirectory "Scripts\python.exe"
$userKnownHosts = Join-Path (Split-Path -Parent $UserPrivateKey) "known_hosts"
$systemSshDirectory = "C:\ProgramData\ServerHealthMonitoring\.ssh"
$systemPrivateKey = Join-Path $systemSshDirectory "id_ed25519"
$systemKnownHosts = Join-Path $systemSshDirectory "known_hosts"

Write-Host "Project: $projectRoot" -ForegroundColor Cyan

foreach ($requiredFile in @($requirements, $runner, $secretFile)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required project file is missing: $requiredFile"
    }
}

Write-Host "[1/6] Checking Windows OpenSSH Client..."
$sshExecutable = Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe"
if (-not (Test-Path -LiteralPath $sshExecutable -PathType Leaf)) {
    Write-Host "Installing Windows OpenSSH Client..." -ForegroundColor Yellow
    $capability = Get-WindowsCapability -Online |
        Where-Object { $_.Name -like "OpenSSH.Client*" } |
        Select-Object -First 1
    if ($null -eq $capability) {
        throw "Windows OpenSSH Client capability is unavailable on this computer."
    }
    Add-WindowsCapability -Online -Name $capability.Name | Out-Null
}
if (-not (Test-Path -LiteralPath $sshExecutable -PathType Leaf)) {
    throw "OpenSSH installation finished but ssh.exe was not found: $sshExecutable"
}

Write-Host "[2/6] Creating Python virtual environment..."
$venvIsHealthy = $false
if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    try {
        & $venvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 7) else 1)"
        $venvIsHealthy = ($LASTEXITCODE -eq 0)
    } catch {
        $venvIsHealthy = $false
    }
    if (-not $venvIsHealthy) {
        Write-Host "The existing .venv is invalid or was copied from another computer; rebuilding it..." -ForegroundColor Yellow
        $resolvedProjectRoot = [IO.Path]::GetFullPath($projectRoot).TrimEnd('\')
        $resolvedVenvDirectory = [IO.Path]::GetFullPath($venvDirectory).TrimEnd('\')
        $venvParent = [IO.Path]::GetDirectoryName($resolvedVenvDirectory).TrimEnd('\')
        $venvLeaf = [IO.Path]::GetFileName($resolvedVenvDirectory)
        if ($venvParent -ne $resolvedProjectRoot -or $venvLeaf -ne ".venv") {
            throw "Refusing to remove an unsafe virtual environment path: $resolvedVenvDirectory"
        }
        Remove-Item -LiteralPath $resolvedVenvDirectory -Recurse -Force
    }
}
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    $pythonCommand = $null
    $pythonPrefix = @()
    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        & $pyLauncher.Source -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 7) else 1)"
        if ($LASTEXITCODE -eq 0) {
            $pythonCommand = $pyLauncher.Source
            $pythonPrefix = @("-3")
        }
    }
    if ($null -eq $pythonCommand) {
        $python = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($null -ne $python) {
            & $python.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 7) else 1)"
            if ($LASTEXITCODE -eq 0) {
                $pythonCommand = $python.Source
            }
        }
    }
    if ($null -eq $pythonCommand) {
        throw "Python 3.7 or newer was not found. Install Python 3.7+ (32-bit or 64-bit) and run this installer again."
    }
    Invoke-Checked -FilePath $pythonCommand -Arguments ($pythonPrefix + @("-m", "venv", $venvDirectory))
}

Invoke-Checked -FilePath $venvPython -Arguments @(
    "-c",
    "import platform, sys; print('Python runtime: {} | {} | {}'.format(sys.version.split()[0], platform.architecture()[0], sys.executable))"
)

Write-Host "[3/6] Installing Python dependencies..."
Invoke-Checked -FilePath $venvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip")
Invoke-Checked -FilePath $venvPython -Arguments @("-m", "pip", "install", "--force-reinstall", "-r", $requirements)
Invoke-Checked -FilePath $venvPython -Arguments @(
    "-c",
    "import reportlab; from reportlab.graphics import renderPM; from reportlab.graphics.shapes import Drawing, Rect; d=Drawing(20,20); d.add(Rect(1,1,18,18)); data=renderPM.drawToString(d,fmt='PNG',backend='_renderPM'); print('ReportLab {} PNG backend: OK'.format(reportlab.Version))"
)

Write-Host "[4/6] Validating private configuration..."
$validationCode = "from server_health_report import secret as s; names=('scp_feima1','scp_feima2','scp_feima_client','scp_system_identity_file','email_sender','email_recipient','email_auth_code','feishu_hook_url'); missing=[n for n in names if not str(getattr(s,n,'')).strip()]; print('Missing secret.py settings: ' + ', '.join(missing) if missing else 'secret.py settings: OK'); raise SystemExit(1 if missing else 0)"
Push-Location $projectRoot
try {
    Invoke-Checked -FilePath $venvPython -Arguments @("-c", $validationCode)
} finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath $UserPrivateKey -PathType Leaf)) {
    throw "User SSH private key was not found: $UserPrivateKey"
}
if (-not (Test-Path -LiteralPath $userKnownHosts -PathType Leaf)) {
    throw "known_hosts was not found: $userKnownHosts. Connect to all three servers once and rerun deployment."
}

Write-Host "[5/6] Installing SYSTEM-only SSH key..."
New-Item -ItemType Directory -Path $systemSshDirectory -Force | Out-Null

$systemSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-18")
$administratorsSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-32-544")
$directoryAcl = New-Object Security.AccessControl.DirectorySecurity
$directoryAcl.SetOwner($administratorsSid)
$directoryAcl.SetAccessRuleProtection($true, $false)
$inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
$propagation = [Security.AccessControl.PropagationFlags]::None
$allow = [Security.AccessControl.AccessControlType]::Allow
$fullControl = [Security.AccessControl.FileSystemRights]::FullControl
$directoryAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($systemSid, $fullControl, $inheritance, $propagation, $allow)))
$directoryAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($administratorsSid, $fullControl, $inheritance, $propagation, $allow)))
Set-Acl -LiteralPath $systemSshDirectory -AclObject $directoryAcl

Copy-Item -LiteralPath $UserPrivateKey -Destination $systemPrivateKey -Force
Copy-Item -LiteralPath $userKnownHosts -Destination $systemKnownHosts -Force

foreach ($systemFile in @($systemPrivateKey, $systemKnownHosts)) {
    $fileAcl = New-Object Security.AccessControl.FileSecurity
    $fileAcl.SetOwner($systemSid)
    $fileAcl.SetAccessRuleProtection($true, $false)
    $fileAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($systemSid, $fullControl, $allow)))
    Set-Acl -LiteralPath $systemFile -AclObject $fileAcl
}

Write-Host "[6/6] Creating the daily 17:40 scheduled task..."
$taskAction = '{0} /d /c "{1}"' -f $env:ComSpec, $runner
Invoke-Checked -FilePath "schtasks.exe" -Arguments @(
    "/Create",
    "/TN", $TaskName,
    "/TR", $taskAction,
    "/SC", "DAILY",
    "/ST", "17:40",
    "/RU", "SYSTEM",
    "/RL", "HIGHEST",
    "/F"
)

Write-Host ""
Write-Host "Deployment completed." -ForegroundColor Green
Write-Host "Scheduled task: $TaskName"
Write-Host "Schedule: every day at 17:40"
Write-Host "Run as: SYSTEM (works while no user is logged in)"
Write-Host "Log: $(Join-Path $projectRoot 'logs\daily_monitoring_task.log')"
Write-Host ""
Write-Host "Recommended manual checks:" -ForegroundColor Cyan
Write-Host "  $venvPython $projectRoot\run_daily_monitoring.py --dry-run"
Write-Host "  schtasks.exe /Query /TN `"$TaskName`" /FO LIST /V"
