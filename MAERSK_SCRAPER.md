# Maersk Schedule Scraper

Scraper de schedules de Maersk usando **Python + Playwright** con interceptación de red, almacenados en **MySQL/MariaDB** mediante **SQLAlchemy**.

---

## Arquitectura

```
maersk_scraper/
├── __init__.py        # Paquete
├── __main__.py        # Punto de entrada (python -m maersk_scraper)
├── config.py          # Configuración desde variables de entorno / .env
├── models.py          # Modelos SQLAlchemy (tablas de BD)
├── database.py        # Conexión y gestión de sesiones
├── scraper.py         # Scraper Playwright con interceptación de red
├── parser.py          # Parser de las respuestas JSON de Maersk
└── cli.py             # Interfaz de línea de comandos (Click)
```

### Estrategia de scraping

El sitio de Maersk es una SPA (React). Cuando el usuario busca un schedule, el frontend hace una llamada interna a su API JSON. **En lugar de parsear HTML frágil**, usamos Playwright para:

1. Abrir el navegador y navegar a `maersk.com/schedules/pointToPoint`
2. Registrar un interceptor de respuestas HTTP (`page.on("response", ...)`)
3. Rellenar el formulario de búsqueda (origen, destino, fecha)
4. Capturar la respuesta JSON de la API interna
5. Parsear el JSON → guardar en BD

---

## Instalación

### 1. Requisitos previos

- Python 3.11+
- MySQL 8+ o MariaDB 10.5+
- pip

### 2. Instalar dependencias

```bash
pip install -r requirements-maersk.txt
playwright install chromium
```

### 3. Configurar la base de datos

Crear la base de datos en MySQL/MariaDB:

```sql
CREATE DATABASE maersk_schedules CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'maersk'@'localhost' IDENTIFIED BY 'tu_password';
GRANT ALL PRIVILEGES ON maersk_schedules.* TO 'maersk'@'localhost';
FLUSH PRIVILEGES;
```

### 4. Configurar variables de entorno

```bash
cp .env.example .env
# Editar .env con tus credenciales de BD
```

### 5. Inicializar tablas

```bash
python -m maersk_scraper init-db
```

---

## Uso

### Búsqueda individual

```bash
python -m maersk_scraper search \
  --origin CNSHA \
  --origin-name "Shanghai" \
  --destination NLRTM \
  --destination-name "Rotterdam" \
  --date 2025-04-01
```

Con navegador visible (útil para depuración):

```bash
python -m maersk_scraper search \
  --origin CNSHA --destination NLRTM \
  --date 2025-04-01 \
  --show-browser
```

Sin guardar en BD (sólo mostrar):

```bash
python -m maersk_scraper search \
  --origin CNSHA --destination NLRTM \
  --date 2025-04-01 \
  --no-save
```

### Búsqueda batch (múltiples rutas)

Editar `routes_example.json` con las rutas deseadas, luego:

```bash
python -m maersk_scraper batch --file routes_example.json --date 2025-04-01
```

### Consultar resultados guardados

```bash
# Todos los últimos schedules
python -m maersk_scraper show --limit 20

# Filtrar por ruta
python -m maersk_scraper show --origin CNSHA --destination NLRTM
```

---

## Esquema de base de datos

### `schedule_queries`
Cada búsqueda realizada.

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | INT PK | Auto-incremental |
| `origin_code` | VARCHAR(10) | Código RKST del puerto origen (ej: CNSHA) |
| `origin_name` | VARCHAR(100) | Nombre del puerto origen |
| `origin_country` | VARCHAR(5) | Código ISO del país |
| `destination_code` | VARCHAR(10) | Código RKST del puerto destino |
| `destination_name` | VARCHAR(100) | Nombre del puerto destino |
| `departure_date` | VARCHAR(20) | Fecha de salida solicitada |
| `scraped_at` | DATETIME | Cuándo se hizo la búsqueda |
| `results_count` | INT | Número de schedules encontrados |

### `schedules`
Cada opción de itinerario encontrada para una búsqueda.

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | INT PK | Auto-incremental |
| `query_id` | INT FK | Referencia a `schedule_queries` |
| `transit_time_days` | INT | Tiempo de tránsito total en días |
| `departure_datetime` | DATETIME | Salida del puerto origen |
| `arrival_datetime` | DATETIME | Llegada al puerto destino |
| `num_legs` | INT | Número de tramos/transbordos |
| `has_transshipment` | INT | 1 si tiene transbordos |
| `transshipment_ports` | TEXT | Puertos de transbordo (separados por coma) |
| `schedule_id_external` | VARCHAR(100) | ID interno de Maersk |

### `schedule_legs`
Cada tramo individual de un schedule (un buque entre dos puertos).

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | INT PK | Auto-incremental |
| `schedule_id` | INT FK | Referencia a `schedules` |
| `sequence` | INT | Orden del tramo (1, 2, ...) |
| `vessel_name` | VARCHAR(100) | Nombre del buque |
| `vessel_imo` | VARCHAR(20) | Número IMO del buque |
| `voyage_number` | VARCHAR(50) | Número de viaje (ej: W102E) |
| `service_name` | VARCHAR(100) | Nombre del servicio (ej: AE1/Shogun) |
| `service_code` | VARCHAR(20) | Código del servicio |
| `transport_mode` | VARCHAR(50) | VESSEL, TRUCK, RAIL, FEEDER |
| `from_port_code` | VARCHAR(10) | Código del puerto de salida |
| `from_port_name` | VARCHAR(100) | Nombre del puerto de salida |
| `from_country_code` | VARCHAR(5) | País del puerto de salida |
| `from_terminal` | VARCHAR(100) | Terminal en el puerto de salida |
| `departure_datetime` | DATETIME | Fecha/hora de salida |
| `departure_cutoff` | DATETIME | Fecha límite para la carga |
| `to_port_code` | VARCHAR(10) | Código del puerto de llegada |
| `to_port_name` | VARCHAR(100) | Nombre del puerto de llegada |
| `to_country_code` | VARCHAR(5) | País del puerto de llegada |
| `to_terminal` | VARCHAR(100) | Terminal en el puerto de llegada |
| `arrival_datetime` | DATETIME | Fecha/hora de llegada |
| `leg_transit_days` | FLOAT | Días de tránsito de este tramo |

---

## Consideraciones importantes

### Sobre el scraping de Maersk
- El sitio puede actualizar sus selectores CSS/ARIA sin previo aviso. La interceptación de red es más robusta que el scraping HTML.
- Maersk puede implementar protecciones anti-bot. Si el scraper falla, intenta con `--show-browser` para ver qué ocurre.
- Usar `REQUEST_DELAY_S` para evitar rate limiting.

### Alternativa: API oficial de Maersk
Maersk tiene una [API oficial para schedules](https://developer.maersk.com/api-catalogue) en su portal de desarrolladores. Si tienes acceso (requiere registro y aprobación), es preferible a hacer scraping.

### Código de puertos
Maersk usa códigos RKST (similares a UNLOC) para los puertos. Ejemplos comunes:

| Puerto | Código |
|---|---|
| Shanghai | CNSHA |
| Rotterdam | NLRTM |
| Hamburg | DEHAM |
| Los Angeles | USLAX |
| New York | USNYC |
| Busan | KRBSN |
| Singapore | SGSIN |
| Barcelona | ESBCN |
| Valencia | ESVLC |
| Algeciras | ESALG |
