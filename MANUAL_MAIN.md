# Manual práctico: uso de `main.py`

Guía paso a paso para operar la aplicación de escritorio (Tkinter) incluida en `src/main.py`, entender sus dependencias y resolver los escenarios más comunes.

## 1. Preparación del entorno

1. **Python y venv**  
   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
2. **Node.js opcional**: solo necesario si piensas servir las páginas con `server.js`.
3. **Modelos locales** (opcional): coloca los pesos de Marian/M2M100/AventIQ en las rutas definidas por `src/core/.vista/config.json`.
4. **Archivos auxiliares**: `datas.zip` (si existe) contiene recursos iniciales; `setup.ps1` puede automatizar todo este proceso.

## 2. Dependencias clave y su propósito

| Módulo | Ubicación | Rol |
| --- | --- | --- |
| `src/core/config.py` | Configuración persistente (`.vista/config.json`) + secretos (`.secrets.json`). |
| `src/core/app_state.py` | Cola UI `ui_queue` y control de generación. |
| `src/core/ui_logging.py` | Enrutamiento de logs hacia el panel Debug. |
| `src/translator/translator.py` | Selección dinámica del backend de traducción y mensajería "Traductor en uso". |
| `src/builder/page_builder.py` | Construcción de páginas/JSON a partir de la biblioteca. |
| `src/gui/config_gui.py` | Asistente gráfico para editar configuración y probar traductores. |

Conocer estos archivos ayuda a depurar o extender la app.

## 3. Ejecución y primer inicio

1. `python -m src.main` abre la ventana principal.
2. Se lanza `ensure_initial_paths()` que detecta si es la primera ejecución.  
   - Si `config['first_run']` es `True`, se abre `ensure_config_via_gui` para definir rutas esenciales.
   - Se ejecuta `run_resource_probe_once()` para detectar CPU/RAM y ajustar parámetros del traductor.
3. Si `config['preload_translator_on_start']` es `True`, se dispara `start_background_model_load()` para precargar Marian en segundo plano.
4. El panel principal se renderiza y comienza el bucle que procesa `ui_queue` cada 200 ms.

## 4. Configuración inicial detallada

Dentro del dialogo **Ajustes**:

1. **Rutas base**: `pages_output_dir`, `media_root_dir`, JSONs (anime/movies) y plantilla.
2. **Backend de traducción**: selecciona `local`, `m2m100`, `aventiq`, `deepl` o `auto`.  
   - `auto`: prioriza DeepL (si hay API key), luego M2M100, AventIQ y por último Marian local.
3. **Credenciales**: DeepL va a `.secrets.json`; TMDB se guarda en `.env` (clave `TMDB_API_KEY`).
4. **Modelos**: puedes proporcionar rutas locales para Marian, M2M100 y AventIQ o repositorios remotos.  
   - Usa los botones **Descargar/seleccionar modelos** y **Actualizar estado modelos** para sincronizar.
5. **Opciones avanzadas**: `translator_batch_size`, `translator_device`, `cache_dir`, etc.
6. **Verificar traductores**: ejecuta pruebas básicas y actualiza los labels "Marian/M2M100/AventIQ".
7. **Probar traducción (A)**: envía un texto corto y muestra el backend utilizado junto con el tiempo de respuesta.

## 5. Flujo cotidiano

1. **Cargar biblioteca**: con rutas configuradas, la aplicación listará carpetas/animes disponibles.
2. **Traducciones**: cada llamada a `translator_translate` o `translator_translate_batch` selecciona automáticamente el backend activo.  
   - El mensaje "Traductor en uso: …" aparece en el panel Debug → Processes y en `debug.log`.
3. **Generar página**:
   - Usa las acciones de `page_builder.py` (por ejemplo `generar_en_hilo_con_tipo`).
   - El proceso reporta progreso mediante `ui_queue` (labels, barras, notificaciones).
4. **Panel Debug** (Ctrl+D o botón correspondiente):
   - **Errors**: logs `WARNING+`.
   - **Processes**: logs `INFO/DEBUG`, incluyendo los mensajes de traductor, construcción y otros eventos.
   - Permite exportar logs, limpiar buffers y buscar texto.
5. **Renombrados/metadata**: si `src/core/renombrar` está disponible, puedes ejecutar funciones de renombrado desde la UI.
6. **Finalización**: al terminar una generación, `process_ui_queue` emite la acción `"generation_finished"` para limpiar botones temporales.

### 5.1 Selección de rutas

1. Abre la ventana principal y pulsa **Configurar rutas**.
2. En el asistente, especifica:
   - `pages_output_dir`: carpeta donde se crearán los HTML finales.
   - `media_root_dir`: carpeta raíz con tus archivos de video/imágenes.
   - JSONs (`anime_json_path`, `movies_json_path`, etc.) y `template_path`.
3. Guarda los cambios; `save_config` escribe las rutas en `.vista/config.json` y, si corresponde, crea carpetas faltantes.
4. Al volver a la ventana principal, refresca la lista de títulos si agregaste nuevos directorios.

### 5.2 Generación de páginas paso a paso

1. Selecciona el título o carpeta desde la lista principal.
2. Configura las opciones deseadas (auto-traducción, metadatos, imagen).
3. Pulsa el botón **Generar página** (equivalente a `generar_en_hilo_con_tipo`).
4. Observa el panel de progreso: se actualiza mediante `ui_queue` con mensajes `progress`, `label_text` y `debug_process`.
5. Una vez finalizado, revisa `pages_output_dir` para confirmar que se generaron HTML/JSON y que los recursos están vinculados correctamente.

### 5.3 Herramienta de renombrado

1. Abre el módulo desde la UI (botón **Renombrar** si está habilitado) o ejecuta `src/core/renombrar.py`.
2. Selecciona la carpeta que contiene los episodios.
3. Define la plantilla de nombres (por ejemplo `Serie - Ep {numero}`).
4. Visualiza la previsualización antes de aplicar.
5. Confirma para que se apliquen los cambios. El módulo usa reglas definidas en `renombrar.py` y respeta los controles de pausa/detener.
6. Regresa a la vista principal y actualiza la lista para asegurarte de que los nuevos nombres se reflejan en los JSON generados.

### 5.4 Extractor de recursos (Carusel / previews)

1. Ubica el botón **Extractor** en la sección superior izquierda de la GUI.
2. Selecciona:
   - Carpeta donde están tus páginas HTML o los recursos a analizar.
   - Ruta del `Carusel.html` principal.
   - Archivo de metadatos (debe llamarse `anime1_info.json`).
3. Ejecuta el extractor; generará o actualizará `anime1_info.json` con los datos necesarios para las previsualizaciones.
4. Si Carusel no muestra previews tras regenerar recursos, vuelve a correr el extractor y verifica que el servidor sirva la carpeta correcta.

## 6. Resolución de problemas frecuentes

| Síntoma | Causa probable | Solución |
| --- | --- | --- |
| Ventana Debug vacía | `src/core/ui_logging.py` no pudo importarse (por rutas) | Confirmar rutas absolutas, reinstalar dependencias. |
| Traducción se queda en inglés | Backend sin modelo configurado o sin credencial | Revisar `config.json` y `.secrets.json`; ejecuta **Verificar traductores**. |
| UI congelada durante generación | Se ejecutó tarea pesada en hilo principal | Usa las funciones "*_en_hilo" de `page_builder` o aumenta `max_generation_threads`. |
| DeepL falla con 403 | API key inválida o excedió cuota | Cambia a otro backend o actualiza la clave. |
| No aparece `/media` en server.js | `media_root_dir` no existe o `server.js` borrado | Regenera `server.js` desde `setup.ps1` o restaura el archivo, asegúrate de que la ruta sea válida. |

## 7. Consejos prácticos

- Mantén `config.json` con rutas relativas cuando compartas el proyecto.
- Guarda un backup de `.vista/config.json` antes de experimentar con nuevos modelos.
- Si usas `setup.ps1`, aun así puedes abrir `src/main.py`; ambos flujos son compatibles.
- Para depurar, habilita `debug_level = "DEBUG"` en `config.json` y revisa `debug.log` junto con el panel.
- Al añadir un nuevo backend, reutiliza `_announce_translator_backend()` para mantener la trazabilidad.

## 8. Siguiente pasos

1. Genera tu página y verifica que `pages_output_dir` contenga HTML/JSON esperados.
2. Sirve el directorio con `node server.js` o cualquier servidor estático.
3. (Opcional) Automatiza rutinas usando scripts que invoquen funciones de `page_builder` directamente.

Con este manual deberías poder instalar, configurar y operar `main.py` de manera recurrente, entendiendo qué módulo interviene en cada etapa y cómo depurar cualquier incidencia.
