# Manual de usuario – Multimedia Server Toolkit

Guía práctica para comenzar desde cero, configurar tus rutas, elegir proveedores y aprovechar todas las herramientas incluidas en `main.py`.

---

## 1. Inicio rápido

1. **Prepara el entorno**
   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
2. **Arranca la aplicación**
   ```powershell
   python -m src.main
   ```
3. En el primer arranque se abrirá el asistente de configuración.

> **Sugerencia:** si prefieres una instalación guiada, ejecuta `./setup.ps1` como administrador y sigue el asistente WPF.

---

## 2. Selección de rutas

1. Desde la ventana principal, pulsa **Configurar rutas** (abre `ensure_config_via_gui`).
2. Completa al menos:
   - `pages_output_dir`: carpeta donde se generarán las páginas HTML.
   - `media_root_dir`: carpeta con tus animes/películas.
   - `anime_json_path` / `movies_json_path`: archivos donde se guardará el catálogo.
   - `template_path`: tu `template.html` base.
3. Guarda los cambios. El sistema escribe la configuración en `.vista/config.json` y crea las carpetas faltantes si es necesario.
4. Regresa a la pantalla principal y pulsa **Refrescar** (si está disponible) para cargar los títulos encontrados.

**Ejemplo:**
- Páginas: `C:\Users\me\multimediaserver\pages`
- Media: `D:\Anime` (contiene carpetas por serie)
- Plantilla: `C:\Users\me\multimediaserver\template.html`

---

## 3. Elección de proveedor y API

En la misma ventana de configuración:

- **Proveedor de metadata** (`metadata_provider`):
  - `jikan` para usar la API pública de MyAnimeList.
  - `tmdb` si prefieres datos de The Movie Database.
- **Claves**:
  - TMDB: ingrésala y se guardará en `.env` (clave `TMDB_API_KEY`).
  - DeepL: se guarda en `.vista/.secrets.json`.
- En la UI principal también verás etiquetas "Metadata: jikan/tmdb" y puedes alternar según tus necesidades.

**Ejemplo de cambio rápido:**
1. Pulsa **Configurar rutas**.
2. Cambia `metadata_provider` a `tmdb`.
3. Guarda y regresa: la etiqueta en la pantalla principal mostrará "Metadata: tmdb".

---

## 4. Tipos de generación

### 4.1 Generación manual (serie por serie)
1. Selecciona una carpeta/serie en la lista.
2. Revisa la sinopsis, imagen y datos mostrados.
3. Pulsa **Generar página** (o botón equivalente). Internamente se llama a `page_builder.generar_en_hilo_con_tipo`.
4. Observa la barra de progreso y los mensajes del panel Debug.
5. Resultado: HTML/JSON nuevos en `pages_output_dir` y recursos asociados en `media_root_dir`.

### 4.2 Generación automática
1. Define filtros o activa el modo automático (botón **Generación automática**, depende de tu configuración).
2. El sistema recorre todas las carpetas dentro de `media_root_dir`, traduce sinopsis y genera páginas en lote (`generar_automatico_en_hilo`).
3. Puedes pausar/detener usando los controles expuestos en la UI (internamente usan `gen_control`).
4. Los eventos se registran en el panel Debug y en `debug.log`.

**Tip:** Activa `config['defer_json_write'] = True` para que los JSON se escriban al final de la generación automática.

---

## 5. Uso de las herramientas adicionales

### 5.1 Renombrar episodios
1. Abre el módulo **Renombrar** desde la ventana principal.
2. Selecciona la carpeta con tus episodios.
3. Define el formato (ej. `Serie_S01E{numero}`).
4. Previsualiza el resultado; si es correcto, confirma.
5. El módulo `src/core/renombrar.py` aplicará los cambios y actualizará los nombres en disco.

**Ejemplo:**
- Carpeta: `D:\Anime\My Series`
- Formato: `My Series - Episodio {numero}`
- Resultado: `My Series - Episodio 01.mp4`, `My Series - Episodio 02.mp4`, etc.

### 5.2 Extractor (Carusel / previews)
1. En la esquina superior izquierda de la GUI pulsa **Extractor**.
2. Configura:
   - Carpeta de páginas: `pages_output_dir` o la carpeta con tus HTML.
   - Ruta a `Carusel.html` principal.
   - Nombre del archivo de metadatos (por defecto `anime1_info.json`).
3. Ejecuta el extractor para regenerar previews y metadatos.
4. Si al abrir el servidor los previews no aparecen, vuelve a correr el extractor y asegúrate de que `server.js` sirva la carpeta correcta.

**Ejemplo:**
- Páginas: `C:\Users\me\multimediaserver\pages`
- Carusel: `C:\Users\me\multimediaserver\Carusel.html`
- Resultado: se actualiza `anime1_info.json` con la info necesaria para los carruseles.

---

## 6. Selección del backend de traducción

1. En **Configurar rutas** elige `translator_backend`:
   - `local` (Marian), `m2m100`, `aventiq`, `deepl` o `auto`.
2. Presiona **Verificar traductores** para revisar disponibilidad.
3. Usa **Probar traducción (A)** para un test corto. El popup muestra el backend y el tiempo empleado.
4. Cada vez que cambie el backend, verás "Traductor en uso: …" en el panel Debug.

**Ejemplo:**
- Seleccionas `auto` y guardas.
- Si tienes clave DeepL, el sistema lo usará primero. Si falla, pasará a M2M100/AventIQ/Marian en ese orden.

---

## 7. Menú principal y opciones rápidas

- **Selección de provider**: junto al nombre del traductor verás "Metadata: jikan/tmdb"; cambia la preferencia en la configuración para alternar.
- **Botones de control**: pausar/reanudar/detener generación automática (usa `gen_control`).
- **Panel Debug (Ctrl + D)**: abre una ventana con dos pestañas; ideal para revisar errores o exportar logs.
- **Extracciones TMDB**: si una serie necesita forzarse como anime/serie, crea `tmdb_overrides.json` y agrega la entrada.

---

## 8. Ejemplo completo

1. Configura rutas (`pages_output_dir`, `media_root_dir`, plantilla, JSONs).
2. Selecciona `metadata_provider = "jikan"` y `translator_backend = "auto"`.
3. Crea tu estructura de carpetas en `media_root_dir` (una carpeta por serie).
4. Pulsa **Generar página** para una serie de prueba.
5. Abre el **Extractor** para garantizar que `Carusel.html` tenga previews.
6. Lanza `node server.js` para revisar el resultado final.

Resultado: tendrás páginas generadas en `pages_output_dir`, recursos listos en `media_root_dir`, y podrás navegar desde `http://localhost:3000`.

---

Con este manual puedes recorrer todas las funciones principales sin necesidad de revisar el código fuente. Si necesitas más detalles internos, revisa `MANUAL_MAIN.md` o entra a los módulos mencionados.
