# Servicio de Procesamiento Asíncrono de Pose

Servicio que recibe video de la plataforma de telefisioterapia, calcula
ángulos articulares con MediaPipe, y devuelve los resultados por webhook.

Parte del trabajo de grado de telefisioterapia (Universidad Antonio Nariño,
grupo LACSER/Bioingeniería). Las decisiones de diseño y su justificación están
en [`docs/DECISIONES_TECNICAS.md`](docs/DECISIONES_TECNICAS.md).

**Repositorios relacionados:**

| Repositorio | Qué es |
|---|---|
| [`telefisioterapia-backend`](https://github.com/davidgarcia05042-netizen/telefisioterapia-backend) | Backend de la plataforma: envía los videos a este servicio y recibe los callbacks |
| [`telefisioterapia-panel`](https://github.com/davidgarcia05042-netizen/telefisioterapia-panel) | Panel web del terapeuta |
| [`telefisioterapia-app`](https://github.com/davidgarcia05042-netizen/telefisioterapia-app) | App Android del paciente |
| [`entorno-pruebas`](https://github.com/davidgarcia05042-netizen/entorno-pruebas) | Comparación de modelos de pose contra gold standard de laboratorio |
| [`pose-benchmark`](https://github.com/davidgarcia05042-netizen/pose-benchmark) | Tiempo, CPU y memoria de cada modelo |

Arquitectura adaptada de [hemantgarg26/VideoProcessing](https://github.com/hemantgarg26/VideoProcessing)
(FastAPI + Celery + Redis), con estos cambios:

| Referencia | Acá | Por qué |
|---|---|---|
| MongoDB | PostgreSQL (columna JSONB) | El payload de resultados es variable según el ejercicio; JSONB evita una migración por variante |
| Amazon S3 | Supabase Storage (bucket privado) | El video espera ahí y se borra al procesarlo. Render no tiene un almacenamiento de objetos estable, y guardarlo en la base la dejaba sin memoria (ver "Los videos en Supabase Storage") |
| Login / usuarios / rate limit por usuario | Firma HMAC | No hay usuarios finales, solo una plataforma cliente |
| Miniaturas de video | (eliminado) | No aplica a análisis de pose |

## Flujo

```
Plataforma --POST /video/--> API  ──valida en memoria──> PostgreSQL (tabla videos)
                              │                              │
                              ├── registro en PostgreSQL     │
                              └── encola en Redis ──> Celery worker
                                                          │
                                        descarga video ────┘
                                        corre MediaPipe
                                        guarda resultados (JSONB)
                                        └──webhook──> resultsEndpoint
```

Estados del job (`app/constants/job_constants.py`): `queued` → `processing` →
`done` / `failed`.

## Estructura

```
app/
├── api/          endpoints (health, video_processing)
├── constants/    JobStatus
├── core/         config, video_job (procesar un video), task_queue (puerto de
│                 la cola), video_processing_service (orquestación),
│                 pose_analysis (MediaPipe), callbacks, retención, barridos
├── dtos/         esquemas de request/response y códigos de error
├── platforms/    lo que depende de dónde se despliega (ver abajo)
│   ├── render/   Celery + Redis + Beat (worker, config, sondeo de /health) y
│   │             el video en la propia base (postgres_storage), en Supabase
│   │             Storage o en Firebase
│   └── gcp/      Google Cloud: Cloud Storage, Cloud SQL, Cloud Tasks hacia el
│                 worker en Cloud Run (worker.py) y Cloud Scheduler (schedule.py)
├── tools/        herramientas de línea de comandos (medición de rendimiento, demo)
└── utils/        db_connect (PostgreSQL), video_storage (puerto del
                  almacenamiento), hmac_auth, request_validations (validación
                  en memoria), logger
deploy/
├── render/       render.yaml (blueprint) y el esquema oficial para validarlo
└── gcp/          regla de ciclo de vida del bucket (se completa por etapas)
requirements/     base.txt (común) y uno por plataforma (render.txt, gcp.txt)
```

### Plataformas de despliegue

El servicio tiene una parte **común** y una parte que depende de **dónde se
despliega**, y las dos no se cruzan:

- **Lo común** (`app/core`, `app/api`, `app/utils`…) es todo lo que hace el
  servicio: validar, analizar el video, calcular ángulos, avisar a la
  plataforma, retener y borrar datos. No importa nada de Celery, de Redis ni
  de ningún proveedor (ni Firebase ni las librerías de Google Cloud).
- **Cada plataforma** (`app/platforms/<nombre>/`) decide dónde espera el
  video, cómo viaja el trabajo de la API al worker, quién dispara las tareas
  periódicas y qué se sondea en `/health`. Se elige con `PLATFORM`: `render`,
  o `gcp`. Una plataforma que todavía está en construcción se acepta solo en
  desarrollo (`PLATFORMS_UNDER_CONSTRUCTION` en `app/core/config.py`).

Lo común le habla a la plataforma por dos puertos:

- `app/core/task_queue.py`: encolar el procesamiento de un video y encolar un
  intento de aviso.
- `app/utils/video_storage.py`: subir, bajar y borrar el video. El puerto
  decide la ruta de cada video (`videos/<uuid><extensión>`); el adaptador de
  la plataforma solo guarda, baja y borra bytes en esa ruta. Así dos
  plataformas no pueden terminar nombrando distinto el mismo video. Del nombre
  que manda el cliente solo queda la extensión: el nombre puede identificar al
  paciente, y por la ruta llegaba a los logs y a la base.

La interfaz completa que implementa cada plataforma está en
`app/platforms/__init__.py`.

La separación no depende de que alguien la recuerde:
`tests/test_platform_boundaries.py` falla si lo común importa Celery, Redis,
Firebase o una librería de Google, si importa una plataforma por su nombre en
vez de pasar por el selector, si una plataforma importa a otra, si le falta
alguna función de la interfaz o si una plataforma en construcción se puede
elegir en producción. También comprueba que la API arranca sin cargar Celery
ni las librerías de almacenamiento. Y `tests/test_video_storage.py` comprueba
que todos los adaptadores de almacenamiento cumplan el mismo contrato.

Cada plataforma tiene además sus propias dependencias
(`requirements/<plataforma>.txt`, que incluye a `base.txt`) y su propia etapa
en el Dockerfile (`docker build --target <plataforma>`), así que la imagen de
una plataforma no instala las librerías de otra.

## Correr esto

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt   # trae también las de producción
copy .env.example .env   # y completar credenciales
```

> **Dependencias separadas, a propósito.** `requirements/` es *exactamente* lo
> que se instala en las imágenes de producción: `base.txt` lo común y un
> archivo por plataforma. El Dockerfile no instala nada más. Todo lo que solo
> sirve para desarrollar —la suite de tests, la herramienta de medición, la
> validación del blueprint— vive en `requirements-dev.txt`, que incluye las de
> todas las plataformas con `-r`. Para desarrollar se instala el de desarrollo
> y ya.

Levantar Redis, PostgreSQL y Flower:

```powershell
docker compose up -d
```

> **PostgreSQL se publica en el puerto 5433, no en el 5432.** Es a propósito:
> el 5432 suele estar tomado por una instalación nativa de PostgreSQL, y el
> contenedor no podría arrancar. Con 5433 conviven sin pisarse, y por eso
> `DATABASE_URL` en `.env.example` apunta a `localhost:5433`. Si prefieres el
> 5432, cambia ambos a la vez.
>
> Redis exige contraseña (`--requirepass`), así que `REDIS_PASSWORD` tiene que
> estar en el `.env` o el compose no levanta.
>
> **Las variables de la cola se llaman `REDIS_BROKER_URL` y `REDIS_BACKEND_URL`,
> no `BROKER_URL`/`BACKEND_URL`.** Esos dos nombres son configuración heredada
> del propio Celery: si aparecen en el entorno, Celery los usa tal cual y
> descarta lo que le pasa el servicio, con lo que el worker pierde la
> contraseña inyectada y el `ssl_cert_reqs` de TLS — sin un solo error visible.
> Hay un test que impide volver a esos nombres.

Crear el esquema de la base (ver *Migraciones* más abajo):

```powershell
alembic upgrade head
```

En terminales distintas:

```powershell
# API
uvicorn app.main:app --reload

# Worker de Celery
celery -A app.platforms.render.worker.celery worker --pool=solo

# Celery Beat: dispara el barrido de jobs atascados (ver más abajo)
celery -A app.platforms.render.worker.celery beat

# Flower (si no se usa el de docker compose; viene en requirements-dev.txt): http://localhost:5555
celery -A app.platforms.render.worker.celery flower --port=5555
```

> En Windows el pool `prefork` de Celery no funciona; usar `--pool=solo` para
> desarrollo local. En el despliegue real (Render, Linux) se usa prefork con
> `--concurrency N`, que es lo que da paralelismo real: cada proceso tiene su
> propio event loop persistente.

Tests:

```powershell
pytest -v
```

Casi ningún test necesita PostgreSQL, Redis ni un worker corriendo: las
piezas externas van mockeadas y la app se ejercita en proceso con
`ASGITransport` de httpx, sin abrir sockets. Las excepciones se saltan solas
si falta lo que necesitan: `test_render_postgres_storage.py` usa un
PostgreSQL real (`POSTGRES_URL_FOR_TESTS`, ver su docstring), y las de Google
Cloud, los emuladores. En la CI corren todas.

- `test_core.py` — piezas puras: validaciones, HMAC, ángulos, invariante del
  event loop del worker.
- `test_video_processing_service.py` — orquestación del `POST /video/`.
- `test_rate_limit.py` — límite de tasa del endpoint.
- `test_security.py` — orden de middlewares, modos de `HMAC_ENFORCE`, firma
  del callback saliente.
- `test_job_logging.py` — registro estructurado y tiempo de procesamiento.
- `test_migrations.py` — las migraciones de Alembic coinciden con los modelos.
- `test_redis_config.py` — contraseña de Redis y TLS obligatorio en producción.
- `test_database_config.py` — driver de la URL de PostgreSQL.
- `test_stuck_jobs.py` — barrido de los jobs colgados, en `processing` y en
  `queued`, y el aviso a la plataforma.
- `test_callback_delivery.py` — política de reintentos del webhook de salida.
- `test_callback_retry.py` — reintentos agendados en la base: el intento, el
  despachador, el rescate de avisos sin tarea y el ciclo completo.
- `test_task_redelivery.py` — el worker descarta las tareas re-entregadas de
  jobs ya cerrados.
- `test_service_status.py` — resumen periódico del estado del servicio.
- `test_data_retention.py` — borrado del video y minimización de los datos
  personales vencidos.
- `test_callback_endpoint.py` — a qué direcciones se acepta enviar resultados.
- `test_benchmark_pipeline.py` — herramienta de medición de rendimiento.
- `test_demo.py` — compose locales, receptor y envío firmado.
- `test_render_postgres_storage.py` — los videos en la base, contra
  PostgreSQL real, y el ciclo del job alrededor: que un worker que termina
  después del barrido no pise su cierre.
- `test_video_io.py` — los videos de celular se procesan en vertical (rotación
  de los metadatos).
- `fakes.py` — dobles compartidos: la sesión de base de datos que captura el SQL.

### En CI

`.github/workflows/tests.yml` corre la suite completa en cada pull request y en
cada push a `main`. Antes no existía: el repositorio solo tenía escaneos de
seguridad, así que un PR con tests rotos se veía en verde y la convención de
`docs/DECISIONES_TECNICAS.md` —*todo cambio de código se verifica con la suite antes de
entregarse*— dependía de que alguien se acordara de correrla.

El workflow instala `requirements-dev.txt` y, antes, `libgl1` y
`libglib2.0-0t64` por apt: `tests/conftest.py` importa `cv2` a nivel de módulo
y los runners de GitHub no traen esas librerías, así que sin ellas falla la
recolección entera y no un test suelto. Es la misma razón que está explicada en
el `Dockerfile`.

> **Lo que CI no cubre.** La suite sustituye todo lo externo por dobles, que es
> lo que la hace rápida y reproducible, pero también significa que los fallos
> que solo aparecen contra servicios reales no se ven acá: la autenticación de
> Redis, el driver de la URL de PostgreSQL, el barrido disparado por Celery
> Beat. Esos se verifican a mano y cada uno queda documentado en su sección.

## Demo local (sin Render)

Para mostrarle el servicio funcionando al equipo sin desplegar nada.
`docker-compose.demo.yml` levanta **la misma imagen que se desplegaría**, con
la API, el worker y Beat como procesos separados, igual que en `deploy/render/render.yaml`.
El video espera en el PostgreSQL de la demo, igual que en Render
(`STORAGE_BACKEND=postgres`), así que nunca sale de la máquina. Lo único
simulado es el extremo que no es nuestro:

- **La plataforma de telefisioterapia**: `app/tools/demo_receiver.py` recibe
  el callback, verifica la firma HMAC y dibuja la curva del ángulo.

No lee el `.env`: cualquiera lo levanta sin configurar nada. Solo publica
puertos en `127.0.0.1`, y `down` borra todo, videos y resultados incluidos.

### Antes de la reunión

La primera construcción de la imagen tarda varios minutos (MediaPipe, OpenCV).
Conviene hacerla antes:

```powershell
docker compose -f docker-compose.demo.yml up --build -d
```

Y dejar abiertas tres pestañas:

| URL | Qué es |
|---|---|
| http://localhost:9099 | La plataforma simulada: acá aparece el resultado |
| http://localhost:5555 | Flower: la cola de tareas en vivo |
| http://localhost:8000/docs | El contrato de la API |

### Guion

**1. Un video real, de punta a punta.** Con el venv activo:

```powershell
python -m app.tools.demo_send ruta\al\video.mp4
```

Por defecto mide el ángulo de la rodilla derecha (`--points 24,26,28`: cadera,
rodilla, tobillo). Hay que señalar tres cosas:

- La respuesta llega en unos 3 s con un `job_id`, aunque el video tarde más de
  medio minuto en procesarse. Eso es lo asíncrono.
- El job pasa por `queued` → `processing` → `done`, y Flower muestra la tarea.
- El resultado aparece solo en http://localhost:9099, **sin que nadie lo
  pida**, con la firma verificada y la curva del ángulo por frame. Así funciona
  el webhook.

**2. Lo que el servicio rechaza.**

```powershell
# Sin firma HMAC: 401
python -m app.tools.demo_send ruta\al\video.mp4 --sin-firma

# Un resultsEndpoint hacia el servicio de metadatos de la nube: rechazado
python -m app.tools.demo_send ruta\al\video.mp4 --endpoint http://169.254.169.254/latest/meta-data/
```

**3. La plataforma se cae y el resultado igual llega.**

```powershell
# Terminal 1
Invoke-RestMethod -Method Post "http://localhost:9099/control/caida?activa=true"
python -m app.tools.demo_send ruta\al\video.mp4

# Terminal 2, en cuanto la página muestre uno o dos 503
Invoke-RestMethod -Method Post "http://localhost:9099/control/caida?activa=false"
```

Dos cosas para que el caso se vea bien en vivo:

- **Hay que devolver la plataforma rápido.** Las esperas entre intentos crecen
  (5 s, 30 s, 2 min, 8 min, 30 min…). Con la caída apagada tras el segundo 503,
  la entrega llega unos 30 s después. Si se deja caída más tiempo, el siguiente
  intento puede estar a varios minutos.
- **Reiniciar el worker durante el caso no afecta.** La hora del próximo
  intento está en PostgreSQL, no en la cola, así que un worker recreado a mitad
  de los reintentos entrega a la hora prevista. Es una buena prueba para
  mostrar en vivo (ver "Dónde espera un reintento").

**4. Lo que queda registrado.**

```powershell
docker compose -f docker-compose.demo.yml logs -f worker beat
```

Se ven los eventos JSON de cada job, el borrado del video al cerrarse
(retención, Ley 1581) y, cada minuto, el resumen de estado del servicio.

**Al terminar:**

```powershell
docker compose -f docker-compose.demo.yml down
```

### Qué se observó al prepararla

> Corrido el 2026-09-24 en este portátil con un video de marcha de una
> persona real (19,8 MB, 334 frames) y MediaPipe Heavy. El almacenamiento era
> entonces un emulador de Cloud Storage.

| Caso | Resultado |
|---|---|
| Recorrido completo | Respuesta en 3,7 s; `done` a los 43 s (36,8 s de procesamiento, en línea con los 35,3 s de `pose-benchmark` en 2 CPU); callback entregado al primer intento con firma válida; 334 de 334 frames con ángulo (113,5° a 175,3°) |
| Borrado del video | El bucket del emulador quedó vacío al cerrarse el job |
| Sin firma | 401 inmediato |
| Endpoint hacia `169.254.169.254` | `invalid_results_endpoint` |
| Plataforma caída | Dos 503 (a los 0 y 5 s) y entrega en el tercer intento, 33 s después |

> Repetido el 2026-10-08 con el video en PostgreSQL, con un video sintético
> sin personas de 47,3 MB (610 frames de ruido):

| Caso | Resultado |
|---|---|
| Recorrido completo | Respuesta en 6,3 s; `done` con el aviso entregado al primer intento; la tabla `videos` quedó vacía al cerrarse el job |
| Nombre del archivo | No aparece en ningún log: solo la ruta `videos/<uuid>.mp4` |
| Memoria de la API | Pico de 351 MiB con un video; 662 MiB con dos de 47 MB a la vez (ver "Decisiones de tamaño") |

### Qué NO muestra

- **La precisión.** Esta demo muestra que el servicio funciona, no qué tan
  bien mide. La comparación contra gold standard vive en el repo hermano
  (`entorno-pruebas`) y no se repite acá.
- **El rendimiento en Render.** Los tiempos son de este portátil.
- **La integración con la plataforma real.** Siguen abiertos los bloqueadores
  de `docs/DECISIONES_TECNICAS.md`: el esquema HMAC, la semántica de `points` y el disparo del
  flujo.

El video de ejemplo es el de una persona real y **no se versiona** en este
repositorio. Se puede usar cualquier otro video.

## Medir el rendimiento

Tres números del servicio están puestos a ojo y necesitan datos para
confirmarse: la latencia de la validación con OpenCV, el umbral de
`STUCK_JOB_TIMEOUT_MINUTES` y que el `visibility_timeout` de Redis (una hora)
supere al video más lento. `app/tools/benchmark_pipeline.py` mide lo necesario
corriendo el **mismo código que usa producción**:

```powershell
python -m app.tools.benchmark_pipeline videos\corto.mp4 videos\largo.mp4
python -m app.tools.benchmark_pipeline "videos\*.mp4" --runs 5 --json medidas.json
python -m app.tools.benchmark_pipeline video.mp4 --models mediapipe_lite mediapipe_heavy
```

> **Correrlo en un portátil sirve para probar la herramienta, no para decidir.**
> Las cifras solo valen en el hardware de producción (Render: worker en 4 CPU
> y 8 GB, sin GPU) y con videos representativos, incluido el más largo y pesado que la
> plataforma pueda enviar.

**Para medir la RAM hace falta `psutil`**, que viene en
`requirements-dev.txt` pero no en `requirements/`: es una herramienta de
desarrollo y no tiene por qué viajar en la imagen de producción. Si se instaló
solo el de producción, el pico de memoria sale como `n/d` y el resto de las
métricas funciona igual.

Dos cosas que ensucian la primera corrida en una máquina nueva: MediaPipe
descarga el modelo la primera vez que se usa (en Render no pasa, el Dockerfile
los deja descargados), y la carga del modelo se paga completa. Por eso el
reporte separa `primera corrida` de la mediana — para decidir, usa la mediana.

Mide un job a la vez. Como en producción el worker corre con `--concurrency N`
y los jobs compiten por CPU, para estimar ese caso hay que lanzar la
herramienta en N terminales a la vez.

### Primera medición en Render

Con el servicio desplegado (2026-10-09), enviando con `app/tools/demo_send.py`
un video de sentadilla descargado de un sitio de videos libres. Es **un solo
video**: sirve como orden de magnitud, no para fijar los umbrales.

| | |
|---|---|
| Video | 1 MB, 10,9 s, 720×1280 (vertical, de celular), 25 fps: 272 frames |
| Hardware | Worker `4c-8g`, `--concurrency=4`, un job a la vez |
| Respuesta del POST | 2,0-2,4 s |
| Procesamiento (MediaPipe Heavy) | 32,4 s y 32,0 s en dos envíos: ~8,5 frames/s, unas 3 veces la duración del video |
| Ángulo de la rodilla derecha (24, 26, 28) | 179,78° a 37,14°, idénticos en los dos envíos |
| Callback | Entregado al primer intento, ~1 s después de `done` |

- **El segundo envío no fue más rápido**: el modelo se crea en cada job, así
  que su carga va incluida siempre.
- **Más núcleos no aceleran un video**: la cifra es parecida a la de 2
  núcleos en `pose-benchmark`. Lo que cambia con `4c-8g` es cuántos videos se
  procesan a la vez.
- **Umbral de jobs atascados**: a este ritmo, un video de 20 s tardaría cerca
  de un minuto, muy lejos de los 15 minutos de `STUCK_JOB_TIMEOUT_MINUTES`. No
  se ajusta hasta medir videos de otras duraciones y resoluciones.

## A dónde puede enviarse el callback

El `resultsEndpoint` **lo elige quien hace la solicitud**. Sin restricción,
este servicio se convierte en un emisor de peticiones firmadas hacia donde le
digan: direcciones internas de la red del proveedor, el servicio de metadatos
de la nube, o un tercero cualquiera al que la política de reintentos le
mandaría intentos durante horas. Y con `HMAC_ENFORCE` en `false` —que es como
está hoy, porque la plataforma todavía no firma— eso queda al alcance de
cualquiera que llegue a la API.

**El control real es `CALLBACK_ALLOWED_HOSTS`**, una lista separada por comas
de los hosts a los que se acepta enviar. Un host que esté en ella se acepta tal
cual, **incluso si es interno**: ponerlo ahí es una decisión explícita de quien
configura el servicio, y es lo que permite apuntar a una máquina local para
probar (`CALLBACK_ALLOWED_HOSTS=localhost,127.0.0.1`).

Mientras la lista esté vacía, lo que queda es defensa en profundidad:

| Se rechaza | Ejemplo |
|---|---|
| Esquemas que no sean http/https | `file:///etc/passwd` |
| IPs privadas, loopback, link-local o reservadas | `http://169.254.169.254/`, `http://127.0.0.1/`, `http://10.0.0.5/` |
| Hosts sin punto: no existen en internet | `http://localhost/`, `http://redis:6379/` |
| `http://` en producción | los ángulos son datos de salud y no viajan en claro |

**El motivo concreto va al log, no a la respuesta.** Quien manda la solicitud
recibe siempre `invalid_results_endpoint`: distinguir "malformado" de "host no
permitido" le serviría a un atacante para averiguar qué direcciones acepta el
servicio, y no le sirve de nada a la plataforma, que solo necesita saber que su
endpoint no se aceptó.

### Se comprueba dos veces

En la entrada y otra vez **justo antes de enviar**. La primera bastaría si la
fila se hubiera creado hoy, pero una fila pudo guardarse antes de que se
restringiera la lista, y el barrido de jobs colgados encola callbacks leyendo
la tabla, sin volver a pasar por la validación de entrada. En la entrega el
rechazo es **definitivo** y no reintentable: insistir durante horas contra una
dirección no permitida es justamente el abuso que esto evita.

> **Límite conocido.** Con la lista vacía, las comprobaciones miran la
> dirección tal como viene escrita: un nombre de dominio que resuelva a una
> dirección interna las pasa. Resolver el DNS en la validación tampoco lo
> cerraría —entre la validación y el envío la resolución puede cambiar—, así
> que **el único control que de verdad cierra esto es llenar la lista**. Por
> eso `deploy/render/render.yaml` la trae con un relleno que hay que reemplazar, y no vacía:
> falla de forma ruidosa y visible en vez de dejar la puerta abierta en
> silencio.

> Verificado contra la API corriendo en un contenedor el 2026-09-23: un `POST
> /video/` apuntando al servicio de metadatos, a loopback y al host interno de
> Redis se rechazó en los tres casos **antes de subir el video**, sin gastar
> una sola fila en la tabla; un endpoint público pasó la validación y siguió su
> curso. El log registró el motivo exacto de cada rechazo mientras la respuesta
> llevaba solo el código genérico.

## Health check

`GET /health` no lleva firma HMAC (lo sondean Render y el monitoreo) y
comprueba de verdad las dependencias en vez de responder `ok` porque el proceso
esté vivo:

```json
{"status": "ok", "checks": {"database": "ok", "queue": "ok"}}
```

Si algo no responde devuelve **503** con el desglose, para que se sepa dónde
mirar sin entrar al log:

```json
{"status": "degraded", "checks": {"database": "error", "queue": "ok"}}
```

**Por qué comprueba y no responde `ok` a secas.** Render decide con este
endpoint si un despliegue tuvo éxito. Un `ok` incondicional deja pasar a
producción una versión con la base mal configurada, y no es hipotético: la URL
de PostgreSQL que entrega Render viene sin driver, y `create_async_engine` no
abre ninguna conexión al construirse — la primera consulta real es la que
descubre si las credenciales y el host sirven. El `SELECT 1` es esa consulta.

**Qué NO comprueba: el almacenamiento aparte.** Cuando el video espera en un
servicio de terceros (Supabase Storage, Firebase), ese servicio está al otro
lado de internet y este endpoint se sondea cada pocos segundos. Incluirlo
significaría que una caída del proveedor reinicia nuestras instancias —cosa
que no arregla nada— y que cada sondeo paga una ida y vuelta hasta allá. Una
falla del almacenamiento se manifiesta donde corresponde: en el job que la
sufre, con su error y su callback. Con el video en la base (la configuración
de Render), el sondeo de la base ya lo cubre.

**El detalle del error no sale en la respuesta**, solo la palabra `error`. Los
mensajes de SQLAlchemy y de redis-py pueden incluir la URL de conexión, y ahí
viaja la contraseña; `/health` es público, así que el error completo va al log
del servicio.

> **Qué provoca un 503, según la documentación de Render.** Durante un
> despliegue: se cancela y la versión anterior sigue recibiendo tráfico — que
> es lo que se busca. Con el servicio ya corriendo: a los 15 segundos de fallas
> consecutivas Render deja de enrutarle tráfico y a los 60 segundos reinicia la
> instancia. O sea que una caída larga de PostgreSQL provoca reinicios. Se
> acepta a conciencia: sin base de datos este servicio no puede aceptar un solo
> video, así que no hay nada que preservar manteniéndolo "sano".

> Verificado contra servicios reales el 2026-09-21: con todo arriba responde
> 200; con PostgreSQL caído, 503 señalando `database`; con Redis caído, 503
> señalando `queue`; y al volver las dos, **vuelve solo a 200** sin reiniciar
> nada. Esa recuperación es la mitad que importa — un health check que se
> quedara pegado en 503 dejaría a Render reiniciando en bucle. La contraseña no
> apareció ni en la respuesta ni en el log.


## Observabilidad

Desplegado en Render, el log es lo único que se ve desde afuera. `/health`
responde si PostgreSQL y Redis contestan, pero con las dependencias sanas y el
worker atascado sigue devolviendo 200 — y Flower, que da la profundidad de la
cola, solo está en el `docker-compose` local.

Por eso el worker emite cada `STATUS_REPORT_INTERVAL_MINUTES` (15 por defecto)
una línea con el estado del servicio:

```json
{"event": "service_status", "window_minutes": 60, "received": 5, "done": 2,
 "failed": 1, "queued": 2, "processing": 1, "undelivered_callbacks": 2,
 "retrying_callbacks": 3}
```

Son tres preguntas distintas y por eso se cuentan por separado:

- **Actividad reciente** (`received`, `done`, `failed`): cuántos videos
  entraron en la ventana y cómo terminaron.
- **Backlog** (`queued`, `processing`): cuántos siguen sin terminar **ahora**,
  sin importar cuándo entraron. Un job de hace tres horas que sigue en `queued`
  no aparece en la ventana reciente, pero es exactamente el síntoma de que el
  worker no da abasto.
- **Deuda con la plataforma** (`undelivered_callbacks`): resultados calculados
  y guardados que la plataforma nunca recibió: los `rejected` y los que agotaron
  la ventana de reintentos. Aparte va `retrying_callbacks`, los que todavía se
  están intentando: no son deuda aún, pero si crecen es que la plataforma no
  está respondiendo.

El resumen sale **siempre**, también con todo en ceros: es el latido que
permite distinguir "no llegó ningún video" de "el worker lleva horas muerto",
que desde afuera se ven idénticos. Lo que solo sale cuando hay algo que mirar
es una advertencia aparte, con nivel `WARNING`, por los resultados sin
entregar.

> **Alcance de esa advertencia.** Cuenta todos los pendientes, no solo los
> recientes, así que se repite en cada resumen hasta que alguien los atienda.
> Es deliberado: ahí solo quedan los avisos que el sistema ya no va a
> reintentar solo (`rejected` y `exhausted`), y el número no baja sin
> intervención.

**Requiere que corra Celery Beat**, igual que el barrido de jobs colgados.

### El nivel del log

Sale de `LOG_LEVEL` (`INFO` por defecto) y vale para los dos procesos. Un
valor mal escrito hace que el servicio no arranque, en vez de quedarse en
silencio con el nivel por defecto.

El nivel **no** se pasa por `--loglevel` en la línea de comandos de Celery: ese
flag queda ignorado a propósito. En Render la línea de comandos vive en
`deploy/render/render.yaml`, así que bajar el nivel a `DEBUG` para diagnosticar un incidente
habría exigido un despliegue — justo lo que la variable existe para evitar.
Quien se lo impide a Celery es la señal `setup_logging` en `app/platforms/render/worker.py`.

### Dónde guarda Beat su estado

`BEAT_SCHEDULE_PATH`. El contenedor corre sin root y `/app` es de root, así que
con el valor por defecto de Celery **Beat arranca y muere** con
`Permission denied: 'celerybeat-schedule'`. El fallo es silencioso: el resto
del servicio sigue procesando videos con normalidad, y lo único que desaparece
es el barrido de jobs colgados y este resumen. El `Dockerfile` crea un
directorio escribible y apunta la variable ahí, para que la imagen sea correcta
sin depender de que alguien pase `-s`.

> Verificado con contenedores el 2026-09-22 contra PostgreSQL y Redis reales,
> con datos sembrados a propósito: el resumen reportó `received: 5` y a la vez
> `queued: 2`, incluyendo un job de tres horas que queda fuera de la ventana
> pero sigue en el backlog — que es justo la distinción que el resumen existe
> para hacer. Los números se contrastaron contra SQL. Con `LOG_LEVEL=WARNING`
> y `--loglevel=info` en la línea de comandos, el resumen desaparece y la
> advertencia se mantiene.

## Retención de datos (Ley 1581 de 2012)

Un video de una persona haciendo un ejercicio de rehabilitación, y los ángulos
articulares que salen de él, son **datos sensibles** bajo el artículo 5 de la
Ley 1581: de salud y biométricos a la vez. El principio de temporalidad prohíbe
conservarlos más allá de la finalidad que justificó recogerlos, y esa finalidad
acá es calcular los ángulos y entregárselos a la plataforma.

El servicio minimiza en dos momentos:

**1. Al cerrar cada job, el worker borra el video** del almacenamiento externo.
Es el dato más sensible y el que antes deja de hacer falta: una vez calculados
los ángulos no vuelve a usarse. Este servicio tampoco es su sistema de
registro — la plataforma lo envió y lo conserva; nuestra copia existe solo para
procesarlo. Se borra también cuando el job **falla**: con la guarda de
re-entrega, esa tarea no se va a reprocesar. Se pierde poder inspeccionar el
video para diagnosticar, y es un costo aceptado.

**2. Pasados `DATA_RETENTION_DAYS` (30 por defecto), un barrido minimiza la
fila.** Borra los ángulos, la ruta del video y el `resultsEndpoint` —que suele
llevar identificadores del paciente o de la sesión— y **conserva** el id, el
estado, el ejercicio, el modelo usado y los tiempos, que no identifican a
nadie. Minimizar es quitar lo que identifica, no destruir el registro
operativo: eso es lo que permite seguir midiendo el servicio.

Ese barrido es además la red del primer momento: si el borrado del video falló
(el almacenamiento no respondía), la fila conserva su `video_url` y acá se
vuelve a intentar.

**El contrato v2 no cambia la minimización.** Sus claves nuevas (`frames`, con
los 33 landmarks por frame —dato biométrico—, y `joints`) viven dentro del
mismo JSONB `results`, y el barrido lo pone en NULL de SQL completo, no clave
por clave. Dos pruebas de `tests/test_contract_v2.py` lo vigilan: la
minimización limpia `results` entero, y ningún dato v2 vive en otra columna.

> **Dónde queda un video borrado.** Para el servicio desaparece al instante.
> Desde el 2026-10-16 los videos están en Supabase Storage y no pasan por la
> base de Render, así que ya no quedan en sus respaldos (WAL), como pasaba con
> `STORAGE_BACKEND=postgres`. Los respaldos de Supabase no incluyen los objetos
> de Storage: ver "Los videos en Supabase Storage".

> **El plazo no es una decisión técnica.** Los 30 días son un punto de partida
> conservador. El número correcto depende de la política de tratamiento de
> datos de la plataforma y de cuánto tiempo necesite el equipo clínico poder
> consultar un resultado. **Hay que confirmarlo con quien responda por el
> tratamiento.**

**Requiere que corra Celery Beat.** Sin él los datos se conservarían
indefinidamente y el servicio quedaría incumpliendo el principio de
temporalidad sin que nada lo hiciera evidente.

### Dos cuidados que tiene el barrido

- **Una fila cuyo video no se pudo borrar NO se minimiza.** Si se le borrara el
  `video_url`, el video quedaría guardado sin ningún registro de su ruta:
  imposible de borrar después, o sea el video de un paciente conservado para
  siempre. Queda intacta y la próxima corrida lo reintenta. Un blob
  problemático no frena la minimización de los demás.
- **Antes de destruir un resultado que la plataforma nunca recibió, lo avisa
  por log con sus ids.** Sin eso, un callback agotado se borraría en silencio
  al vencer el plazo y nadie sabría que ese procesamiento se perdió. El aviso
  no detiene la minimización: el plazo legal no se suspende porque haya una
  entrega pendiente. Sigue siendo la última línea de defensa aunque ahora
  exista la re-entrega: esa insiste 3 días, y lo que llegue acá es lo que ni
  así se pudo entregar.

> Verificado contra PostgreSQL real el 2026-09-22 con un caso de cada tipo. Un
> job de 5 días se minimizó y uno de 1 hora quedó intacto; uno cuyo video no se
> pudo borrar quedó entero para la corrida siguiente; y una segunda pasada solo
> volvió a encontrar ese, sin repetir nada sobre los ya minimizados. Las filas
> minimizadas conservan `exercise`, `pose_model` y `processing_seconds`.
>
> Esa verificación destapó dos fallos que los tests con dobles no podían ver.
> **Uno:** en una columna JSONB, SQLAlchemy traduce `None` al valor JSON
> `null`, que no es el `NULL` de SQL — la fila quedaba con el contenido borrado
> pero `results IS NOT NULL` seguía siendo cierto, así que el barrido la
> reseleccionaba para siempre. **Dos:** se borraba el `video_url` aunque el
> borrado en Firebase hubiera fallado, dejando el blob huérfano.



## Jobs atascados

Hay **dos** formas de que un job se quede colgado, con causas distintas, y las
dos son silenciosas: no hay excepción ni log de error, solo una fila que dejó
de avanzar mientras la plataforma espera un callback que no va a llegar.

| Estado | Qué pasó | Umbral | Código |
|---|---|---|---|
| `processing` | El worker tomó el trabajo y desapareció a mitad. El OOM-kill es el caso realista: videos de hasta 200 MB y varios procesos compartiendo la RAM. | `STUCK_JOB_TIMEOUT_MINUTES` (15) | `processing_timeout` |
| `queued` | Nadie lo tomó nunca: la tarea encolada se perdió (un reinicio del broker vacía la cola) o no quedó ningún worker vivo. | `STUCK_JOB_QUEUED_TIMEOUT_MINUTES` (60) | `queued_timeout` |

`app/core/stuck_jobs.py` barre los dos casos cada
`STUCK_JOB_SCAN_INTERVAL_MINUTES` (5 por defecto), cierra las filas como
`failed` con el código que corresponde **y le avisa a la plataforma**,
encolando el mismo callback de fallo que usaría un job que falló normalmente.
Ese aviso importa: cerrar la fila saca del engaño a quien mire la base de
datos, pero sin avisar, la plataforma seguiría esperando indefinidamente.

También **borra el video**, como cualquier otro cierre. Antes no lo hacía, y
el video de un job cerrado por el barrido quedaba guardado hasta la retención,
30 días después.

**Un worker que termina tarde no pisa el cierre.** Si el barrido cerró un job
que en realidad seguía procesándose, el worker, al terminar, no lo deja en
`done` ni encola un aviso de éxito: tomar y cerrar el job solo se hace si sigue
abierto (`update_open_job` en `app/utils/db_connect.py`). Antes, la plataforma
recibía primero el fallo y después el éxito por el mismo job. Lo mismo vale
si la misma tarea llega dos veces.

> **Por qué el umbral de `queued` es más alto.** Esperar en la cola es normal.
> Con C procesos en el worker (`--concurrency`, 4 en Render), una acumulación
> de N videos hace que el último espere alrededor de N/C veces lo que tarda
> uno. Si el umbral quedara por debajo de
> esa espera, el barrido cerraría como perdidos justamente los jobs de un pico
> de carga — el peor momento para equivocarse. Los 60 minutos son
> provisionales por la misma razón que los 15: falta medir.

**Requiere que corra Celery Beat**, además del worker. Sin Beat el servicio
procesa videos con normalidad, solo que nadie cierra los jobs colgados.

**Qué NO cubre.** El barrido corre *sobre* el worker y existe para detectar que
un worker murió, así que solo funciona mientras quede alguno vivo. Cubre el
caso realista en Render —un proceso hijo del pool muere por OOM y sus hermanos
siguen consumiendo la cola—, pero no cubre que el servicio worker caiga
entero: ahí Beat sigue publicando barridos que nadie ejecuta y se acumulan en
la cola hasta que vuelva un worker. Detectar esa caída es trabajo del
monitoreo del proveedor, no de este mecanismo.

> Verificado contra Redis y PostgreSQL reales el 2026-09-21: Beat publica la
> tarea, el worker la consume, las filas colgadas en `processing` y en `queued`
> pasan a `failed` con su código, y el aviso sale hacia `resultsEndpoint`.
> Mientras tanto un `processing` reciente, un `queued` reciente, un `done`
> viejo y un `failed` viejo quedan intactos. Los barridos siguientes devuelven
> vacío sin repetir el cierre ni escribir ruido en el log.

## Apagado ordenado

Cada despliegue de Render apaga los procesos viejos: manda `SIGTERM` y, pasado
el plazo, `SIGKILL`. El plazo **por defecto son 30 segundos**, y para este
servicio eso es poco.

Celery responde al `SIGTERM` con un **apagado caliente**: deja de tomar tareas
nuevas y espera a que terminen las que ya están corriendo. Pero en Celery 5.4
esa espera no tiene límite propio —`worker_soft_shutdown_timeout` llegó en
5.5—, así que el único reloj que manda es el de Render. Si el video en curso no
alcanza a terminar, llega el `SIGKILL` y el proceso muere a mitad: el job queda
en `processing`, el barrido lo cierra quince minutos después y a la plataforma
le llega un fallo por un video que no tenía nada malo.

Por eso `deploy/render/render.yaml` fija `maxShutdownDelaySeconds` en cada servicio:

| Servicio | Plazo | Por qué |
|---|---|---|
| `pose-worker` | **300 s** (el máximo que permite Render) | Todo video que se procese en menos de cinco minutos termina normalmente durante el despliegue. |
| `pose-api` | 60 s | Solo tiene que terminar de responder lo que esté en curso: validar y encolar. |
| `pose-beat` | 30 s | No ejecuta tareas, solo las publica. No hay nada que esperar. |

> **Cómo se sabe si el apagado fue ordenado.** Desde afuera los dos casos se
> ven igual: el servicio desaparece. La diferencia está en el log — un apagado
> limpio deja `Conexión a PostgreSQL cerrada` y, en el worker, `Worker apagado:
> conexiones cerradas y loop liberado`. Si esas líneas no aparecen, hubo
> `SIGKILL`.

### Tareas re-entregadas

El worker corre con `task_acks_late`, así que una tarea que muere sin
confirmarse se vuelve a entregar cuando vence el `visibility_timeout` de Redis.

Deja de ser útil cuando el barrido ya lo cerró. Como el barrido actúa a los 15
minutos y la re-entrega llega a la hora, el barrido gana siempre — y
reprocesar entonces le mandaría a la plataforma un callback de **éxito**
después de haberle mandado uno de **fallo** por el mismo job. Por eso
`process_video_job` descarta los jobs que ya están en un estado final (ver
`JobStatus.terminal()`), lo que de paso evita entregar dos veces el resultado
de un job en `done` cuya confirmación se perdió.

#### Ninguna tarea espera dentro de Redis

El `visibility_timeout` es lo que tarda Redis en dar por perdida una tarea
entregada y no confirmada. Una tarea programada con espera (`countdown`) se
entrega al worker **enseguida** y queda sin confirmar hasta que se ejecuta. Eso
causaba dos problemas con los reintentos de callback, que antes se programaban
así:

- **Duplicados.** Si la espera superaba el `visibility_timeout`, Redis
  re-encolaba la tarea mientras el primer worker todavía la guardaba, y al
  llegar la hora se ejecutaban todas las copias. Con reintentos de 1 y 2 horas
  y el plazo de 1 hora, la plataforma recibía esos avisos dos o tres veces.
- **Reintentos varados.** Si el worker se reiniciaba mientras guardaba un
  reintento, ese reintento quedaba retenido en Redis hasta que venciera el
  plazo.

> Verificado el 2026-09-24 con Redis real: con `visibility_timeout` de 10 s y
> una tarea programada a 250 s, la tarea se ejecutó **3 veces en el mismo
> segundo**. Y en la demo, un worker recreado mientras guardaba un reintento
> lo dejó retenido una hora exacta.

Por eso la espera entre reintentos vive ahora en PostgreSQL (ver "Dónde espera
un reintento"), y **ninguna tarea se programa con espera**: un test recorre
`app/` y falla si alguien vuelve a usar `countdown`, `eta`, `apply_async` o
`.retry(`. Con solo tareas inmediatas, el `visibility_timeout` queda en una
hora explícita (`VISIBILITY_TIMEOUT_SECONDS` en `app/platforms/render/worker.py`) y solo
tiene que superar al video más lento, que es lo que mide
`app/tools/benchmark_pipeline.py`.

> Verificado con contenedores el 2026-09-22, usando `docker stop` porque manda
> `SIGTERM`, espera y luego `SIGKILL`, igual que Render. Para tener una tarea
> lenta sin tocar el código se bloqueó la tabla `jobs` desde otra sesión de
> PostgreSQL. Con plazo amplio, el worker esperó **18,7 s**, la tarea terminó
> bien, se ejecutó la limpieza y el contenedor salió con código 0. Con plazo de
> 10 segundos y la misma tarea en curso: código de salida **137** (`SIGKILL`),
> la tarea sin terminar y la limpieza sin ejecutarse. La API apaga en menos de
> un segundo.

## Imagen Docker (despliegue)

Una imagen por plataforma (una etapa del Dockerfile cada una). Dentro de una
plataforma, la misma imagen corre todos los procesos cambiando solo el
comando. En Render:

```powershell
docker build --target render -t pose-service .
# API (comando por defecto)
docker run --env-file .env -p 8000:8000 pose-service
# Worker
docker run --env-file .env pose-service celery -A app.platforms.render.worker.celery worker --concurrency=4
# Beat (barrido de jobs atascados) -- va en UNA sola instancia, nunca replicado
docker run --env-file .env pose-service celery -A app.platforms.render.worker.celery beat
```

- Corre **sin root** (`appuser`). Los modelos Lite y Heavy de MediaPipe se
  descargan al construir: MediaPipe los baja en tiempo de ejecución dentro de
  `site-packages`, y sin root esa descarga fallaría con el primer video.
- `.env` y las credenciales de Firebase quedan fuera de la imagen por
  `.dockerignore`: se inyectan en tiempo de ejecución, nunca se hornean.
- El workflow `security-scan.yml` construye esta imagen y la escanea con Trivy
  en cada push, en cada PR y una vez por semana. Las excepciones aceptadas
  están en `.trivyignore`, cada una con su justificación.

## Despliegue en Render

**El servicio va en Render** (decisión del grupo de investigación,
2026-10-08): los tres procesos, la cola y la base. Los videos esperan en
Supabase Storage (desde el 2026-10-16).
`deploy/render/render.yaml` es el blueprint: describe la API, el worker, Beat,
la Key Value que hace de cola y la base (`pose-db`, Render Postgres), con las
variables ya cableadas entre ellos. Se crea desde el Dashboard con **New >
Blueprint** apuntando a este repositorio, en vez de crear cinco recursos a mano
y mantener sus variables sincronizadas.

**Los videos esperan en un bucket privado de Supabase Storage**
(`STORAGE_BACKEND=supabase`, bucket `pose-videos`) y el worker borra cada uno
al procesarlo. Hasta el 2026-10-16 esperaban en la misma base de Render: un
video de celular la dejaba sin memoria. Ver "Los videos en Supabase Storage",
más abajo.

**Fase actual: pruebas aisladas de la plataforma.** Los únicos que envían
videos somos nosotros, con `app/tools/demo_send.py`, y los avisos van a
webhook.site. Por eso `HMAC_ENFORCE` está en `true` y `CALLBACK_ALLOWED_HOSTS`
solo admite `webhook.site`. Por webhook.site pasan los ángulos de cada video:
en esta fase se prueba **solo con videos sin datos de pacientes**.

**Render construye la imagen desde GitHub** (decidido 2026-10-09; antes la
bajaba de Docker Hub). Cada commit que llega a `main` se despliega en los tres
servicios **solo cuando pasan todos sus checks de GitHub**
(`autoDeployTrigger: checksPass`): la suite, el escaneo de seguridad y
GitGuardian. Si uno falla, ese commit no se despliega. Así:

- lo que corre en Render es lo que pasó los tests, sin credenciales de Docker
  Hub ni deploy hooks;
- volver atrás es elegir un despliegue anterior en el Dashboard (Events >
  Rollback), o revertir el commit.

Dos cosas a tener en cuenta:

- **Render construye la última etapa del Dockerfile**, porque el blueprint no
  permite elegir otra. Por eso la etapa `render` va al final; un test lo
  comprueba.
- **Cada servicio construye su propia imagen**: un despliegue son tres builds
  de mediapipe + opencv, que tardan varios minutos y consumen minutos de build
  del workspace. `buildFilter` evita reconstruir cuando el cambio no toca la
  imagen (README, tests, workflows); un test comprueba que cubra todo lo que
  copia el Dockerfile.

Las migraciones corren como `preDeployCommand` del servicio de la API
(`alembic upgrade head`): una sola vez por despliegue, en una instancia aparte,
antes de que la versión nueva reciba tráfico. Si fueran parte del comando de
arranque, cada réplica intentaría migrar a la vez.

### Lo que el blueprint NO puede hacer

Dos cosas:

1. **Darle a Render acceso a este repositorio**, que es privado (la app de
   GitHub de Render, ver "Orden").
2. **Cargar `SUPABASE_SECRET_KEY` en pose-api y en pose-worker**, con el mismo
   valor en los dos. Es el único secreto que no sale del blueprint: la URL de
   la base sale de `pose-db` (`fromDatabase`) y la clave HMAC la genera Render.
   - Va en el blueprint como `sync: false` **por servicio**: Render ignora en
     silencio un `sync: false` dentro de un grupo de variables.
   - Render la pide solo al **crear** el blueprint. En uno ya creado ignora las
     variables `sync: false`: hay que agregarla a mano en el Dashboard
     (Environment de cada servicio) antes del despliegue que la necesita.
   - Beat no la lleva: no toca el almacenamiento.

Un test (`test_only_the_supabase_key_is_pasted_by_hand`) fija exactamente esa
lista: cualquier otro secreto a mano lo hace fallar.

### Orden

```
1. GitHub: que la app de Render tenga acceso a este repositorio
   (github.com > Settings > Applications > Render > Configure, o al conectar
   la cuenta desde Render).
2. Unir a main y esperar a que los checks del commit pasen.
3. Dashboard > New > Blueprint > este repositorio, rama main, Blueprint Path
   deploy/render/render.yaml. Render muestra lo que va a crear (tres
   servicios, la Key Value y pose-db) y su costo. Al confirmar construye la
   imagen tres veces (varios minutos) y, antes de levantar la API, el
   pre-despliegue corre las migraciones en pose-db.
4. Comprobar: GET /health responde 200, el log del worker dice "ready".
5. Probar desde una máquina del equipo, con un video sin datos de pacientes
   y de menos de 50 MB:
     $env:HMAC_SECRET = "<el valor generado, copiado del Dashboard>"
     python -m app.tools.demo_send video.mp4 --api https://<pose-api>.onrender.com --endpoint https://webhook.site/<id>
```

Desde ahí, cada merge a main que toque la imagen se despliega solo. Un cambio
en `render.yaml` (planes, variables) lo aplica la sincronización del
blueprint: automática si el blueprint tiene **Auto Sync**, o con **Manual
Sync** desde su página.

> **Manual Deploy no es Manual Sync** (aprendido en la prueba con la
> plataforma, 2026-10-16). Manual Deploy vuelve a desplegar el código con las
> variables que el servicio ya tiene: no lee `render.yaml`. Para que llegue un
> cambio de variables hace falta **Manual Sync**. Las variables `sync: false`
> (`SUPABASE_SECRET_KEY`) tampoco las aplica una sincronización: se cargan a
> mano en el Dashboard.

`HMAC_SECRET` se declara con `generateValue: true`: Render genera un secreto
fuerte una sola vez y se lo entrega igual a los tres servicios. Es a propósito
— si se pidiera por servicio, Render preguntaría tres veces y bastaría un error
de tipeo para que el worker firmara el callback con un secreto que la
plataforma no conoce.

**Al conectar la plataforma** cambian dos valores de `deploy/render/render.yaml`:

- `CALLBACK_ALLOWED_HOSTS` pasa a ser el host de la plataforma, sin
  webhook.site.
- `HMAC_ENFORCE` pasa a `false` (modo advertencia) si la plataforma todavía no
  firma sus solicitudes. Si no, rechazaría con 401 todo lo que envíe.

Y se le entrega el `HMAC_SECRET`.

### Operación: encender y suspender

**Requisitos de Supabase** (una sola vez, antes del primer despliegue; el
blueprint no los crea):

| Bucket | Quién lo usa | Visibilidad |
|---|---|---|
| `pose-videos` | Este servicio (`SUPABASE_STORAGE_BUCKET`) | **Privado**, límite de 50 MB, tipos `video/*`. Si no existe, la subida falla con `Bucket not found` (pasó el 2026-10-16). |
| `videos` | La plataforma de telefisioterapia | **Público**: su backend descarga el video por la URL pública para enviarlo acá. Privado, esa URL responde `Bucket not found`. |

`SUPABASE_SECRET_KEY` va en el Environment de pose-api y pose-worker, con el
mismo valor (ver "Lo que el blueprint NO puede hacer").

**Encender** (en este orden):

1. **`pose-db`.** El pre-despliegue (`alembic upgrade head`) y el `/health` de
   la API la necesitan: sin ella el despliegue falla.
2. **`pose-api`, `pose-worker` y `pose-beat`.** `pose-keyvalue` queda
   encendida siempre: guarda la cola en disco.
3. **`GET /health` → 200** (revisa la base y la cola), y el log del worker dice
   "ready".
4. **Recién entonces, subir videos.**

**Suspender** (al revés, sin dejar videos a medias):

1. **No subir más videos** y esperar a que terminen los jobs en curso y la
   plataforma reciba sus avisos.
2. **`pose-api`**, para que no entre nada nuevo.
3. **`pose-worker` y `pose-beat`.** Un video que llegue con el worker
   suspendido espera en la cola, y pasada una hora el barrido lo da por
   perdido y avisa un fallo.
4. **`pose-db`, al final**: la API, el worker y Beat la usan mientras están
   encendidos.

Para pausas cortas basta con suspender el worker (ver "Costos").

### Render sigue probándose en cada PR

- La suite corre los tests de la plataforma Render, valida
  `deploy/render/render.yaml` contra el esquema oficial y comprueba que sus
  comandos apunten a módulos que existen.
- La demo local (`docker-compose.demo.yml`) corre el camino de Render, con el
  video en la base como en producción.
- La etiqueta `render-supabase-v1` marca el estado anterior, con la base y los
  videos en Supabase (2026-10-07).

### Costos

| Recurso | Plan | USD/mes | Por qué ese |
|---|---|---|---|
| `pose-worker` | `4c-8g` | 175 | Ver "Decisiones de tamaño". |
| `pose-api` | `1c-2g` | 25 | Solo valida y encola, pero necesita memoria para dos videos a la vez (ver "Decisiones de tamaño"). |
| `pose-beat` | `0.5c-512mb` | 7 | Solo publica tareas periódicas. |
| `pose-keyvalue` (cola) | `256mb` | 10 | El plan gratuito no guarda nada en disco y Render puede reiniciarlo cuando quiera: en cada reinicio se pierden **todas** las tareas encoladas. |
| `pose-db` (base de los jobs) | `basic-256mb` | 6 | 100 conexiones. El gratuito vence a los 30 días y se borra. |
| Disco de `pose-db` | 5 GB | 1,50 | 0,30 USD/GB. Se dimensionó para los videos en espera; desde el 2026-10-16 están en Supabase Storage (plan gratuito) y el disco no se achica. |
| **Total** | | **≈ 225** | Más el plan del workspace de Render (Hobby $0 o Pro $25). |

Precios verificados en render.com/pricing el 2026-10-08. Render cobra el
cómputo por segundo **mientras el servicio está encendido**, trabaje o no:
con poco volumen, lo que baja la cuenta es el tamaño de las instancias, no la
cantidad de videos.

Los Background Workers de Render no existen en plan gratuito, y
`preDeployCommand` tampoco está disponible en instancias gratuitas: los tres
procesos son de pago sí o sí. **Tener la cuenta en un plan de pago no rescata
a un recurso puesto en plan gratuito**: *"Upgrading your workspace plan does
not remove limitations on Free instances."*

> **Ojo con la cola gratuita, más allá del precio.** Si se pierden las tareas
> encoladas, el barrido de jobs atascados las rescata (cubre `queued` y
> `processing`), pero recién cuando vence su umbral: cada reinicio sería un
> lote de videos demorados y avisos de fallo a la plataforma.

**Suspender el worker entre pruebas.** Es el 78 % de la cuenta, y mientras
probamos de a uno pasa la mayor parte del tiempo sin trabajo. Se suspende en
el Dashboard (`pose-worker` > Settings > Suspend) y se reanuda antes de la
siguiente prueba. Dos cuidados:

- Suspenderlo sin videos pendientes. Un video que llegue con el worker
  suspendido espera en la cola, y si pasa más de una hora ahí el barrido lo
  da por perdido y le avisa a la plataforma con un fallo.
- La API, Beat, la cola y la base quedan encendidos: son ≈ 50 USD/mes, y la
  base y la cola guardan los jobs. Suspender todo sirve para pausas largas.

**Más CPU todavía**: `8c-16g` (300 USD/mes) con `--concurrency=8` procesa ocho
videos a la vez.

### Decisiones de tamaño

- **`pose-worker` va en `4c-8g`** (4 CPU, 8 GB), aprobado por el grupo el
  2026-10-09 (antes `2c-4g`, decidido el 2026-10-06 por presupuesto). Más
  núcleos dan más videos a la vez, no un video más rápido: cada proceso usa
  un núcleo, como antes. La RAM sobra: en `pose-benchmark` (Render `2c-8g`,
  2026-10-01) MediaPipe Heavy llegó a 649 MB de pico por proceso, y con los
  cuatro procesos del worker son ~2,6 GB. El margen cubre la deriva de
  memoria entre jobs, que `MALLOC_ARENA_MAX=2` acota.
- **El worker corre con `--concurrency=4`**, uno por CPU: el procesamiento de
  video satura el núcleo, así que más procesos solo se estorbarían. Un test
  comprueba que la concurrencia sea igual a los núcleos del plan y que los
  procesos quepan en su memoria.
- **`pose-beat` nunca se escala a más de una instancia** (`numInstances: 1`).
  Cada réplica publicaría la misma tarea periódica y el barrido correría
  tantas veces como réplicas haya.
- **`pose-api` va en `1c-2g`** (1 CPU, 2 GB), aprobado por el grupo el
  2026-10-09: 25 USD/mes, 18 más que `0.5c-512mb`. El límite de la API es la
  memoria, no la CPU. Cada video llega a estar varias veces en memoria al
  recibirlo: el cuerpo entero para verificar la firma, el contenido para
  validarlo y la copia que se envía a PostgreSQL. Medido el 2026-10-08 con la
  demo local y un video de 47 MB: pico de 351 MiB con uno y **662 MiB con dos
  a la vez**. En `0.5c-512mb` dos videos simultáneos reiniciarían la
  instancia por falta de memoria y las dos subidas fallarían; Render no tiene
  un plan entre 512 MB y 2 GB. Un
  test exige al menos un 50 % de margen sobre lo medido. Si se sube
  `MAX_VIDEO_SIZE_MB`, hay que volver a medir.

### Alternativa que se descartó para la base

- **Neon** (Postgres serverless, plan gratuito permanente, 0.5 GB): **no
  sirve para este servicio**. Su plan gratuito da 100 CU-hora al mes y la
  instancia mínima es 0.25 CU, así que una base que nunca queda ociosa consume
  ~187 CU-hora al mes (750 h × 0.25) y el cómputo se suspende hasta el mes
  siguiente. Y acá nunca queda ociosa: Celery Beat dispara tareas cada pocos
  segundos, indefinidamente.

### La URL de PostgreSQL

Render entrega la URL de su base como `postgresql://...`, sin driver. Este
servicio habla con PostgreSQL solo de forma asíncrona (`asyncpg` está en
`requirements/base.txt`, `psycopg2` no), así que SQLAlchemy intentaría cargar
psycopg2 y fallaría con `ModuleNotFoundError` tanto al arrancar como en el
`alembic upgrade head` del pre-despliegue. `app/core/config.py` normaliza el
esquema a `postgresql+asyncpg://` al leer la configuración, en un solo lugar
para que ningún punto de uso pueda olvidarlo. Cubierto por
`tests/test_database_config.py`.

La misma normalización traduce `?sslmode=...` a `?ssl=...`. Las URLs externas
(la de Render vista desde fuera, o la de Supabase) piden TLS con `sslmode`, que
es el nombre de libpq. asyncpg no lo conoce y fallaba al conectar con
`TypeError: connect() got an unexpected keyword argument 'sslmode'`.

**En producción, una base fuera de la red privada de Render exige TLS.** El
engine y Alembic leen `settings.database_url`, que no arranca si el host no es
el interno de Render (`dpg-...`, sin dominio) y la URL no pide `ssl=require`,
`verify-ca` o `verify-full`. Es la misma regla que ya tiene Redis.

### Los videos en Supabase Storage

Desde el 2026-10-16 el video espera al worker en el bucket **privado**
`pose-videos` del proyecto de Supabase (`STORAGE_BACKEND=supabase`,
`app/platforms/render/supabase_storage.py`, el mismo cliente que se usó entre
el 2026-10-07 y el 2026-10-08, etiqueta `render-supabase-v1`). La API lo sube,
el worker lo baja y lo borra al cerrar el job, y la retención borra lo que
quede.

**Configuración** (en `deploy/render/render.yaml`):

- En el grupo `pose-config`: `STORAGE_BACKEND=supabase`,
  `SUPABASE_URL=https://eytjjlcthcghftlwxgab.supabase.co` (la URL del proyecto
  no es secreta) y `SUPABASE_STORAGE_BUCKET=pose-videos`.
- En pose-api y pose-worker: `SUPABASE_SECRET_KEY` con `sync: false` (ver "Lo
  que el blueprint NO puede hacer"). La API y el worker no arrancan sin ella:
  así no aceptan un video que después no podrían guardar.

**Por qué se dejó la base.** El 2026-10-16, en la prueba de punta a punta con
la plataforma de telefisioterapia, cada subida de un video de celular tumbó
`pose-db` (basic-256mb):

- los logs de la base mostraban `database system was not properly shut down`
  justo en cada subida: se quedaba sin memoria al guardar el video como bytea;
- sin base, `/health` de pose-api daba 503 y Render reiniciaba la API, que era
  solo una consecuencia.

Agrandar la base solo para que pasen videos en tránsito no tenía sentido
cuando el almacenamiento de objetos ya estaba implementado y probado.

**Ley 1581: lo que queda después de borrar.**

- **Los videos ya no quedan en los respaldos de Render.** Con
  `STORAGE_BACKEND=postgres`, un video borrado seguía 3 a 7 días en el WAL de
  pose-db. Ahora no pasan por pose-db: sus respaldos solo tienen los jobs.
- **Los respaldos de Supabase no incluyen los objetos de Storage.** Su
  documentación: *"Database backups do not include objects you store via the
  Storage API, as the database only includes metadata about these objects.
  Restoring an old backup does not restore objects you deleted after that
  backup."* Los metadatos (ruta `videos/<uuid>.<ext>`, tamaño, fechas) sí
  quedan en los respaldos del proyecto, pero no identifican al paciente.
- **Supabase no documenta conservar un objeto borrado** (ni papelera ni
  versiones). Verificado en su documentación el 2026-10-16. No es una garantía
  contractual: es lo que hay que confirmar si la política de tratamiento lo
  exige.
- **A cambio, el video viaja por internet** (cifrado, HTTPS) a un proveedor
  más, que trata datos de salud. Con la base de Render iba por la red privada.
  Hay que tenerlo presente para el aval del tratamiento de datos.

**Límites del plan gratuito de Supabase**, que condicionan el servicio:

| Límite | Valor | Efecto |
|---|---|---|
| Tamaño por archivo | 50 MB | `MAX_VIDEO_SIZE_MB` no puede pasar de 50. Los videos de TOMA FRONTAL pesan 28–43 MB (11–17 s): 50 MB son ~20 s con esa calidad. |
| Almacenamiento | 1 GB | Alcanza: el worker borra cada video al terminar, así que solo se guardan los que están en cola. |
| Egreso | 5 GB/mes | Cada video se descarga una vez (el worker): con ~40 MB por video son ~125 videos al mes. |

**Probarlo** desde una máquina del equipo, con un archivo de prueba sin datos
de nadie (lo sube, lo baja, lo compara y lo borra):

```powershell
cd "<carpeta del servicio>"
venv\Scripts\Activate.ps1
$env:STORAGE_BACKEND = "supabase"
$env:SUPABASE_URL = "https://eytjjlcthcghftlwxgab.supabase.co"
$env:SUPABASE_STORAGE_BUCKET = "pose-videos"
$env:SUPABASE_SECRET_KEY = "sb_secret_..."   # nunca en el repositorio
python -m app.tools.storage_check
```

### Los videos en Render Postgres (2026-10-08 a 2026-10-16)

Queda como referencia: `postgres_storage.py` sigue en el código y se elige con
`STORAGE_BACKEND=postgres`. **No con `pose-db` en basic-256mb**: ver arriba.


El video esperaba al worker en la tabla `videos` de `pose-db`, la misma base
de los jobs. La API lo guardaba, el worker lo leía y lo borraba al cerrar el
job.

**Por qué en la base.** Era la única opción que dejaba todo en Render sin
cambiar la arquitectura:

| Opción | Problema |
|---|---|
| Object Storage de Render | En *early access* (su cliente de Python está en un módulo `experimental`) y se autentica con una clave de API de toda la cuenta. No para videos de pacientes. |
| Disco persistente | Lo ve un solo servicio: la API y el worker no podrían compartirlo. Un servicio con disco tampoco escala ni se despliega sin cortarse. |
| Key Value (Redis) | Todo en memoria, en un plan de 256 MB. |
| **La base** | Ya la comparten la API y el worker por la red privada. |

**Por qué parecía funcionar**, aunque en general no se guardan archivos en una
base (las mediciones de abajo fueron en un PostgreSQL local con bytes
aleatorios, no en el plan basic-256mb):
cada video pesa como mucho `MAX_VIDEO_SIZE_MB`, espera alrededor de un minuto y
se borra apenas el worker cierra el job. La tabla nunca guarda más que los
videos en cola. Medido el 2026-10-08 contra PostgreSQL 16 con 50 MB de bytes
aleatorios: se guardan en ~1 s, se leen en ~0,25 s idénticos, y el espacio de
uno borrado lo libera el autovacuum en unos 15 s. La columna del contenido va
sin comprimir (`STORAGE EXTERNAL`, migración `0005_videos_table`): un video ya
viene comprimido.

**Ley 1581: lo que queda después de borrar.**

- **Para el servicio, el video desaparece al instante**: nada puede volver a
  leerlo.
- **En el disco de la base**, en menos de un minuto (autovacuum).
- **En los respaldos de Render, no.** PostgreSQL anota cada escritura en su
  registro de cambios (WAL), y Render lo guarda para poder restaurar la base a
  cualquier momento: **3 días con cuenta Hobby, 7 con Pro**. Mientras dure ese
  plazo, el video se puede reconstruir restaurando la base. Las copias lógicas
  (7 días) solo lo tienen si se tomaron justo mientras esperaba. Todo está
  cifrado (AES-256) y hace falta entrar a la cuenta de Render. Verificado el
  2026-10-08: después de borrar y limpiar, los bytes ya no estaban en la
  tabla, pero sí en el WAL.

Es un plazo acotado y con el mismo proveedor, pero es un dato de salud
conservado más allá de su finalidad: **tiene que quedar escrito en la política
de tratamiento y aprobarlo quien responde por los datos.** La cuenta Hobby lo
deja en 3 días en vez de 7.

**El disco.** 5 GB (1,50 USD/mes) alcanzan para unos 100 videos de 50 MB
esperando a la vez. Si el worker se cae, la API sigue recibiendo y la tabla
crece; con el disco lleno, la base deja de aceptar escrituras y se cae todo el
servicio. Por eso `storageAutoscalingEnabled`: Render agranda el disco antes.
El disco se agranda y no se achica.

**Sin acceso desde internet** (`ipAllowList: []`): solo los servicios, por la
red privada. Para entrar con `psql` o `pg_dump` desde afuera, se agrega la IP
propia en el Dashboard mientras haga falta.

**Probarlo** desde una máquina del equipo, contra una base con las
migraciones corridas (sube un archivo de prueba, lo baja, lo compara y lo
borra):

```powershell
$env:DATABASE_URL = "postgresql://..."   # la externa de pose-db, con la IP propia permitida
python -m app.tools.storage_check
```

### Si la base vuelve a Supabase

Hasta el 2026-10-08 la base y los videos estuvieron en un proyecto de Supabase
(etiqueta `render-supabase-v1`). Lo que sigue queda como referencia por si se
vuelve. El proyecto se prepara así, antes de crear el blueprint:

1. **Un proyecto propio, no tablas dentro del proyecto de la plataforma.** Si
   compartieran proyecto, la configuración de la plataforma (su API REST, sus
   roles) alcanzaría a la tabla `jobs`, que tiene datos de salud.
2. **Región "West US (Oregon)".** El servicio está en Render Oregon, que corre
   sobre AWS en esa misma región (lo verificó pose-benchmark el 2026-10-01).
3. **Desactivar la Data API** (Project Settings > Data API). Por defecto,
   Supabase publica por REST las tablas del esquema `public` y les da permisos
   de lectura y escritura al rol anónimo, cuya clave está pensada para ir en
   un navegador. Este servicio no usa esa API.
4. **Copiar la URL del "Session pooler"** (Connect > Session pooler:
   `aws-0-<región>.pooler.supabase.com:5432`) y agregarle `?sslmode=require`.
   Es la que se pega en Render.
   - **No la conexión directa:** es solo IPv6, y Render no sale por IPv6.
   - **No el puerto 6543** (modo "Transaction"): no admite los *prepared
     statements* que usa asyncpg.

**Segunda barrera, por si la Data API vuelve a activarse:** la migración
`0004_row_level_security` activa RLS en `jobs` y en `alembic_version`, sin
políticas. Los roles de la API REST no ven ni tocan nada, y el servicio no se
entera porque se conecta como dueño de las tablas. Se verificó el 2026-10-06
contra PostgreSQL 16 con un rol `anon` con los permisos que da Supabase:

- con RLS, `anon` veía 0 filas y no podía modificar ni borrar ninguna;
- el dueño seguía viéndolas todas;
- al revertir la migración, `anon` volvía a verlas.

**Conexiones: hay un techo de 15.** El Session pooler del plan gratuito admite
15 conexiones en total (el "Pool Size" del proyecto). Pasado eso, Supabase
rechaza con *"max clients reached in session mode"*. Por eso `deploy/render/render.yaml`
fija `DB_POOL_SIZE=2` y `DB_MAX_OVERFLOW=1`: hasta 3 por proceso. En el peor
momento de un despliegue suman 13, contando la API vieja y la nueva, los dos
procesos del worker y el alembic del pre-despliegue. Un test comprueba la
suma: si se sube la concurrencia del worker, ese test avisa.

**Lo que hay que saber del plan gratuito:**

- **Pausa:** Supabase pausa los proyectos gratuitos tras 7 días de
  inactividad. El despachador de reintentos consulta la base cada 10 s, pero
  no está verificado que Supabase cuente eso como actividad.
- **Respaldos:** los diarios (7 días) son del plan Pro.
- **Límite de proyectos:** 2 por organización.
- **Si la organización ya está en Pro** (la de la plataforma, por ejemplo), un
  proyecto más cuesta unos 10 USD/mes por la instancia Micro: el crédito de
  10 USD que incluye Pro cubre un solo proyecto.

**Ley 1581:** Supabase es un proveedor más que trata datos de salud, y los
datos viajan por internet, cifrados. Con la base de Render iban por la red
privada. Hay que tenerlo presente para el aval del tratamiento de datos.

### Otros almacenamientos

`video_storage()` de `app/platforms/render/` elige entre `postgres_storage.py`,
`supabase_storage.py` y `firebase_storage.py`. Los tres cumplen el mismo
contrato que cualquier almacenamiento (ver "Plataformas de despliegue"), y el
resto del servicio llega a ellos por `app/utils/video_storage.py`.

- **Firebase** es poner `STORAGE_BACKEND=firebase`, agregar
  `FIREBASE_STORAGE_BUCKET`, subir el Secret File `firebase-credentials.json`
  a pose-api y pose-worker, y devolver `MAX_VIDEO_SIZE_MB` a 200.
- **Un bucket de Supabase nuevo** se prepara así: Storage > New bucket,
  **privado** (Public bucket apagado), *file size limit* de 50 MB y *allowed
  MIME types* `video/*`. La **secret key** (`sb_secret_...`, Project Settings >
  API Keys) opera como el rol `service_role`, que se salta RLS: va solo en
  Render y en tu terminal, nunca en el repositorio.

### Validar el blueprint antes de subirlo

Render publica un JSON Schema oficial. Un blueprint mal escrito se descubriría
recién al crearlo en el Dashboard, así que la suite lo valida en cada PR
(`tests/test_render_blueprint.py`) contra una copia guardada en
`deploy/render/render.schema.json`. Se guarda en el repositorio para que el
test no dependa de la red. Para refrescarla:

```powershell
curl -sL https://render.com/schema/render.yaml.json -o deploy/render/render.schema.json
```

La suite también comprueba que los comandos de Celery del blueprint apunten a
un módulo que existe: si el worker se mueve, el blueprint no queda apuntando
al lugar viejo.

**Al crear el Blueprint en Render**, el campo *Blueprint Path* va en
`deploy/render/render.yaml` (desde el 2026-10-07 ya no está en la raíz).

## Despliegue en Google Cloud

> **El 2026-10-08 el grupo de investigación decidió quedarse en Render** (ver
> "Despliegue en Render"). El código de Google sigue en el repositorio y se
> prueba en cada PR, pero no se despliega.

El grupo de investigación quería probar el servicio en Google Cloud con los
créditos de prueba (300 USD por 90 días). Se construyó por etapas, cada una
con sus tests y su PR:

| Etapa | Qué | Estado |
|---|---|---|
| 0 | Separar lo común de la plataforma (`app/platforms/`) | Hecha |
| 1 | Almacenamiento del video: Cloud Storage | Hecha |
| 2 | Base: Cloud SQL | Hecha |
| 3 | Cola: Cloud Tasks hacia el worker en Cloud Run | Hecha |
| 4 | Tareas periódicas: Cloud Scheduler | Hecha |
| 5 | Imagen, `deploy/gcp/` y CI hacia Artifact Registry y Cloud Run | Pendiente |

El código de Google está completo desde la etapa 4, y `PLATFORM=gcp` se acepta
en producción. Lo que falta (etapa 5) es infraestructura: publicar la imagen en
Artifact Registry, desplegar los servicios de Cloud Run y el CI. Todo se prueba
en cada PR contra emuladores, y el camino completo corre en local (ver "El
camino de Google en local"). Contra el servicio real, por ahora, solo el bucket
(`storage_check`, abajo).

### El almacenamiento en Cloud Storage

`app/platforms/gcp/cloud_storage.py`, con `google-cloud-storage`
(`requirements/gcp.txt`). Se configura con una sola variable, `GCS_BUCKET`.

**No hay credenciales en el código ni en variables.** En Cloud Run el servicio
corre con una cuenta de servicio y la librería la toma sola. Esa cuenta
necesita sobre el bucket el rol `roles/storage.objectUser` (crear, leer y
borrar objetos) y nada más.

**Preparar el bucket** (cuando exista el proyecto, con `gcloud` instalado):

```powershell
$PROYECTO = "<id del proyecto>"
$BUCKET = "$PROYECTO-videos"

gcloud storage buckets create gs://$BUCKET --project=$PROYECTO `
    --location=us-central1 --uniform-bucket-level-access `
    --public-access-prevention --soft-delete-duration=0

gcloud storage buckets update gs://$BUCKET `
    --lifecycle-file=deploy/gcp/storage-lifecycle.json
```

Por qué cada opción:

- **`us-central1`**: la capa gratuita de Cloud Storage (5 GB) solo aplica en
  regiones de EE. UU. Cloud Run tiene que ir en la misma región: así subir y
  bajar el video no cruza regiones, que es lo que se cobra como egreso.
- **Acceso uniforme y prevención de acceso público**: el bucket no puede
  quedar público por un permiso puesto a mano en un objeto.
- **`--soft-delete-duration=0`**: por defecto Cloud Storage retiene 7 días lo
  que se borra (borrado lógico). Con videos de pacientes eso contradice el
  principio de temporalidad de la Ley 1581: el video seguiría recuperable una
  semana después de que el worker lo borró. Además se cobra. En un bucket ya
  creado se desactiva con `gcloud storage buckets update gs://$BUCKET
  --clear-soft-delete`.
- **Regla de ciclo de vida** (`deploy/gcp/storage-lifecycle.json`): borra
  todo video con más de un día. Es la red de seguridad por si el worker y el
  barrido de retención fallaran los dos. Un test comprueba que nunca borre un
  video que un job vivo todavía necesita (un día supera de sobra los umbrales
  de jobs atascados) y que no conserve más que `DATA_RETENTION_DAYS`.

**Probarlo desde una máquina del equipo** (sube un archivo sin datos de
nadie, lo baja, lo compara y lo borra):

```powershell
gcloud auth application-default login
$env:GCS_BUCKET = "<bucket>"
python -m app.tools.storage_check --plataforma gcp
```

**En la suite**, `tests/test_gcp_storage.py` sube, baja y borra de verdad
contra el emulador de Cloud Storage (`fake-gcs-server`, el mismo de la demo),
incluida la subida reanudable que usa la librería desde 8 MB. El workflow de
tests levanta el emulador, así que corren en cada PR. En local:

```powershell
docker run -d --rm -p 127.0.0.1:4443:4443 fsouza/fake-gcs-server:1 `
    -scheme http -port 4443 -backend memory -external-url http://localhost:4443
$env:GCS_EMULATOR_FOR_TESTS = "http://localhost:4443"
pytest tests/test_gcp_storage.py
```

Sin esa variable, esos tests se saltan y el resto corre igual.

### La base en Cloud SQL

Cloud SQL para PostgreSQL, en la instancia más chica (`db-f1-micro`). El
esquema es el mismo de siempre: lo crea `alembic upgrade head`.

**Cómo se conecta:** Cloud Run monta la instancia como un **socket Unix** en
`/cloudsql/<proyecto>:<región>:<instancia>`. Por ese socket la atiende el proxy
de Cloud SQL, que cifra con TLS el tramo hasta la base y autoriza con la cuenta
de servicio, que necesita el rol `roles/cloudsql.client`. La URL queda así:

```
postgresql+asyncpg://pose:<clave>@/pose_service?host=/cloudsql/<proyecto>:us-central1:pose-db
```

Sin `sslmode`: la regla de TLS en producción acepta un socket Unix sin pedirlo,
porque no sale de la máquina (ver `database_url` en `app/core/config.py`). Esa
regla es común, no de Google. En cambio, cualquier URL con un host de red, una
IP interna de Google incluida, tiene que pedir TLS: el servicio no usa VPC.

**Crear la instancia** (cuando exista el proyecto):

```powershell
$PROYECTO = "<id del proyecto>"

gcloud sql instances create pose-db --project=$PROYECTO `
    --database-version=POSTGRES_16 --edition=enterprise --tier=db-f1-micro `
    --region=us-central1 --ssl-mode=ENCRYPTED_ONLY --deletion-protection

gcloud sql databases create pose_service --instance=pose-db --project=$PROYECTO

# Clave aleatoria de solo letras y números: va dentro de una URL, y así no
# hay que escapar nada.
$bytes = New-Object byte[] 24
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$CLAVE = [Convert]::ToBase64String($bytes) -replace '[+/=]', ''
gcloud sql users create pose --instance=pose-db --project=$PROYECTO --password=$CLAVE

# El nombre de conexión, para la URL:
gcloud sql instances describe pose-db --project=$PROYECTO --format="value(connectionName)"
```

Por qué cada opción:

- **`--edition=enterprise`**: en PostgreSQL 16 la edición por defecto es
  Enterprise Plus, que no admite las instancias de núcleo compartido como
  `db-f1-micro`.
- **`us-central1`**: la misma región que el bucket y que Cloud Run.
- **`--ssl-mode=ENCRYPTED_ONLY`**: la instancia rechaza cualquier conexión sin
  cifrar, venga de donde venga.
- **`--deletion-protection`**: borrar la instancia exige quitar esto antes. Es
  donde están los resultados.

La clave va a Secret Manager junto con la URL, cuando se arme el despliegue
(etapa 5). No se escribe en el repositorio ni en el chat.

**Probarla desde una máquina del equipo**, antes de que exista Cloud Run, con
el [proxy de Cloud SQL](https://cloud.google.com/sql/docs/postgres/sql-proxy)
(el mismo que usa Cloud Run por dentro):

```powershell
gcloud auth application-default login
.\cloud-sql-proxy.exe "<proyecto>:us-central1:pose-db" --port 5434

# En otra terminal, con el venv activo y ENVIRONMENT=development:
$env:DATABASE_URL = "postgresql+asyncpg://pose:<clave>@127.0.0.1:5434/pose_service"
alembic upgrade head
```

**Conexiones:** `db-f1-micro` admite 25 conexiones en total, y como con
Supabase, el techo es la suma de todos los procesos (cada instancia de Cloud
Run, cada proceso del worker, las migraciones). El tamaño de los pools se fija
con el de Cloud Run, en la etapa 5.

**Costo:** unos 8 USD/mes la instancia más unos 2 USD de disco (10 GB SSD).
Cloud SQL no tiene capa gratuita; lo cubre el crédito. Se cobra mientras esté
encendida, aunque no llegue ningún video. Entre rondas de pruebas se puede
apagar y solo queda el disco:

```powershell
gcloud sql instances patch pose-db --project=$PROYECTO --activation-policy=NEVER   # apagar
gcloud sql instances patch pose-db --project=$PROYECTO --activation-policy=ALWAYS  # encender
```

**Ley 1581:** igual que con Supabase, los datos quedan en un proveedor externo
y fuera de Colombia (us-central1), cifrados en reposo. Hay que tenerlo presente
para el aval del tratamiento de datos.

**En la suite**, `tests/test_gcp_database.py` corre las migraciones y lee y
escribe jobs, en modo producción, contra un PostgreSQL real que escucha en un
socket con la misma forma de ruta, dos puntos incluidos. El workflow de tests
lo levanta. asyncio no tiene sockets Unix en Windows, así que en local corre
dentro de Linux:

```powershell
docker volume create cloudsql-pruebas
docker run -d --rm --name cloudsql-pruebas -e POSTGRES_PASSWORD=postgres `
    -v cloudsql-pruebas:/cloudsql --entrypoint sh postgres:16-alpine -c `
    "mkdir -p '/cloudsql/proyecto-pruebas:us-central1:pose-db' && chown postgres '/cloudsql/proyecto-pruebas:us-central1:pose-db' && exec docker-entrypoint.sh postgres -c 'unix_socket_directories=/var/run/postgresql,/cloudsql/proyecto-pruebas:us-central1:pose-db'"
docker build --target render -t pose-service:pruebas .
docker run --rm --user root -e CLOUDSQL_SOCKET_DIR_FOR_TESTS=/cloudsql `
    -v cloudsql-pruebas:/cloudsql -v "${PWD}:/src:ro" -w /src pose-service:pruebas `
    sh -c "pip install -q pytest==9.1.1 pytest-asyncio==1.4.0 && python -m pytest -p no:cacheprovider tests/test_gcp_database.py"
```

### La cola en Cloud Tasks

En Google no hay Celery, Redis ni Beat. La cola es **Cloud Tasks**, que le
entrega cada tarea al worker por HTTP:

```
API (Cloud Run, pública) ── crea la tarea ──▶ Cloud Tasks ── POST + token OIDC ──▶ worker (Cloud Run, privado)
                                              pose-videos     /tasks/process-video
                                              pose-callbacks  /tasks/send-callback
```

- **Dos servicios de Cloud Run con la misma imagen** (etapa `gcp` del
  Dockerfile). La API (`uvicorn app.main:app`) es pública porque la llama la
  plataforma, con HMAC. El worker (`uvicorn app.platforms.gcp.worker:app`) es
  privado, y cada endpoint suyo llama a la misma función de la parte común que
  una tarea de Celery en Render.
- **Dos colas**: `pose-videos` y `pose-callbacks`. Así un aviso no espera
  detrás de videos de varios minutos, y cada cola tiene sus propios límites.
- **Solo Cloud Tasks puede darle trabajo al worker.** Primera barrera: el
  worker se despliega sin acceso público, y solo la cuenta de servicio de las
  tareas puede invocarlo. Segunda: el worker verifica el token OIDC de cada
  tarea (`app/platforms/gcp/task_auth.py`): firmado por Google, emitido para la
  URL del worker y de esa cuenta. Sin un token válido responde 401 y no ejecuta
  nada. Desplegar el worker público por error es un solo flag; la segunda
  barrera es para eso.
- **Reintentos**: si el worker no responde 2xx a tiempo, o muere a mitad de un
  video, Cloud Tasks reintenta. Es lo mismo que `task_acks_late` en Render, y
  la parte común ya descarta la re-entrega de un job cerrado. Un video que
  falló responde 200: su desenlace ya quedó en la base. Cada entrega queda en
  el log (`task_received`) con su número de reintento.
- **Un video no se encola dos veces**: la tarea se llama `video-<job_id>`, y
  Cloud Tasks rechaza crear dos con el mismo nombre.
- **Plazo de 30 minutos por video**, el máximo de Cloud Tasks. Tiene que
  superar al barrido de jobs atascados: si fuera menor, Cloud Tasks
  reintentaría un video que el worker todavía procesa. Un test protege esa
  relación.
- **Un video por instancia** (concurrencia 1 en Cloud Run): MediaPipe bloquea
  el event loop mientras analiza. El paralelismo viene de tener varias
  instancias, y lo limita la cola: `--max-concurrent-dispatches` de
  `pose-videos` es cuántos videos se procesan a la vez, o sea cuántas
  instancias grandes pueden estar encendidas. Es la palanca principal de costo.
- **Solo se paga mientras se procesa**: Cloud Run cobra la CPU durante las
  solicitudes y escala a cero cuando no llegan videos. Es la diferencia de
  fondo con Render, donde el worker se cobra encendido aunque no llegue nada.

**Crear las colas** (cuando exista el proyecto; los servicios de Cloud Run se
despliegan en la etapa 5):

```powershell
gcloud services enable cloudtasks.googleapis.com --project=$PROYECTO

gcloud tasks queues create pose-videos --project=$PROYECTO --location=us-central1 `
    --max-concurrent-dispatches=2 --max-attempts=3 --min-backoff=30s --max-backoff=300s

gcloud tasks queues create pose-callbacks --project=$PROYECTO --location=us-central1 `
    --max-concurrent-dispatches=5 --max-attempts=3 --min-backoff=10s
```

Con `--max-attempts=3`, un video que tumba al worker tres veces (por ejemplo,
por falta de memoria) deja de reintentarse. El job queda en `processing`, y el
barrido de jobs atascados lo cierra y le avisa a la plataforma.

**Cuentas de servicio** (se terminan de armar en la etapa 5):

- `pose-tasks`: la que firma los tokens de las tareas (`TASKS_SERVICE_ACCOUNT`).
  Necesita `roles/run.invoker` sobre el worker y nada más.
- La de la API necesita `roles/cloudtasks.enqueuer` para crear tareas, y
  `roles/iam.serviceAccountUser` sobre `pose-tasks` para firmarlas con ella.

Variables de la cola: `GCP_PROJECT`, `GCP_REGION`, `WORKER_URL` (https) y
`TASKS_SERVICE_ACCOUNT`. En producción son obligatorias, y el servicio no
arranca si falta alguna.

**En la suite**, `tests/test_gcp_tasks.py` encola de verdad contra el emulador
de Cloud Tasks (`aertje/cloud-tasks-emulator`, la misma API gRPC). El
emulador le entrega la tarea por HTTP a un worker real, que verifica el token
que firmó el emulador. El workflow de tests lo levanta; para correrlo en local,
ver el docstring de ese archivo.

### Las tareas periódicas en Cloud Scheduler

Son las mismas cinco que Celery Beat dispara en Render, con los mismos nombres
y la misma frecuencia: el barrido de jobs atascados, el resumen de estado, la
retención de datos, el despachador de reintentos de avisos y el rescate de
avisos perdidos. La tabla está en `app/platforms/gcp/schedule.py`. Cloud
Scheduler llama a `POST /cron/<nombre>` del worker, firmado con la misma cuenta
y la misma audiencia que Cloud Tasks, así que el worker lo verifica igual.

Los tests comparan las dos plataformas. Las dos tablas tienen que tener las
mismas tareas, con la misma frecuencia, y cada tarea tiene que llamar a la
misma función común que en Render. Esa función se averigua corriendo la tarea
de Celery. Agregar una tarea periódica en una plataforma y olvidarla en la
otra rompe la suite.

**La única diferencia: Cloud Scheduler no baja de un minuto.** El despachador
de reintentos, que en Render corre cada 10 s, acá corre cada minuto. Los dos
primeros reintentos de un aviso, a los 5 y a los 30 s, se hacen hasta un minuto
después de su hora; desde el tercero, a los 2 minutos, ya no se nota.

Se aceptó así porque la alternativa, un despachador que se quede despierto el
minuto entero, es pagar una instancia siempre encendida. Es justo lo que se
evita en Google. Y para la plataforma, recibir un resultado un minuto más tarde
después de una caída breve no cambia nada.

**Crear los trabajos** (cuando estén desplegados los servicios, en la etapa 5).
Los comandos se generan desde la tabla, para que no se desalineen del código:

```powershell
python -m app.tools.gcp_scheduler --proyecto $PROYECTO `
    --worker-url https://pose-worker-<...>.run.app `
    --cuenta pose-tasks@$PROYECTO.iam.gserviceaccount.com
```

Solo imprime los comandos de `gcloud`, no ejecuta nada. Cada trabajo va con
zona horaria de Bogotá: la retención corre a las 3 a. m. Ninguno se reintenta
(`--max-retry-attempts=0`), porque la corrida siguiente retoma lo que haya
quedado pendiente en la base.

**Costo:** Cloud Scheduler da 3 trabajos gratis por cuenta de facturación y
cobra 0,10 USD/mes por cada uno de más. Con cinco son 0,20 USD/mes. Cada
llamada despierta el worker unos milisegundos, sin cargar MediaPipe (se importa
recién con el primer video de cada instancia). El despachador corre cada
minuto, y aun así entra holgado en la capa gratuita de Cloud Run.

### El camino de Google en local

`docker-compose.gcp.yml` corre la etapa `gcp` de la imagen con todo lo que
existe de Google hasta ahora, con emuladores. Es el equivalente de la demo de
Render:

- la API y el worker como dos servicios, igual que en Cloud Run;
- PostgreSQL detrás de un socket con la misma ruta que Cloud SQL;
- los emuladores de Cloud Storage y de Cloud Tasks, que firma los tokens con
  su propia clave;
- `programador`, que hace de Cloud Scheduler, que no tiene emulador. Cuando
  vence una tarea periódica, la crea en el emulador de Cloud Tasks, que se la
  entrega al worker firmada (`app/tools/local_scheduler.py`);
- el receptor, que hace de plataforma.

```powershell
docker compose -f docker-compose.gcp.yml up --build
# En otra terminal, con el venv activo y un video SIN datos de pacientes:
python -m app.tools.demo_send <video.mp4>
# Al terminar (borra todo, también el volumen del socket):
docker compose -f docker-compose.gcp.yml down -v
```

Verificado el 2026-10-08 con un video sintético: subida a Cloud Storage, tarea
en `pose-videos`, worker con el token verificado, descarga, análisis y borrado
del video, aviso por `pose-callbacks`, y entrega al receptor al primer intento.
El worker respondió 401 a una solicitud sin token y a una con un token
fabricado.

Las tareas periódicas también, con la plataforma caída a propósito
(`POST http://localhost:9099/control/caida?activa=true`):

1. El primer aviso falló con 503 y quedó en `retrying`.
2. Se levantó la plataforma.
3. El despachador lo tomó en su corrida del minuto y lo entregó al segundo
   intento.

El aviso llegó 36 s después de su hora prevista, que es justo el efecto del
piso de un minuto.

## Migraciones (Alembic)

**El esquema lo maneja Alembic. No se usa `create_all` ni se corren `ALTER
TABLE` a mano.** `Database.connect()` ya no crea tablas: si lo hiciera, las
crearía sin registrar nada en `alembic_version` y la siguiente migración
correría contra una base que Alembic cree vacía.

Alembic toma la conexión de `settings.DATABASE_URL` (ver `alembic/env.py`), la
misma que usa el servicio, así que no hay una segunda credencial que mantener
y `alembic.ini` no lleva la URL.

Flujo esperado ante **cualquier** cambio a los modelos de SQLAlchemy en
`app/utils/db_connect.py`:

```powershell
# 1. Cambiar el modelo (agregar columna, índice, tabla...)
# 2. Generar la migración comparando modelos contra la base
alembic revision --autogenerate -m "descripcion del cambio"
# 3. REVISAR el archivo generado en alembic/versions/ antes de aplicarlo
# 4. Aplicar
alembic upgrade head
```

El paso 3 no es opcional: autogenerate detecta bien columnas y tablas, pero se
le escapan cambios de tipo sutiles y renombres (ve un renombre como "borrar una
columna y crear otra", que destruye los datos). El commit debe incluir el
cambio al modelo **y** su migración juntos.

Comandos útiles:

```powershell
alembic current              # en qué revisión está la base
alembic history --verbose    # historial de migraciones
alembic downgrade -1         # deshacer la última
alembic upgrade head --sql   # ver el SQL sin aplicarlo
```

## Detalle importante: Celery y async/await

Celery no soporta `async/await` nativamente, pero la tarea necesita código
async (SQLAlchemy con asyncpg, httpx para el webhook). La solución está en
`app/platforms/render/worker.py`: **un event loop persistente por proceso worker**, creado
en la señal `worker_process_init` y reutilizado por todas las tareas.

No se usa `asyncio.run()` por tarea: eso crea y cierra un loop nuevo cada vez,
lo que invalida las conexiones de asyncpg (quedan atadas a un loop cerrado) y
bajo concurrencia produce `RuntimeError: event loop is already running`.

## Contrato con la plataforma

`POST /video/` (multipart):

| Campo | Tipo | Descripción |
|---|---|---|
| `video` | archivo | El video a analizar |
| `points` | JSON | v1: 3 índices de landmark de MediaPipe, ej. `[24, 26, 28]`. v2: lista de 1 a 12 ternas, ej. `[[23, 25, 27], [24, 26, 28]]` |
| `exercise` | string | Identificador del ejercicio |
| `resultsEndpoint` | URL | A dónde llamar de vuelta al terminar |

Headers: `X-Timestamp` y `X-Signature` (ver `app/utils/hmac_auth.py`).

Respuesta inmediata: `{status, internal_status_code, job_id}`.

Callback a `resultsEndpoint` al terminar, **v1** (una terna):
`{error, results: {points}, max_angle, min_angle}`.

### Contrato v2: varias articulaciones en una pasada

Una solicitud con **lista de ternas** es un solo job: MediaPipe corre **una
vez** por video y calcula todas las ternas en esa pasada (antes la plataforma
mandaba un job por articulación, y el mismo video se procesaba N veces). Cada
terna cumple las reglas de v1 (3 enteros distintos en 0–32, sin booleanos), son
entre 1 y 12 y no se repiten; si no, `invalid_points`, como siempre.

Callback **v2** (firmado igual que v1):

```json
{
  "error": null,
  "contract_version": 2,
  "max_angle": null,
  "min_angle": null,
  "results": {
    "points": [],
    "fps": 30.0,
    "duration_s": 10.4,
    "joints": [
      {"points": [23, 25, 27], "vertex": 25, "max_angle": 179.9, "min_angle": 23.7, "valid_frames": 301}
    ],
    "frames": [
      {"t": 0.0333,
       "landmarks": [[0.51, 0.32, -0.12, 0.99], "... 33"],
       "world_landmarks": [[0.02, -0.41, 0.05], "... 33"],
       "angles_2d": [92.4],
       "angles_3d": [95.0]}
    ]
  }
}
```

- `landmarks`: normalizados 0–1, 4 decimales. `world_landmarks`: metros.
- `angles_2d` / `angles_3d`: uno por terna, en el orden de `joints`; `null` si
  ese frame no tuvo pose. Frames sin pose: `landmarks` y `world_landmarks` en
  `null`.
- **El 2D es la medición clínica**: la misma `_angle_deg` sobre píxeles que v1,
  sin modificar. **El 3D es visualización**, no está validado contra gold
  standard.
- Primer nivel `max_angle`/`min_angle` en `null` y `results.points` vacío: los
  valores están por articulación en `joints`.
- Error: `error` lleno y `joints`/`frames` vacíos.

**v1 sigue vigente.** Una terna sola recibe exactamente el callback de siempre.
La recepción marca `contract_version: 2` en el JSONB `results` solo para v2; el
worker y `payload_from_job` eligen la forma por esa marca, y su ausencia (v1 o
filas anteriores) da el comportamiento original. Pruebas: `tests/test_contract_v2.py`.

Tamaño: unos 6 MB de callback para 2 minutos a 30 fps (33 puntos por frame).

### Videos grabados con celular

Un celular que graba en vertical guarda los píxeles "acostados" y un metadato
de rotación. OpenCV lo lee pero no lo aplica: sin corregirlo, el modelo recibe
a la persona girada 90°. Por eso todo video se abre con
`app/utils/video_io.py::open_video`, que activa `CAP_PROP_ORIENTATION_AUTO`.
Un test falla si aparece un `cv2.VideoCapture(` en otro lugar de `app/`.

- **El ángulo casi no cambia.** Es invariante a la rotación, y MediaPipe
  compensa el giro internamente. En `entorno-pruebas` se midió 8,03° → 8,04°
  de error con MediaPipe Heavy; YOLO, en cambio, pasó de 51,6° a 44,1°.
- **Las coordenadas sí cambian.** Las `x`/`y` de cada landmark se calculan con
  el tamaño del frame, así que sin la corrección quedaban en el marco acostado
  y no en el del video que ve el fisioterapeuta.

> Verificado el 2026-09-28, en Windows y en la imagen Linux del servicio
> (cv2 4.11.0): sin la corrección, un video con metadato de 90° se lee acostado
> en los dos sistemas.

### Entrega del callback y reintentos

La entrega va en **su propia tarea de Celery**, no dentro de la que procesa el
video: cuando toca avisar, el resultado ya está calculado y guardado, así que
reintentar debe costar un POST y no volver a correr MediaPipe.

| Respuesta de la plataforma | Qué hace el servicio |
|---|---|
| 2xx | Entregado, fin |
| Timeout, conexión rechazada, 5xx, 429 | Reintenta |
| Cualquier otra que no sea 2xx (3xx, 400, 401, 403, 404, 422) | **No** reintenta |

Son 9 intentos repartidos en ~5,7 horas (5s, 30s, 2min, 8min, 30min, 1h, 2h,
2h) con jitter de ±20%. Si la respuesta trae `Retry-After`, ese valor manda,
acotado a 2 horas — el header lo elige el receptor, y sin tope un
`Retry-After: 604800` dejaría el aviso parado una semana.

Eso es **una ronda**. Si se agota, la siguiente empieza una hora después
(`CALLBACK_RETRY_INTERVAL_MINUTES`), y así hasta completar la ventana de 3 días
(ver abajo).

No se reintentan los 4xx porque significan que la plataforma rechazó el
contenido o la firma: el mismo payload fallaría igual las nueve veces. Acá
pesa que el esquema HMAC todavía no está acordado formalmente (bloqueador #2
de `docs/DECISIONES_TECNICAS.md`), así que un 401 es el 4xx más probable y lo que necesita es
que alguien lo revise, no insistencia. Las guías del tema no coinciden en
esto: [Svix](https://www.svix.com/resources/webhook-best-practices/retries/)
recomienda reintentar todo lo que no sea 2xx;
[Hookdeck](https://hookdeck.com/outpost/guides/outbound-webhook-retry-best-practices)
recomienda lo que hacemos acá. Se eligió lo segundo porque el receptor es uno
solo y conocido.

Cada intento **vuelve a firmar**: la firma lleva timestamp y hay ventana
anti-replay de 5 minutos, así que reusar la firma original haría que todo
reintento posterior a esa ventana fuera rechazado.

### Dónde espera un reintento

En la tabla, en `callback_next_attempt_at`. La tarea de entrega hace **un solo
intento** y, si falla, deja escrita la hora del próximo. Un despachador que
corre cada `CALLBACK_DISPATCH_INTERVAL_SECONDS` (10 s) encola los que ya
vencieron. La cola de Redis solo lleva tareas inmediatas (ver "Ninguna tarea
espera dentro de Redis").

```
pending ──intento──► delivered            (la plataforma respondió 2xx)
   ▲          ├────► rejected             (respondió algo no reintentable)
   │          └────► retrying ──┐         (fallo transitorio: queda la hora)
   │                            │
   └──── despachador ◄──────────┘         (cuando la hora llega)

retrying ──► exhausted   si el próximo intento caería fuera de la ventana
```

- **Rondas.** Dentro de una ronda, las esperas siguen la escalera (5 s, 30 s,
  2 min…). Terminada una ronda de 9 intentos, la siguiente empieza tras
  `CALLBACK_RETRY_INTERVAL_MINUTES`. Cada ronda vuelve a empezar con esperas
  de segundos, así que una plataforma que acaba de volver recibe su aviso
  enseguida.
- **Ventana.** Se insiste hasta `CALLBACK_RETRY_WINDOW_HOURS` (3 días, la
  ventana de Stripe) contadas desde que entró el job. Pasado eso, el aviso
  queda en `exhausted` y aparece en `undelivered_callbacks` del resumen de
  estado. Con la plataforma caída todo el tiempo son unas 10 rondas.
- **Los `rejected` no se reintentan.** La plataforma respondió algo no
  reintentable, típicamente un 4xx: el mismo payload falla igual mañana. El
  caso realista es un 401 por el esquema HMAC, que todavía no está acordado
  (bloqueador #2 de `docs/DECISIONES_TECNICAS.md`). Se arregla del lado de la configuración, y
  después esas filas se recuperan a mano.
- **Rescate de avisos sin tarea.** Un aviso en `pending` debería resolverse en
  segundos. Si lleva más de `CALLBACK_STALE_PENDING_HOURS` (3 h) así, su tarea
  se perdió (el worker murió a mitad del intento, o el broker se vació), y un
  barrido lo devuelve a `retrying`. El umbral supera el `visibility_timeout`
  (1 h) a propósito: antes de eso, Redis re-entrega la tarea por su cuenta, y
  rescatarla también la duplicaría.
- **El payload se arma desde la fila en cada intento.** La tarea solo lleva el
  `job_id`: la tabla es la única fuente de verdad.
- **Un job minimizado por retención queda fuera solo:** al minimizar se borra
  el `results_endpoint`, y el despachador solo mira filas que lo tengan.

Tomar y marcar van en **una sola sentencia** `UPDATE ... RETURNING`: si fueran
un `SELECT` y después un `UPDATE`, dos corridas solapadas encolarían el mismo
aviso dos veces.

La entrega es **al menos una vez**: si un worker muere justo después de que la
plataforma recibió el aviso y antes de escribirlo en la tabla, el aviso se
vuelve a enviar. Es inherente a cualquier webhook, y por eso el receptor
debería tolerar duplicados.

> Verificado el 2026-09-24 contra PostgreSQL y Redis reales, reproduciendo el
> caso que antes fallaba: plataforma caída, dos 503, y el worker **recreado a
> mitad de los reintentos**. Mientras el aviso esperaba, Redis no guardaba
> ninguna tarea (`unacked = 0`); el intento estaba agendado para las 21:12:16
> y se entregó a las 21:12:18. Tres intentos en el receptor y tres en la fila,
> sin duplicados. El rescate también: una fila puesta en `pending` con 4 horas
> de antigüedad volvió al ciclo y se entregó en segundos. La migración 0003 se
> aplicó sobre la base de la demo, que ya tenía datos.

### Qué queda registrado

El desenlace del aviso vive en la tabla, en columnas aparte de `status`. Son
dos dimensiones distintas: un job puede quedar `done` (los ángulos se
calcularon y están guardados) y aun así tener el callback en `exhausted`
porque la plataforma estuvo caída toda la ventana. Mezclarlas en una sola
columna haría que el estado del procesamiento mintiera sobre sí mismo.

| Columna | Qué guarda |
|---|---|
| `callback_status` | `pending` / `retrying` → `delivered` / `rejected` / `exhausted` |
| `callback_attempts` | Cuántos intentos se hicieron, contando todas las rondas |
| `callback_error` | El motivo del último intento fallido |
| `callback_next_attempt_at` | Cuándo toca el próximo intento, si queda alguno |

Va en PostgreSQL y no solo en el log porque los logs de Render son efímeros, y
esto es justo lo que hay que poder consultar días después. Los resultados que
nunca llegaron a la plataforma se encuentran así:

```sql
SELECT id, status, callback_status, callback_attempts, callback_error
FROM jobs
WHERE callback_status IN ('rejected', 'exhausted')
ORDER BY updated_at DESC;
```

Ninguno de esos perdió su resultado: sigue en la tabla y se puede consultar
por `GET /jobs/{job_id}`, que devuelve también estas columnas. Los que el
sistema todavía está intentando están en `retrying`; los de la consulta de
arriba ya no se van a reintentar solos y necesitan que alguien mire.

Ver `docs/DECISIONES_TECNICAS.md` para los supuestos pendientes de confirmar con la plataforma.
