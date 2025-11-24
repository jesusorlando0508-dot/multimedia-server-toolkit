# Multimedia Server Toolkit – Guía completa

Plataforma híbrida (Python + Node.js) para indexar bibliotecas de anime/películas, traducir metadatos y servir páginas estáticas listas para streaming o reproducción local.

## Tabla de contenidos

1. [Características](#características)
2. [Requisitos](#requisitos)
3. [Estructura del proyecto](#estructura-del-proyecto)
4. [Instalación manual](#instalación-manual)
5. [Modo automático (`setup.ps1`)](#modo-automático-setupps1)
6. [Uso de la interfaz](#uso-de-la-interfaz)
7. [Servidor estático](#servidor-estático)
8. [Mensajes de depuración y traductores](#mensajes-de-depuración-y-traductores)
9. [Notas para Carusel y TMDB](#notas-para-carusel-y-tmdb)
10. [Aviso de uso de APIs y contenido](#aviso-de-uso-de-apis-y-contenido)

## Características

- Gestión de rutas de medios y generación de páginas HTML desde plantillas.
- Integración con traductores: Marian local, Facebook M2M100, AventIQ y DeepL API.
- Panel Debug en Tkinter con logs en tiempo real.
- Servidor Express listo para servir `/media` y páginas HTML.
- Script de instalación (`setup.ps1`) que automatiza `venv`, `npm` y junctions.

## Requisitos

| Tipo | Detalle |
| --- | --- |
| OS | Windows 10/11 (PowerShell para `setup.ps1`) |
| Python | 3.11+ con `pip` |
| Node.js | 18 o superior |
| Dependencias | `pip install -r requirements.txt` |
| Modelos opcionales | Directorios configurados en `.vista/config.json` |

## Estructura del proyecto

- `src/main.py`: entrada principal (UI Tkinter).
- `src/gui/config_gui.py`: asistente de configuración y pruebas de traductores.
- `src/core/`: configuración, logging, caché y utilidades.
- `src/builder/`: generador de páginas y extractores.
- `src/translator/`: implementación de backends de traducción.
- `server.js`: servidor Express (Node) para contenido estático y `/media`.
- `setup.ps1`: instalador/automatizador WPF.
- `.vista/`: carpeta oculta con `config.json`, `.secrets.json` y `.env`.

## Instalación manual

1. **Clona el repositorio**
   ```powershell
   git clone https://github.com/jesusorlando0508-dot/multimedia-server-toolkit.git
   cd multimedia-server-toolkit
   ```
2. **Crea y activa el entorno virtual**
   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   ```
3. **Instala dependencias**
   ```powershell
   pip install -r requirements.txt
   ```
4. **Configura modelos locales (opcional)** copiando los pesos de Marian/M2M100/AventIQ en las rutas definidas en `config.json`.
5. **Inicia la aplicación**
   ```powershell
   python -m src.main
   ```

### Primer arranque

- Se abre el asistente para definir `pages_output_dir`, `media_root_dir`, JSONs de salida y plantilla.
- Selecciona el backend de traducción (`local`, `m2m100`, `aventiq`, `deepl` o `auto`).
- Las claves DeepL/TMDB se guardan en `.secrets.json` o `.env`.

### Utilidades desde la UI

- **Verificar traductores**: comprueba la disponibilidad de cada backend.
- **Probar traducción (A)**: muestra backend activo y tiempo de respuesta.
- **Generar página**: ejecuta los constructores de `builder/page_builder.py`.
- **Panel Debug**: pestañas “Errors” y “Processes” con logs en vivo.

## Modo automático (`setup.ps1`)

1. Abre PowerShell como administrador en la raíz del proyecto.
2. Ejecuta:
   ```powershell
   ./setup.ps1
   ```
3. En la UI WPF:
   - Agrega carpetas raíz (se vincularán dentro de `media_all`).
   - Selecciona la carpeta destino `media_all`.
   - Indica la ruta de `python.exe`.
   - Pulsa **Iniciar instalación**.

> **IMPORTANTE:** `setup.ps1` está pensado para un único uso inicial. Una vez completado podrás ejecutar:
> - `python -m src.main`
> - `node server.js`

### Pipeline automático

- Extracción de `datas.zip` (si existe).
- Creación de `venv` + instalación de `requirements.txt`.
- Generación de `package.json` y `server.js` + `npm install`.
- Creación de junctions para carpetas multimedia en `media_all`.
- Actualización de `.vista/config.json` con rutas detectadas.

## Uso de la interfaz

1. **Home**: selección rápida de títulos, estados y botones para generar páginas.
2. **Config**: abre `ensure_config_via_gui` para editar rutas, modelos y claves.
3. **Panel Debug** (`Ctrl + D`): muestra logs de proceso y errores.
4. **Generación automática**: `builder/page_builder.py` usa hilos para no bloquear la UI.

## Servidor estático

```powershell
npm install
node server.js
```

- Monta `/media` según `.vista/config.json`.
- Sirve `Carusel.html` como página principal.
- Endpoints incluidos:
  - `/video` → streaming parcial (Range).
  - `/skip` → lee `skip.json` por episodio.

## Mensajes de depuración y traductores

- `src/translator/translator.py` emite `Traductor en uso: …` al cambiar de backend.
- Los mensajes aparecen en:
  - Consola (`logging.INFO`).
  - Panel Debug → pestaña **Processes**.
  - Archivo `debug.log` (rotativo).

## Notas para Carusel y TMDB

- Si los previews del Carusel no cargan tras generar recursos y montar el servidor, ejecuta el **Extractor**:
  1. En la GUI (parte superior izquierda) pulsa el botón del extractor.
  2. Selecciona la ruta de tus páginas HTML y la ruta al `Carusel.html` principal.
  3. Asegúrate de que el archivo de metadatos se llame `anime1_info.json`.
- Para series que usan TMDB, crea `tmdb_overrides.json` con la forma:
  ```json
  {
    "Marvel Zombies": "serie",
    "Devil May Cry": "serie",
    "Halo Legends": "serie"
  }
  ```
- La GUI principal permite seleccionar proveedores (providers) visualmente para organizar mejor el contenido.

## Aviso de uso de APIs y contenido

### Uso de APIs externas
Este proyecto consume servicios de terceros (Jikan, Marian, M2M100, AventIQ, DeepL) solo para fines personales, educativos o experimentales. Respeta los términos de servicio de cada API.

### Sin fines de lucro
No está destinado a distribución comercial ni a obtener ganancias mediante servicios de terceros.

### Propiedad intelectual
Los contenidos de anime/películas y metadatos pertenecen a sus autores. Este proyecto no reclama derechos sobre ellos.

### Descargo de responsabilidad
El autor no se responsabiliza por bloqueos, cambios o limitaciones impuestas por terceros, ni por usos indebidos del software.
