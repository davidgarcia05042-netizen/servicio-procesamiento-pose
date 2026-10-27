# Decisiones técnicas — Servicio de Procesamiento Asíncrono de Pose

Reúne el contexto del servicio, las decisiones de diseño con su
justificación, el contrato con la plataforma de telefisioterapia y las
convenciones de trabajo. La operación del despliegue (encender, suspender,
costos, variables) está en el `README.md`.

## Contexto

Trabajo de grado (Universidad Antonio Nariño, Ingeniería de Sistemas) de Juan
David García Rubio y Camila Sandoval Ruiz, dirigido por Ph.D. David Alberto
Herrera Álvarez, en el marco del grupo de investigación LACSER/Bioingeniería.

Este repositorio es el **servicio real** que se integra con la plataforma de
telefisioterapia: recibe un video, lo procesa con el modelo de pose elegido y
devuelve los ángulos articulares resultantes. Es la contraparte de producción
del entorno experimental: allí se decidió qué modelo usar y qué precisión
esperar; aquí se construye el servicio que lo sirve.

**Repositorios relacionados:**
- [`entorno-pruebas`](https://github.com/davidgarcia05042-netizen/entorno-pruebas):
  entorno donde se comparó MediaPipe Pose con YOLO-Pose contra el gold
  standard de laboratorio (mocap Maxtraq). Contiene los resultados de
  precisión (RMSE, error angular) que justifican la elección de modelo de este
  servicio y el código de comparación y evaluación, que **no se duplica ni se
  reimplementa aquí**.
- [`pose-benchmark`](https://github.com/davidgarcia05042-netizen/pose-benchmark):
  mediciones de tiempo, CPU y memoria por modelo.
- La plataforma que consume el servicio:
  [`telefisioterapia-backend`](https://github.com/davidgarcia05042-netizen/telefisioterapia-backend),
  [`telefisioterapia-panel`](https://github.com/davidgarcia05042-netizen/telefisioterapia-panel)
  y [`telefisioterapia-app`](https://github.com/davidgarcia05042-netizen/telefisioterapia-app).

**Este repositorio empezó limpio** (2026-09-14): no reutiliza el esbozo de API
que existía antes en el entorno experimental (un marcador de posición con
consulta periódica y `BackgroundTasks` en memoria, nunca conectado a un
procesamiento real). Ese esbozo se eliminó de allí porque no correspondía a
ese repositorio, y el servicio se diseñó desde cero con el contrato correcto
(webhook, ver abajo).

## Modelos a servir

Decisión tomada en el entorno experimental:

- **YOLO26-nano** (`yolo26n-pose.pt`)
- **MediaPipe Heavy** (`model_complexity=2`)
- **MediaPipe Lite** (`model_complexity=0`)

YOLOv8 y YOLO11 quedaron descartados: MediaPipe los superó consistentemente
en las pruebas contra gold standard. El detalle del barrido está en
`entorno-pruebas` (README, "Decisiones ya tomadas").

En producción corre **MediaPipe Heavy**, fijo por `POSE_MODEL`; YOLO26-nano no
está implementado en el servicio (ver "Bloqueadores resueltos", punto 4).

## Decisiones técnicas clave

### Esquema de keypoints hacia la plataforma

La plataforma de telefisioterapia usa los 33 índices de MediaPipe como
convención (`LEFT_SHOULDER`=11, etc., en mayúsculas). Toda salida de este
servicio hacia la plataforma está en esos términos, **no en COCO-17**. COCO-17
es el vocabulario interno de `entorno-pruebas`, necesario allí para comparar
MediaPipe y YOLO entre sí; aquí esa unificación no hace falta mientras YOLO26
no se use en producción.

### Umbral de error angular

≤5° aceptable, 5-10° moderado, >10° no aceptable. Fundamentado en Gajdosik &
Bohannon (1987, *Physical Therapy*). La métrica que se reporta como protocolo
principal es RMSE (decisión del 2026-09-10 en el entorno experimental), no el
error absoluto medio.

### Contrato con la plataforma: webhook

Es un patrón de **webhook**, no de consulta periódica. La plataforma hace
`POST /video/` (multipart: video + points + exercise + `resultsEndpoint`),
espera un 200 inmediato, y este servicio llama de vuelta a `resultsEndpoint`
cuando termina, con `{error, results: {points}, max_angle, min_angle}`.
Repositorio de referencia del contrato original:
`santivarelaagent-cmd/telerehabilitacion_be` (documentado vía DeepWiki).

### Contrato v2

Desde el 2026-10-16 (`docs/DECISIONES_TECNICAS.md` de la plataforma), `points`
acepta también una **lista de 1 a 12 ternas** (`[[23, 25, 27], [24, 26, 28]]`).
Esa solicitud es **un solo job y una sola pasada de MediaPipe** para todas las
ternas (`analyze_video_v2`), y recibe el callback v2:
- `contract_version: 2`;
- `max_angle`/`min_angle` de primer nivel en `null` y `results.points` vacío;
- `results.joints`: por terna, `points`, `vertex`, `max_angle`, `min_angle`,
  `valid_frames`;
- `results.frames`: por frame, `t`, los 33 `landmarks` `[x, y, z, visibilidad]`
  normalizados, los 33 `world_landmarks` `[x, y, z]` en metros, y
  `angles_2d`/`angles_3d` en el orden de `joints`.

**El contrato v1 sigue vigente**: una sola terna recibe exactamente el
callback de siempre. La marca `contract_version` en el JSONB `results` solo la
pone la recepción para v2, y su ausencia significa v1.

### El ángulo 2D validado no se toca

`_angle_deg` (en píxeles) es la medición clínica contrastada contra gold
standard. La v2 la reutiliza tal cual, así que una terna da los mismos valores
en v1 y en v2. El ángulo 3D (`_angle_deg_3d`, sobre `pose_world_landmarks`) es
una función aparte y se entrega como **visualización, no como medición
validada**.

### Destino de despliegue: Render

Decidido el 2026-10-08 por el grupo de investigación: los tres procesos, la
cola y la base (Render Postgres, `pose-db` en `deploy/render/render.yaml`).
Los videos esperan en un bucket privado de **Cloudflare R2** desde el
2026-10-22 (antes, Supabase Storage; ver "Almacenamiento de video").

Antes (2026-10-07) se había empezado a llevar a **Google Cloud** por etapas
(Cloud Storage, Cloud SQL, Cloud Tasks + Cloud Run, Cloud Scheduler). Ese
código sigue en el repositorio y se prueba, pero no se despliega. La etiqueta
`render-supabase-v1` de los repositorios de trabajo marca el estado anterior,
con base y videos en Supabase.

**Las estructuras de cada plataforma no se cruzan:**
- lo común vive en `app/core`, `app/api` y `app/utils`; lo de cada plataforma,
  en `app/platforms/<plataforma>/`, con sus propias dependencias
  (`requirements/<plataforma>.txt`) y su etapa del Dockerfile;
- en Google, la cola es Cloud Tasks, que entrega cada tarea por HTTP a un
  worker privado en Cloud Run (`app/platforms/gcp/worker.py`) con un token
  OIDC que el worker verifica;
- lo común nunca importa Celery, Redis ni librerías de un proveedor, y le habla
  a la plataforma por dos puertos: `app/core/task_queue.py` (la cola) y
  `app/utils/video_storage.py` (el almacenamiento, que decide la ruta de cada
  video);
- las tareas periódicas son las mismas en las dos plataformas (Beat en Render,
  Cloud Scheduler en Google; las pruebas lo comparan). La excepción: Cloud
  Scheduler no baja de un minuto, así que allí el despachador de reintentos de
  avisos corre cada minuto y no cada 10 s. Se aceptó, porque lo contrario
  exige pagar una instancia siempre encendida;
- una plataforma en construcción se acepta en `PLATFORM` solo en desarrollo
  (`PLATFORMS_UNDER_CONSTRUCTION`), y `docker-compose.gcp.yml` corre el camino
  de Google en local con emuladores.

`tests/test_platform_boundaries.py` hace cumplir esta separación. Ver README,
"Plataformas de despliegue".

### Hardware de despliegue

Render, con el worker en 4 CPU y 8 GB (`4c-8g`, `--concurrency=4`, aprobado el
2026-10-09; antes `2c-4g`), **sin GPU**. Se suspende entre pruebas para no
pagarlo (ver README, "Costos"). La API va en `1c-2g` (2026-10-09) por memoria:
dos videos de 47 MB a la vez llegan a 662 MiB.

Por eso los modelos "nano"/"lite" eran los candidatos principales para
producción, y no las variantes más pesadas, sujeto a las mediciones de
`pose-benchmark` (tiempo, CPU y memoria por variante) antes de fijar el modelo
por defecto.

### Procesamiento asíncrono: FastAPI + Celery + Redis

Decidido el 2026-09-14. Arquitectura adaptada de `hemantgarg26/VideoProcessing`:
estructura por dominio (`api/`, `core/`, `dtos/`, `utils/`, `constants/`),
validación del video en memoria antes de subirlo al almacenamiento externo, y
Flower para monitorear la cola. Se descartaron de la referencia las miniaturas
de video, el manejo de usuarios y contraseñas (aquí la autenticación es firma
HMAC con la plataforma cliente) y las consultas "por usuario" (no hay usuarios
finales).

### Event loop persistente en el worker

Celery no soporta async/await de forma nativa (plataforma Render), y la tarea
necesita código asíncrono (SQLAlchemy con asyncpg, httpx para el webhook). La
solución es un **event loop persistente por proceso worker**, creado en la
señal `worker_process_init` y reutilizado por todas las tareas
(`app/platforms/render/worker.py::run_async`).

No se usa `asyncio.run()` por tarea: crea y cierra un loop nuevo cada vez, lo
que invalida las conexiones de asyncpg y, bajo concurrencia, produce
`RuntimeError: event loop is already running`. Una prueba protege esta
invariante (`test_worker_reuses_the_same_event_loop_across_tasks`).

### Persistencia

PostgreSQL. La tabla `jobs` guarda el resultado en una columna **JSONB**,
porque su forma depende del ejercicio y no se quiere una migración por cada
variante. En Render, Render Postgres por la red privada (antes, Supabase); en
Google, Cloud SQL por el socket Unix que monta Cloud Run
(`/cloudsql/<proyecto>:<región>:<instancia>`).

El worker toma y cierra cada job solo si sigue abierto (`update_open_job`):
así, un worker que termina tarde no pisa el cierre del barrido de jobs
colgados ni manda un segundo aviso. En producción se exige TLS hacia la base,
salvo por la red privada de la plataforma o por un socket Unix, que no sale de
la máquina (regla común en `app/core/config.py`).

### Almacenamiento de video

Lo pone cada plataforma (`video_storage()` en `app/platforms/<plataforma>/`).
En Render se elige con `STORAGE_BACKEND`; desde el 2026-10-22 es `r2`: el
video espera en el bucket privado `pose-videos` de Cloudflare R2
(`r2_storage.py`, boto3 por la API S3, vía `asyncio.to_thread`) y el worker lo
borra al cerrar el job. El token de R2 (`R2_ACCESS_KEY_ID` y
`R2_SECRET_ACCESS_KEY`) son los únicos secretos que se cargan a mano, en
pose-api y pose-worker (`sync: false` por servicio: Render lo ignora en un
grupo y, en un blueprint ya creado, no lo pide; una prueba lo fija).

- **Por qué R2 y no `supabase`** (2026-10-16 a 2026-10-22): el plan gratuito de
  Supabase pausa el proyecto tras una semana sin actividad y, pausado, no se
  procesa ningún video nuevo. El código de Supabase sigue disponible.
- **Cómo se configuró R2** (detalle en el README, "Los videos en Cloudflare R2"):
  - bucket `pose-videos` **privado**: sin URL `r2.dev`, sin dominio propio y sin
    CORS; solo le hablan la API y el worker, desde el servidor;
  - **token propio** del servicio, con permiso de lectura y escritura solo sobre
    `pose-videos`; no se reutiliza el de la plataforma;
  - **regla de ciclo de vida de 7 días**: borra los objetos con más de una
    semana. Es la red de seguridad de la Ley 1581 si un video quedara
    huérfano; el borrado normal lo hace el worker.

- **Por qué se dejó `postgres`** (2026-10-08 a 2026-10-16, el video como bytea
  en la tabla `videos` de pose-db): en la prueba de punta a punta con la
  plataforma, un video de celular agotó la memoria de pose-db (basic-256mb).
  La base se reiniciaba en cada subida y, sin base, la API daba 503 y Render la
  reiniciaba. El código sigue disponible.
- **Ley 1581:** con `postgres`, lo borrado seguía de 3 a 7 días en los
  respaldos de Render (WAL). Con Supabase Storage los videos ya no pasan por
  pose-db ni por su WAL. Los respaldos de Supabase no incluyen los objetos de
  Storage, solo sus metadatos (ruta, tamaño, fechas), y Supabase no documenta
  que conserve un objeto borrado. A cambio, el video viaja por internet
  (cifrado) a un proveedor más; está explicado en el README.
- **Otras opciones:** Firebase sigue disponible. En Google Cloud se usa Cloud
  Storage con la cuenta de servicio de Cloud Run (sin claves), sin borrado
  lógico y con una regla de ciclo de vida como red de seguridad (Ley 1581).
  Los SDK de `firebase-admin` y `google-cloud-storage` son síncronos y
  bloqueantes, así que se llaman vía `asyncio.to_thread` para no bloquear el
  event loop (ni el de uvicorn ni el del worker).
- **Nombre del archivo:** la ruta de cada video es `videos/<uuid><extensión>`.
  Del nombre que manda el cliente solo queda la extensión, porque puede
  identificar al paciente.

## Bloqueadores resueltos

Resueltos el 2026-10-15, al integrar la copia propia de la plataforma de
telefisioterapia (`telefisioterapia-backend` y `telefisioterapia-panel`; ver
su `docs/DECISIONES_TECNICAS.md`). Ninguno exigió
cambiar el código de este servicio.

1. **Disparo de los flujos:** el backend de la plataforma dispara ambos
   (referencia `Exercise` y paciente `ExerciseResult`). El flujo de referencia
   funcionaba en el navegador; el del paciente nunca había existido.
   - **Desde la v2 (2026-10-16): un solo job por video**, con todas las ternas
     de las articulaciones rastreadas en `points` (contrato v2, una sola
     pasada de MediaPipe).
   - Los callbacks llegan **sin `point_tracked`**: el backend asocia cada
     entrada de `results.joints` a su articulación por la terna completa.
   - El `resultsEndpoint` lleva la huella del video (`?video=<sha256[:16]>`),
     para que la plataforma rechace con 404 el aviso de un video de referencia
     ya reemplazado: `/exercises/{id}/video_results/?video=...` (con barra
     final) y `/exercise_results/{id}/results?video=...` (sin barra final).
   - En la v1 (2026-10-15) era un job por articulación, con
     `?point_tracked={pt_id}` en el endpoint. La plataforma ya no envía jobs
     v1: solo acepta avisos v1 del paciente durante la transición (los de
     referencia los rechaza).
   - El contrato v1 sigue vigente en este servicio (una terna sola) y no
     cambió.
2. **Esquema HMAC:** acordado; es el de `app/utils/hmac_auth.py`, sin cambios.
   La plataforma firma lo que envía y verifica los callbacks con el mismo
   algoritmo (comprobado con un vector de prueba común).
3. **Semántica de `points`:** confirmada con el código del panel. Cada terna
   son 3 índices de MediaPipe `[vecino, vértice, vecino]` y el ángulo se mide
   en el vértice, en 2D. En la v2, `points` es una lista de 1 a 12 ternas.
4. **Modelo en producción:** MediaPipe Heavy, fijo por `POSE_MODEL`.
   YOLO26-nano no está implementado en el servicio. Si cambia `POSE_MODEL`, la
   plataforma debe reprocesar los videos de referencia: de lo contrario,
   referencia y paciente quedan medidos con modelos distintos.

`CALLBACK_ALLOWED_HOSTS` apunta solo al backend de la plataforma
(`deploy/render/render.yaml`) y `HMAC_ENFORCE` sigue en `true`.

## Convenciones del código

Heredadas del entorno experimental, para mantener la consistencia:

- Identificadores en inglés; docstrings y comentarios largos en español (el
  equipo sustenta en español).
- Entorno de desarrollo: Windows, PowerShell, VS Code, Python 3.12, venv por
  proyecto (`venv\Scripts\Activate.ps1`).
- Los scripts se corren como módulo (`python -m ...`), no como archivo suelto,
  por la resolución de paquetes.
- Todo cambio de código se verifica con la suite de pruebas (`pytest`) antes
  de entregarse; el CI la corre en cada PR y en cada push a `main`.
