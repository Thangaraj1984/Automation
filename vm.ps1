<#
.SYNOPSIS
    VM Container Manager — Start/Stop/Status containers on your deployment VM

.EXAMPLES
    .\vm.ps1 status                    # Show all containers
    .\vm.ps1 start nse-options-server  # Start app container
    .\vm.ps1 stop nse-options-server   # Stop app container
    .\vm.ps1 start all                 # Start nginx + nse-options-server
    .\vm.ps1 stop all                  # Stop nginx + nse-options-server
    .\vm.ps1 logs nse-options-server   # Tail last 50 log lines
    .\vm.ps1 restart nse-options-server
    .\vm.ps1 deploy                    # Rebuild & restart nse-options-server
#>

param(
    [Parameter(Position=0)]
    [ValidateSet("status","start","stop","restart","logs","deploy","help")]
    [string]$Command = "help",

    [Parameter(Position=1)]
    [string]$Target
)

$ConfigPath = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "project.config"
if (-not (Test-Path $ConfigPath)) {
    Write-Host "ERROR: Missing project.config" -ForegroundColor Red
    Write-Host "Copy project.config.example to project.config and fill your values." -ForegroundColor Yellow
    exit 1
}

$Config = @{}
Get-Content $ConfigPath | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { return }
    $parts = $line.Split('=', 2)
    $Config[$parts[0].Trim()] = $parts[1].Trim()
}

$VM_USER = $Config['VM_USER']
$VM_HOST = $Config['VM_HOST']
$SSH_KEY = $Config['SSH_KEY_PATH']
$VM_SSH_PORT = if ($Config['VM_SSH_PORT']) { $Config['VM_SSH_PORT'] } else { '22' }
$VM_DIR = if ($Config['VM_DIR']) { $Config['VM_DIR'] } else { '/home/user/nse-options' }

if (-not $VM_USER -or -not $VM_HOST -or -not $SSH_KEY) {
    Write-Host "ERROR: VM_USER, VM_HOST, SSH_KEY_PATH must be set in project.config" -ForegroundColor Red
    exit 1
}

if ($SSH_KEY.StartsWith('~')) {
    $SSH_KEY = Join-Path $HOME $SSH_KEY.Substring(2)
}

function Invoke-VM([string]$cmd) {
    ssh -p $VM_SSH_PORT -i $SSH_KEY "$VM_USER@$VM_HOST" $cmd
}

switch ($Command) {

    "status" {
        Write-Host ""
        Write-Host "  Containers on target VM:" -ForegroundColor Cyan
        Write-Host "  $('─' * 70)" -ForegroundColor DarkGray
        Invoke-VM "docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"
        Write-Host ""
    }

    "start" {
        if (-not $Target) { Write-Host "ERROR: Specify container name or 'all'" -ForegroundColor Red; return }
        switch ($Target) {
            "all" {
                Write-Host "Starting nginx + nse-options-server..." -ForegroundColor Yellow
                Invoke-VM "docker start nginx nse-options-server"
            }
            "nse-options" {
                Write-Host "Starting nginx + nse-options-server..." -ForegroundColor Yellow
                Invoke-VM "docker start nginx nse-options-server"
            }
            default {
                Write-Host "Starting $Target..." -ForegroundColor Yellow
                Invoke-VM "docker start $Target"
            }
        }
        Write-Host "Done." -ForegroundColor Green
    }

    "stop" {
        if (-not $Target) { Write-Host "ERROR: Specify container name or 'all'" -ForegroundColor Red; return }
        switch ($Target) {
            "all" {
                Write-Host "Stopping nginx + nse-options-server..." -ForegroundColor Yellow
                Invoke-VM "docker stop nse-options-server nginx 2>/dev/null"
            }
            default {
                Write-Host "Stopping $Target..." -ForegroundColor Yellow
                Invoke-VM "docker stop $Target"
            }
        }
        Write-Host "Done." -ForegroundColor Green
    }

    "restart" {
        if (-not $Target) { Write-Host "ERROR: Specify container name" -ForegroundColor Red; return }
        Write-Host "Restarting $Target..." -ForegroundColor Yellow
        Invoke-VM "docker restart $Target"
        Write-Host "Done." -ForegroundColor Green
    }

    "logs" {
        if (-not $Target) { Write-Host "ERROR: Specify container name" -ForegroundColor Red; return }
        Invoke-VM "docker logs --tail 50 $Target"
    }

    "deploy" {
        Write-Host "Deploying nse-options-server to VM..." -ForegroundColor Cyan
        $files = @("nse_server.py", "NseUtility.py", "requirements.txt", "Dockerfile", "docker-compose.yml", ".dockerignore")
        $srcDir = Split-Path -Parent $MyInvocation.ScriptName
        foreach ($f in $files) {
            Write-Host "  Uploading $f..." -ForegroundColor DarkGray
            scp -P $VM_SSH_PORT -i $SSH_KEY "$srcDir\$f" "${VM_USER}@${VM_HOST}:${VM_DIR}/"
        }
        Write-Host "  Building image (this takes ~90s)..." -ForegroundColor DarkGray
        Invoke-VM "cd ${VM_DIR} && docker compose build --no-cache"
        Write-Host "  Restarting container..." -ForegroundColor DarkGray
        Invoke-VM "cd ${VM_DIR} && docker compose up -d"
        Write-Host "Deploy complete." -ForegroundColor Green
    }

    default {
        Write-Host ""
        Write-Host "  VM Container Manager" -ForegroundColor Cyan
        Write-Host "  Usage: .\vm.ps1 [command] [target]" -ForegroundColor White
        Write-Host ""
        Write-Host "  Commands:" -ForegroundColor Yellow
        Write-Host "    status                   Show all containers"
        Write-Host "    start [name|all]         Start container(s)"
        Write-Host "    stop  [name|all]         Stop container(s)"
        Write-Host "    restart [name]            Restart a container"
        Write-Host "    logs [name]               Show last 50 log lines"
        Write-Host "    deploy                    Upload code + rebuild nse-options-server"
        Write-Host ""
        Write-Host "  Targets:" -ForegroundColor Yellow
        Write-Host "    nse-options               nginx + nse-options-server"
        Write-Host "    all                       nginx + nse-options-server"
        Write-Host "    [container-name]          Any specific container"
        Write-Host ""
        Write-Host "  Containers:" -ForegroundColor Yellow
        Write-Host "    nse-options-server         NSE Options Google Sheets feed"
        Write-Host "    nginx                      Reverse proxy + SSL"
        Write-Host "    [custom-name]              Any container on your VM"
        Write-Host ""
    }
}
