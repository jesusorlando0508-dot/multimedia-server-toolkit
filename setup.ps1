# setup_gui.ps1 - WPF installer for the multimedia web project
# Usage: Run PowerShell as Administrator, then: .\setup_gui.ps1
# NOTE: Requires Node.js/npm installed and access to create junctions (admin).

Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-PythonExe {
    $pyCmd = (Get-Command python.exe -ErrorAction SilentlyContinue)
    if ($pyCmd) { return $pyCmd.Path }
    $pyCmd = (Get-Command py.exe -ErrorAction SilentlyContinue)
    if ($pyCmd) { return "$pyCmd -3" }
    return $null
}

[xml]$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Setup - Multimedia Server" Height="600" Width="900" WindowStartupLocation="CenterScreen">
  <Grid Margin="10">
    <Grid.ColumnDefinitions>
      <ColumnDefinition Width="*"/>
      <ColumnDefinition Width="420"/>
    </Grid.ColumnDefinitions>

    <StackPanel Grid.Column="0" Margin="6">
      <TextBlock FontSize="18" FontWeight="Bold">Instalador multimedia (GUI - WPF)</TextBlock>
      <TextBlock Margin="0,8,0,12">Selecciona carpetas raíz que contienen tus bibliotecas (una por una). Cada subcarpeta se montará en media_all.</TextBlock>

      <ListBox x:Name="LbRoots" Height="220" />

      <WrapPanel Margin="0,8,0,0">
        <Button x:Name="BtnAdd" Width="120" Margin="2">Agregar carpeta</Button>
        <Button x:Name="BtnRemove" Width="120" Margin="2">Quitar seleccionada</Button>
        <Button x:Name="BtnClear" Width="120" Margin="2">Limpiar lista</Button>
      </WrapPanel>

      <Separator Margin="0,10,0,10" />

      <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
        <TextBlock VerticalAlignment="Center">Carpeta destino (media_all):</TextBlock>
        <TextBox x:Name="TbMediaAll" Width="360" Margin="8,0,8,0" />
        <Button x:Name="BtnSelectMediaAll" Width="90">Seleccionar</Button>
      </StackPanel>

      <StackPanel Orientation="Horizontal" Margin="0,10,0,0">
        <Button x:Name="BtnDetectPython" Width="160">Detectar Python</Button>
        <TextBlock x:Name="TbPython" Margin="10,2,0,0" VerticalAlignment="Center" />
      </StackPanel>

      <Separator Margin="0,10,0,10" />

      <Button x:Name="BtnStart" Height="44" Background="#FF2D7FFF" Foreground="White" FontWeight="Bold">Iniciar instalación</Button>

    </StackPanel>

    <StackPanel Grid.Column="1" Margin="6">
      <TextBlock FontSize="16" FontWeight="Bold">Registro / Estado</TextBlock>
      <TextBox x:Name="TbLog" AcceptsReturn="True" VerticalScrollBarVisibility="Auto" TextWrapping="Wrap" Height="420" IsReadOnly="True"/>
      <StackPanel Margin="0,8,0,0">
        <ProgressBar x:Name="PbProgress" Height="18" Minimum="0" Maximum="100" />
        <TextBlock x:Name="TbStatus" Margin="0,4,0,0" FontStyle="Italic" />
      </StackPanel>

      <StackPanel Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,8,0,0">
        <Button x:Name="BtnOpenRoot" Width="120" Margin="2">Abrir raíz</Button>
        <Button x:Name="BtnExit" Width="120" Margin="2">Salir</Button>
      </StackPanel>
    </StackPanel>
  </Grid>
</Window>
"@

$reader = (New-Object System.Xml.XmlNodeReader $xaml)
$window = [Windows.Markup.XamlReader]::Load($reader)

# Controls
$LbRoots = $window.FindName('LbRoots')
$BtnAdd = $window.FindName('BtnAdd')
$BtnRemove = $window.FindName('BtnRemove')
$BtnClear = $window.FindName('BtnClear')
$TbMediaAll = $window.FindName('TbMediaAll')
$BtnSelectMediaAll = $window.FindName('BtnSelectMediaAll')
$BtnDetectPython = $window.FindName('BtnDetectPython')
$TbPython = $window.FindName('TbPython')
$BtnStart = $window.FindName('BtnStart')
$TbLog = $window.FindName('TbLog')
$BtnOpenRoot = $window.FindName('BtnOpenRoot')
$BtnExit = $window.FindName('BtnExit')
$PbProgress = $window.FindName('PbProgress')
$TbStatus = $window.FindName('TbStatus')

function Log-Info {
    param(
        [string]$Message
    )
    $TbLog.Dispatcher.Invoke([action]{
        $TbLog.AppendText((Get-Date -Format "HH:mm:ss") + " - " + $Message + "`r`n")
        $TbLog.ScrollToEnd()
    }) | Out-Null
}

function Update-ProgressUI {
    param(
        [double]$Percent,
        [string]$StatusText
    )
    if ($Percent -lt 0) { $Percent = 0 }
    if ($Percent -gt 100) { $Percent = 100 }
    if ($PbProgress) {
        $PbProgress.Dispatcher.Invoke([action]{ $PbProgress.Value = $Percent }) | Out-Null
    }
    if ($TbStatus -and $StatusText) {
        $TbStatus.Dispatcher.Invoke([action]{ $TbStatus.Text = $StatusText }) | Out-Null
    }
}

function Process-JobOutput {
    param(
        [string]$Line
    )
    if ([string]::IsNullOrWhiteSpace($Line)) { return }
    if ($Line.StartsWith('__PROGRESS__|')) {
        $parts = $Line.Split('|',3)
        if ($parts.Length -ge 3) {
            $pct = 0
            [double]::TryParse($parts[1], [ref]$pct) | Out-Null
            $msg = $parts[2]
            Update-ProgressUI -Percent $pct -StatusText $msg
        }
        return
    }
    Log-Info $Line
}

function Initialize-MediaAllPath {
    if (-not [string]::IsNullOrWhiteSpace($TbMediaAll.Text)) { return }
    $defaultMedia = Join-Path (Get-Location).Path 'media_all'
    $TbMediaAll.Text = $defaultMedia
    Log-Info "📁 Carpeta destino media_all detectada automáticamente: $defaultMedia"
}

Update-ProgressUI -Percent 0 -StatusText 'En espera de iniciar instalación.'
Initialize-MediaAllPath

$script:ActiveJob = $null
$script:JobMonitor = New-Object System.Windows.Threading.DispatcherTimer
$script:JobMonitor.Interval = [TimeSpan]::FromMilliseconds(700)
$script:JobMonitor.add_Tick({
    if (-not $script:ActiveJob) {
        $script:JobMonitor.Stop()
        return
    }
    $lines = Receive-Job -Job $script:ActiveJob -ErrorAction SilentlyContinue
    foreach ($line in $lines) { Process-JobOutput $line }
    if ($script:ActiveJob.State -in @('Completed','Failed','Stopped')) {
        $remaining = Receive-Job -Job $script:ActiveJob -ErrorAction SilentlyContinue
        foreach ($line in $remaining) { Process-JobOutput $line }
        switch ($script:ActiveJob.State) {
            'Completed' {
                Update-ProgressUI -Percent 100 -StatusText 'Instalación completada.'
                Log-Info "✅ Job de instalación finalizado correctamente."
            }
            'Failed' {
                Update-ProgressUI -Percent 100 -StatusText 'Job falló.'
                Log-Info "❌ Job de instalación falló. Revisa los mensajes anteriores para detalles."
            }
            Default {
                Update-ProgressUI -Percent 100 -StatusText "Job detenido ($($script:ActiveJob.State))."
                Log-Info "⚠ Job de instalación detenido (estado: $($script:ActiveJob.State))."
            }
        }
        Remove-Job $script:ActiveJob | Out-Null
        $script:ActiveJob = $null
        $script:JobMonitor.Stop()
    }
})

function Browse-Folder([string]$title) {
    $f = New-Object System.Windows.Forms.FolderBrowserDialog
    $f.Description = $title
    # UseDescriptionForTitle no existe en Windows PowerShell clásico
    if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { 
        return $f.SelectedPath 
    } else { 
        return $null 
    }
}

$BtnAdd.Add_Click({
    $p = Browse-Folder "Selecciona la carpeta raíz (ej: D:\ANIME)"
    if ($p) {
        $LbRoots.Items.Add($p) | Out-Null
        Log-Info "➕ Ruta raíz agregada: $p"
    }
})

$BtnRemove.Add_Click({
    if ($LbRoots.SelectedIndex -ge 0) {
        $item = $LbRoots.SelectedItem
        $LbRoots.Items.Remove($item) | Out-Null
        Log-Info "➖ Ruta raíz removida: $item"
    }
})

$BtnClear.Add_Click({
    $LbRoots.Items.Clear()
    Log-Info "🧹 Lista de rutas raíz limpiada"
})

$BtnSelectMediaAll.Add_Click({
    $p = Browse-Folder "Selecciona la carpeta donde se creará media_all (o la propia media_all)"
    if ($p) {
        $TbMediaAll.Text = $p
        Log-Info "📁 Carpeta destino media_all seleccionada: $p"
    }
})

$BtnDetectPython.Add_Click({
    $py = Get-PythonExe
    if ($py) {
        $TbPython.Text = $py
        Log-Info "🐍 Python detectado automáticamente: $py"
    } else {
        Log-Info "⚠ Python no encontrado en PATH. Selección manual requerida."
        $sel = Browse-Folder "Selecciona carpeta que contiene python.exe (ej: C:\Python311)"
        if ($sel) {
            $exe = Join-Path $sel 'python.exe'
            if (Test-Path $exe) { 
                $TbPython.Text = $exe
                Log-Info "🐍 Python seleccionado manualmente: $exe" 
            }
            else { 
                Log-Info "❌ python.exe no encontrado en la carpeta seleccionada: $sel" 
            }
        }
    }
})

$BtnOpenRoot.Add_Click({
    Start-Process "explorer.exe" -ArgumentList (Get-Location).Path
})

$BtnExit.Add_Click({ $window.Close() })


# INSTALLATION PIPELINE
$BtnStart.Add_Click({

    Log-Info "▶ Iniciando pipeline de instalación..."
    if ($script:ActiveJob -and ($script:ActiveJob.State -notin @('Completed','Failed','Stopped'))) {
        Log-Info "⚠ Ya existe una instalación en curso. Espera a que finalice antes de iniciar otra."
        return
    }

    if ($LbRoots.Items.Count -eq 0) { 
        Log-Info "❌ Debes agregar al menos una ruta raíz antes de continuar."
        return 
    }
    if ([string]::IsNullOrWhiteSpace($TbMediaAll.Text)) { 
        Log-Info "❌ Debes seleccionar la carpeta destino media_all."
        return 
    }

    $pythonCmd = $TbPython.Text
    if (-not $pythonCmd) { 
        Log-Info "🔎 Buscando Python automáticamente..."
        $pythonCmd = Get-PythonExe 
    }
    if (-not $pythonCmd) { 
        Log-Info "❌ Python no disponible. Abortando instalación."
        return 
    }

    Log-Info "✅ Python que se usará: $pythonCmd"

    $roots = @()
    foreach ($i in 0..($LbRoots.Items.Count - 1)) { 
        $roots += $LbRoots.Items[$i] 
    }

    $projectRoot = (Get-Location).Path
    Log-Info "📂 Raíz del proyecto: $projectRoot"

    #
    # 1) PRE-STEP: Extraer datas.zip ANTES del job
    #
    $zipPath = Join-Path $projectRoot 'datas.zip'
    if (Test-Path $zipPath) {
        Log-Info "📦 Encontrado datas.zip en: $zipPath"
        Log-Info "🛠 Iniciando extracción de recursos (datas.zip)..."

        try {
            # Si quieres ser agresivo, aquí podrías limpiar cosas antes.
            [System.IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $projectRoot)
            Log-Info "✅ Extracción de datas.zip completada correctamente."
        }
        catch {
            Log-Info "❌ Error descomprimiendo datas.zip: $($_.Exception.Message)"
            Log-Info "⛔ Instalación abortada por fallo en recursos base."
            return
        }
    } else {
        Log-Info "ℹ datas.zip no encontrado. No se extraen recursos adicionales."
    }

    #
    # 2) JOB START (venv, npm, junctions, config, generador)
    #
    Log-Info "🚀 Lanzando job en segundo plano para crear entorno y servidor..."

    $job = Start-Job -ScriptBlock {
        param($roots, $mediaAll, $pythonCmd, $projectRoot)

        function JLog([string]$m) { "$(Get-Date -Format 'HH:mm:ss') - $m" }
        function JProgress([double]$pct, [string]$msg) {
            Write-Output "__PROGRESS__|$pct|$msg"
        }

        Set-Location $projectRoot
        Write-Output (JLog "➡ Job iniciado. Proyecto en: $projectRoot")
        JProgress 5 "Preparando entorno en $projectRoot"

        #
        # Paso 2: Crear venv
        #
        Write-Output (JLog "🐍 [1/6] Creando entorno virtual (venv)...")
        JProgress 12 "Creando entorno virtual (venv)..."
        & $pythonCmd -m venv (Join-Path $projectRoot 'venv')

        $venvPython = Join-Path $projectRoot 'venv\\Scripts\\python.exe'
        if (-not (Test-Path $venvPython)) { 
            Write-Output (JLog "❌ venv Python no encontrado en: $venvPython. Abortando job.")
            JProgress 100 "Error: venv Python no encontrado."
            exit 1 
        }
        Write-Output (JLog "✅ venv creado correctamente. Python venv: $venvPython")
        JProgress 22 "Entorno virtual creado."

        #
        # Paso 3: Instalar requirements
        #
        $req = Join-Path $projectRoot 'requirements.txt'
        if (Test-Path $req) {
            Write-Output (JLog "📦 [2/6] Instalando dependencias Python desde requirements.txt...")
            JProgress 32 "Instalando dependencias Python..."
            & $venvPython -m pip install -r $req
            Write-Output (JLog "✅ Dependencias Python instaladas.")
            JProgress 40 "Dependencias Python instaladas."
        } else {
            Write-Output (JLog "ℹ requirements.txt no encontrado, se omite instalación de dependencias Python.")
            JProgress 32 "Sin requirements.txt, se continua."
        }

        #
        # Paso 4: Crear package.json
        #
        Write-Output (JLog "📄 [3/6] Generando package.json para servidor Node...")
        JProgress 45 "Generando package.json..."
        $pkg = @'
{
  "name": "vista-server",
  "version": "1.0.0",
  "type": "module",
  "main": "server.js",
  "dependencies": {
    "express": "^4.18.2",
    "mime": "^3.0.0"
  }
}
'@
        Set-Content -LiteralPath (Join-Path $projectRoot 'package.json') -Value $pkg -Encoding UTF8
        Write-Output (JLog "✅ package.json creado.")

        #
        # Paso 5: Generar server.js
        #
        Write-Output (JLog "🖥 [4/6] Generando server.js (servidor Express)...")
        JProgress 50 "Generando server.js..."
        $serverJs = @'
import express from "express";
import fs from "fs";
import path from "path";
import mime from "mime";

const app = express();
const __dirname = path.resolve();

let mediaRoot = null;

try {
  const cfgPath = path.join(__dirname, ".vista", "config.json");
  if (fs.existsSync(cfgPath)) {
    const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8") || "{}");
    if (cfg.media_root_dir) {
      mediaRoot = cfg.media_root_dir;
      if (!path.isAbsolute(mediaRoot)) mediaRoot = path.join(__dirname, mediaRoot);
    }
  }
} catch (e) { console.warn("Config read error:", e.message); }

const fallbackMedia = path.join(__dirname, "media_all");
const finalMediaRoot = mediaRoot && fs.existsSync(mediaRoot) ? mediaRoot : (fs.existsSync(fallbackMedia) ? fallbackMedia : null);

if (finalMediaRoot) {
  app.use("/media", express.static(finalMediaRoot));
  console.log("Serving /media from:", finalMediaRoot);
} else {
  console.warn("No media root configured or media_all not found. /media will not be served.");
}

app.use(express.static(__dirname, { extensions: ["html","js","json"] }));

app.get("/", (req, res) => { 
  res.sendFile(path.join(__dirname, "Carusel.html")); 
});

const projectFolder = path.basename(__dirname);
app.use(`/${projectFolder}`, (req, res) => { 
  res.redirect(req.originalUrl.replace(new RegExp(`^/${projectFolder}`), "")); 
});

app.use('/video', (req, res) => {
  if (!finalMediaRoot) return res.status(500).send("Media root no configurado.");
  let relPath = req.path.replace(/^\/+/,'');
  const videoPath = path.join(finalMediaRoot, relPath);
  if (!fs.existsSync(videoPath)) return res.status(404).send('Archivo no encontrado');
  const range = req.headers.range; if (!range) return res.status(400).send('Requiere Range header');
  const size = fs.statSync(videoPath).size; const CHUNK = 1000000; const start = Number(range.replace(/\\D/g,'')); const end = Math.min(start+CHUNK, size-1);
  res.writeHead(206, { 
    'Content-Range': `bytes ${start}-${end}/${size}`, 
    'Accept-Ranges': 'bytes', 
    'Content-Length': end-start+1, 
    'Content-Type': mime.getType(videoPath) 
  });
  fs.createReadStream(videoPath, { start, end }).pipe(res);
});

app.get('/skip', (req, res) => {
  try {
    if (!finalMediaRoot) return res.status(500).json({ error: 'Media root no configurado' });
    const videoParam = decodeURIComponent(req.query.video || ''); 
    if (!videoParam) return res.status(400).json({ error: 'Missing video parameter' });

    let full = videoParam; 
    if (videoParam.startsWith('/media/')) 
      full = path.join(finalMediaRoot, videoParam.replace('/media/','')); 
    else if (!path.isAbsolute(videoParam)) 
      full = path.join(__dirname, videoParam);

    full = path.normalize(full);
    const folder = path.dirname(full); 
    const filename = path.basename(full); 
    const episodeNumber = parseInt((filename.match(/(\\d+)/) || ['',''])[1],10);

    const skipFile = path.join(folder, 'skip.json'); 
    if (!fs.existsSync(skipFile)) 
      return res.status(404).json({ error: 'skip.json not found', path: skipFile });

    const json = JSON.parse(fs.readFileSync(skipFile, 'utf8')); 
    const epData = (json.episodes || []).find(e => Number(e.episode) === episodeNumber);

    if (!epData) return res.status(404).json({ error: `No skip data para el episodio ${episodeNumber}` });
    res.json(json);
  } catch (err) { 
    res.status(500).json({ error: err.message }); 
  }
});

const PORT = 3000; 
app.listen(PORT, '0.0.0.0', () => { 
  console.log(`🚀 Server on: http://localhost:${PORT}`); 
});
'@
        Set-Content -LiteralPath (Join-Path $projectRoot 'server.js') -Value $serverJs -Encoding UTF8
        Write-Output (JLog "✅ server.js generado.")
        JProgress 55 "server.js listo."

        #
        # Paso 6: npm install
        #
        Write-Output (JLog "📦 [5/6] Ejecutando npm install (esto puede tardar)...")
        JProgress 60 "Ejecutando npm install..."
        Push-Location $projectRoot
        npm install
        Pop-Location
        Write-Output (JLog "✅ npm install completado.")
        JProgress 70 "Dependencias npm instaladas."

        #
        # Paso 7: Crear media_all + junctions
        #
        Write-Output (JLog "🗂 [6/6] Creando media_all y junctions desde rutas raíz seleccionadas...")
        JProgress 75 "Preparando media_all y enlaces..."
        if (-not (Test-Path $mediaAll)) {
            Write-Output (JLog "📁 media_all no existe. Creando en: $mediaAll")
            New-Item -ItemType Directory -Path $mediaAll | Out-Null
        }

        foreach ($r in $roots) {
            if (-not (Test-Path $r)) { 
                Write-Output (JLog "⚠ Ruta raíz no existe (omitida): $r"); 
                continue 
            }
            Write-Output (JLog "🔗 Procesando subcarpetas de: $r")
            Get-ChildItem $r -Directory | ForEach-Object {
                $subName = $_.Name
                $targetPath = $_.FullName
                $linkPath = Join-Path $mediaAll $subName

                if (-not (Test-Path $linkPath)) {
                    New-Item -ItemType Junction -Path $linkPath -Target $targetPath | Out-Null
                    Write-Output (JLog "✔ Junction creada: $subName → $targetPath")
                } else {
                    Write-Output (JLog "ℹ Ya existía junction/carpeta para: $subName (no se modifica).")
                }
            }
        }
        JProgress 82 "media_all configurado."

        #
        # Paso 8: Guardar config .vista/config.json
        #
        Write-Output (JLog "💾 Guardando configuración en .vista/config.json ...")
        JProgress 86 "Guardando configuración..."
        $cfgDir = Join-Path $projectRoot '.vista'
        if (-not (Test-Path $cfgDir)) { 
            New-Item -ItemType Directory -Path $cfgDir | Out-Null 
        }

        $cfgObj = @{ media_root_dir = (Resolve-Path -LiteralPath $mediaAll).ProviderPath }
        $cfgObj | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $cfgDir 'config.json') -Encoding UTF8

        Write-Output (JLog "✅ Configuración guardada correctamente.")
        JProgress 90 "Configuración guardada."

        #
        # Paso 9: Ejecutar generador Python
        #
        Write-Output (JLog "🧬 Ejecutando generador Python: python -m src.main (dentro del venv)...")
        JProgress 94 "Ejecutando generador Python..."
        & $venvPython -m src.main
        Write-Output (JLog "✅ Generador Python completado.")
        JProgress 100 "Instalación completada."

        Write-Output (JLog "🎉 INSTALACIÓN COMPLETA. Proyecto listo para usar.")
    } -ArgumentList ($roots, $TbMediaAll.Text, $pythonCmd, $projectRoot)

    $script:ActiveJob = $job
    Update-ProgressUI -Percent 5 -StatusText 'Job iniciado, preparando entorno...'
    if (-not $script:JobMonitor.IsEnabled) {
        $script:JobMonitor.Start()
    }

    Log-Info "⏳ Instalación en segundo plano iniciada. Sigue el log para ver el progreso..."
})

# Show window
$window.ShowDialog() | Out-Null
