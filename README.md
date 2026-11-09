# AUDIOVISUALES — versión mínima con SQLite

Dos pipelines independientes y **tres archivos Python**. Requiere Python 3.10 o superior con `sqlite3`, incluido en las instalaciones habituales de Python. No hay paquetes adicionales que instalar.

| Archivo | Función |
|---|---|
| `cli.py` | Leer los argumentos y ejecutar el pipeline elegido. |
| `ingesta.py` | Consultar YouTube, validar y guardar datos crudos, comentarios y progreso en SQLite. |
| `clasificacion.py` | Leer un vídeo de SQLite, clasificar por lotes y guardar el dataset final en JSON. |
| `prompt.txt` | Reglas de clasificación V2 del notebook. |
| `README.md` | Instrucciones de uso. |

## 1. Claves

Desde PowerShell, en la carpeta del proyecto:

```powershell
$env:YOUTUBE_API_KEY = "tu_clave_de_youtube"
$env:OPENAI_API_KEY = "tu_clave_de_openai"
```

En bash se utiliza `export YOUTUBE_API_KEY="..."` y `export OPENAI_API_KEY="..."`. Las claves se leen del entorno; no se guardan en la base ni en los resultados. Esta versión no lee archivos `.env`.

## 2. Ingesta a SQLite

```powershell
python cli.py ingesta --video-id VIDEO_ID_11
```

Sustituye `VIDEO_ID_11` por el identificador real. Por defecto descarga todos los comentarios disponibles, incluyendo respuestas, y guarda en **`datos/audiovisuales.sqlite`**, relativo al directorio desde el que ejecutes el comando. La carpeta se crea automáticamente.

Puedes introducir varios vídeos en la misma base: cada uno tiene sus comentarios y su propio progreso, identificados por `video_id`.

Para elegir otra base, limitar la extracción o recoger sólo comentarios principales:

```powershell
python cli.py ingesta --video-id VIDEO_ID_11 --db datos/otra_captura.sqlite --limite 1000 --solo-principales
```

`--limite` es un máximo de comentarios únicos; encontrar menos no es un error. Se recorren primero los principales y después las respuestas. El límite puede alcanzarse antes de empezar las respuestas. La selección sigue el orden de la API; no es aleatoria.

### Qué se guarda

La base tiene tres tablas:

| Tabla | Contenido |
|---|---|
| `ingestas` | Configuración, cursor de paginación, fechas y estado de finalización de cada vídeo. |
| `paginas_crudas` | Bytes originales de cada respuesta válida de YouTube, parámetros de la petición sin la clave y fecha de recepción. |
| `comentarios` | Comentarios identificados por vídeo e ID, con su texto, fechas, contadores, relación padre/respuesta, orden y referencia a la página de origen. |

Se conserva exactamente la respuesta recibida de los campos solicitados a la API. El texto de los comentarios no se limpia ni se modifica. Los IDs repetidos conservan su primera observación en `comentarios`; las páginas crudas conservan lo que devolvió cada petición.

Si una página contiene más comentarios de los que quedan hasta `--limite`, se conserva la página cruda completa, pero sólo se incorporan a `comentarios` los necesarios para respetar ese máximo. El clasificador utiliza esta última tabla.

### Guardado y reanudación

Cada página se valida entera antes de guardarse. **La página cruda, los comentarios aceptados y el avance del cursor se confirman en una sola transacción SQLite.** Si la validación o la escritura falla, esa página no avanza el progreso ni deja comentarios parcialmente guardados. La petición a YouTube se hace fuera de la transacción.

Se utiliza SQLite con claves foráneas, restricciones de unicidad, WAL y `synchronous=FULL`. La ingesta consulta e inserta las filas necesarias; no carga ni reescribe todo el corpus en cada página.

Para continuar después de un error o interrupción, repite el mismo comando y sus parámetros añadiendo `--reanudar`:

```powershell
python cli.py ingesta --video-id VIDEO_ID_11 --reanudar
```

Si habías indicado `--db`, `--limite` o `--solo-principales`, conserva esos valores. Una ingesta ya completa se reconoce sin volver a consultar YouTube. Para obtener otra captura del mismo vídeo, usa otra base con `--db`; esta versión conserva una captura por vídeo en cada base.

No hay reintentos automáticos ni registros de errores adicionales. Si dos procesos intentan avanzar el mismo vídeo, el segundo comprueba que el progreso no haya cambiado antes de guardar y se detiene si encuentra un conflicto.

## 3. Clasificación desde SQLite

Cuando la ingesta del vídeo haya terminado:

```powershell
python cli.py clasificacion --video-id VIDEO_ID_11 --obra "Nombre de la película o serie" --estreno 2022-09-02
```

Lee por defecto `datos/audiovisuales.sqlite`. Para otra base, añade `--db datos/otra_captura.sqlite`. `--video-id` selecciona únicamente los comentarios de ese vídeo. La base se abre en modo de sólo lectura y se rechaza una ingesta incompleta.

La fecha de estreno es opcional. Por defecto se usa `gpt-6-luna`, lotes de 20, razonamiento `medium` y un máximo de 16.384 tokens de salida por petición. Modelo y tamaño de lote se pueden cambiar con `--modelo` y `--lote`; las otras dos constantes están al principio de `clasificacion.py`.

El resultado se guarda en **`datos/VIDEO_ID_11/clasificaciones.json`**. Con otra base, se crea la carpeta del vídeo junto a esa base; `--salida` permite elegir otro archivo. Cada elemento de `resultados` contiene el comentario y `analysis`, con `Tone`, `Intention`, `Metaconversation`, `Emotion` y `Confidence`.

La salida de clasificación conserva el mecanismo anterior: un JSON con resultados y progreso, sustituido de forma atómica después de cada lote válido. El cambio a SQLite se aplica al almacenamiento de datos crudos y a la entrada del clasificador.

Se comprueban los IDs exactos, las cinco dimensiones, tipos y categorías, listas sin duplicados, la exclusividad de `None`, la relación `Tone=null`/`Confidence=-1` y la incompatibilidad de las dos categorías de crítica. Una respuesta incompleta, un rechazo o un análisis inválido detiene el proceso sin guardar ese lote. Se admite un último lote más pequeño.

Para reanudar:

```powershell
python cli.py clasificacion --video-id VIDEO_ID_11 --obra "Nombre de la película o serie" --estreno 2022-09-02 --reanudar
```

La clasificación verifica que no hayan cambiado los comentarios del vídeo, la captura de origen, el prompt, el esquema o los parámetros. La huella `entrada_sha256` corresponde al contenido lógico del vídeo seleccionado; añadir otro vídeo a la base no invalida una clasificación iniciada. Los resultados confirmados no se vuelven a solicitar.

`prompt.txt` conserva V2 porque el notebook adjunto envía `instructions_2`, aunque su nombre de salida diga V3. Las respuestas se clasifican con su propio texto, sin añadir automáticamente el texto del padre.

## Límites de esta versión

- La API de YouTube puede cambiar mientras dura la extracción. Se conserva lo que devolvió cada petición; no se garantiza una instantánea de todos los comentarios en un mismo instante. Si un cursor deja de ser válido, se conservan los datos ya confirmados, pero puede ser necesario iniciar otra captura.
- Un corte después de que la API haya atendido una petición y antes de guardarla puede obligar a repetir esa petición. En clasificación, esto puede repetir su coste.
- La clasificación carga en memoria los comentarios del vídeo seleccionado y los resultados JSON. La ingesta trabaja página a página sobre SQLite.
- Usa la base en disco local. SQLite puede crear archivos auxiliares `-wal` y `-shm` mientras está abierta; no copies sólo el archivo principal durante una ejecución. Cierra los procesos antes de copiar o sincronizar la base.
- Esta versión crea su propio esquema SQLite. No convierte automáticamente JSON ni bases de otras versiones. `--salida` sigue siendo un alias de `--db` para ingesta, y `--entrada` lo es para clasificación.

## Comprobación

Las pruebas se realizan con respuestas simuladas, sin consumir las APIs: guardado y rollback de páginas completas, conservación del raw, reanudación, paginación de respuestas, deduplicación, separación por vídeo y lectura del clasificador. Las pruebas privadas no forman parte del proyecto entregado.

Referencias técnicas: [YouTube: hilos](https://developers.google.com/youtube/v3/docs/commentThreads/list), [YouTube: respuestas](https://developers.google.com/youtube/v3/docs/comments/list), [SQLite: configuración](https://www.sqlite.org/pragma.html).
