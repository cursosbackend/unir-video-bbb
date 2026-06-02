# Unir Video BBB

Dashboard web para descargar y fusionar grabaciones de **BigBlueButton**. Combina el video de pantalla (`deskshare.webm`) con el audio del webcam (`webcams.webm`) y concatena múltiples sesiones por clase.

## Estructura

```
unir-video-bbb/
├── dashboard/
│   ├── main.py              # FastAPI backend
│   ├── requirements.txt     # Dependencias Python
│   └── templates/
│       └── index.html       # Frontend Bootstrap 5
├── clases/                  # Videos de salida (generado)
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## Requisitos

- **Python 3.10+** y **ffmpeg** (sin Docker)
- O **Docker** + **docker-compose**

## Uso

### Con Docker (recomendado)

```bash
docker-compose up -d
```

Abrir `http://localhost:8000`

Para ver los logs:

```bash
docker-compose logs -f
```

Para detener:

```bash
docker-compose down
```

### Sin Docker

```bash
# Instalar ffmpeg (Debian/Ubuntu)
sudo apt-get install -y ffmpeg

# Instalar dependencias Python
pip install -r dashboard/requirements.txt

# Iniciar servidor
uvicorn dashboard.main:app --host 0.0.0.0 --port 8000
```

Abrir `http://localhost:8000`

## Cómo funciona

Por cada clase se pueden agregar **1 o 2 URLs de reproducción de BBB**.

### Pipeline

```
URL Sesión 1 ──→ deskshare1.webm + webcams1.webm
                    │
                    ├── merge (video pantalla + audio webcam) → session1_merged.webm
                    │
URL Sesión 2 ──→ deskshare2.webm + webcams2.webm   (opcional)
                    │
                    ├── merge (video pantalla + audio webcam) → session2_merged.webm
                    │
                    └── concat ──→ Clase_completa.webm
```

- **Merge**: toma el video de `deskshare.webm` (pantalla, 1280×720) y el audio de `webcams.webm`. No recodifica (copia de streams), es inmediato.
- **Concat**: une las sesiones una detrás de otra sin recodificar si los codecs coinciden.
- **Si solo hay 1 sesión**: se salta el paso de concatenación.

### Formatos soportados

| Archivo | Contenido |
|---------|-----------|
| `deskshare.webm` | Video de pantalla (VP9, 1280×720, ~5 fps) |
| `webcams.webm` | Video de webcam + audio (VP9 + Vorbis estéreo 48 kHz) |
| `Clase_completa.webm` | Video pantalla + audio (salida final) |

### URLs de descarga

El sistema extrae el ID de grabación de la URL de reproducción de BBB y prueba múltiples patrones para descargar los archivos raw:

- `https://servidor/presentation/{ID}/deskshare.webm`
- `https://servidor/playback/presentation/2.0/{ID}/deskshare.webm`
- `https://servidor/playback/presentation/2.3/{ID}/deskshare.webm`
- `https://servidor/playback/presentation/{ID}/deskshare.webm`

## API REST

| Método | Ruta | Descripción |
|--------|------|-------------|
| `GET` | `/` | Interfaz web |
| `GET` | `/api/classes` | Lista todas las clases |
| `POST` | `/api/classes` | Agrega una clase (name, url1, url2 opcional) |
| `GET` | `/api/classes/{id}` | Estado de una clase |
| `DELETE` | `/api/classes/{id}` | Elimina una clase |
| `GET` | `/api/download/{id}` | Descarga el video final |

### Ejemplo API

```bash
curl -X POST http://localhost:8000/api/classes \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Clase 1 - Álgebra",
    "url1": "https://aulavirtual.../playback/presentation/2.3/recording-id-1",
    "url2": "https://aulavirtual.../playback/presentation/2.3/recording-id-2"
  }'
```

## Personalización

### Directorio de salida

Por defecto los videos se guardan en `./clases/`. Se puede cambiar con la variable de entorno:

```bash
export CLASES_DIR=/ruta/personalizada
```

En Docker se configura en `docker-compose.yml`:

```yaml
volumes:
  - ./clases:/app/clases
```

## Notas

- Las URLs deben apuntar a grabaciones de BigBlueButton (formato `playback/presentation/2.3/{id}`)
- Si el servidor requiere autenticación, las descargas fallarán. En ese caso se necesitaría acceso directo a los archivos raw.
- Los archivos intermedios (descargas, merged) se conservan en la carpeta de la clase por si se necesita depurar.
