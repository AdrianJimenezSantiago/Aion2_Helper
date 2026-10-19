# Investigación: Hidden Cubes (cubos rojos con llave) en los mapas de la comunidad

Fecha: 14 de octubre de 2026 (cliente global build 0.0.4387.0, lanzamiento global el 5 de octubre).

## 1. Qué son los "cubos rojos con llave"

Los **Hidden Cubes** son cofres ocultos del mundo abierto. No salen en el mapa ni en el minimapa, emiten
un zumbido al acercarte y se abren con una canalización de 4 s. El primero que lo abre se lo lleva y el
cubo desaparece. Después vuelve a salir en otro de sus spots posibles.

| Mapa (id interno) | Cubos | Spots posibles | Regla de llave |
|---|---:|---:|---|
| Verteron (`World_L_A`) | 95 | 1 440 | **todos necesitan llave** (rojos) |
| Altgard (`World_D_A`) | 59 | 1 371 | **todos necesitan llave** (rojos) |
| Eltnen (`World_L_B`) | 26 | 456 | cubo normal gratis + gemelo que solo abre con llave |
| Morheim (`World_D_B`) | 26 | 455 | igual que Eltnen |
| **Total** | **206** | **3 722** | |

- La llave es el objeto *Key: Hidden Cube* (`it_610900001`, más variantes ligadas o con caducidad:
  `it_630900003`, `it_600900001/2/3`). Se consigue en la tienda de temporada (3 000 Oath Coin, máximo 10
  por cuenta), en misiones (24 en total, varias Duty de nivel 45), con logros, el pase de batalla, los
  Monolitos y las recompensas de asistencia.
- Los cubos **dorados** son los que se abren gratis (Eltnen y Morheim). Los **rojos** necesitan llave y
  tienen mejor tabla de premios: 36 recompensas posibles, desde comunes hasta especiales.
- Cada cubo pertenece a un **grupo de spots**: solo uno está activo a la vez y, al reaparecer, puede
  salir en cualquier spot del grupo. Los grupos tienen 5, 10, 15, 20, 25 o 30 spots, **no** forman
  racimos en el espacio (están repartidos por toda la zona) y 15 de los 206 grupos son submarinos.
- El cliente no tiene temporizador de respawn y no se sabe si el cubo es por servidor o por canal.

## 2. Herramientas de la comunidad y cómo obtienen los datos

| Sitio | Datos | Cómo los obtiene | ¿Se pueden reutilizar? |
|---|---|---|---|
| **aion2.th.gl** (The Hidden Gaming Lair) | 3 722 spots, coordenadas 3D del mundo, más ~19 000 puntos fijos (recolección, NPCs, portales...) | Datamining del cliente. Los marcadores de cada mapa son un fichero estático `cdn.th.gl/aion2/nodes/World_X.<hash>.raw` en CBOR (librería *cbor-x*). Su *Companion App* dibuja tu posición en directo sobre esas mismas coordenadas. | **Sí**: CDN pública, sin login ni token. Pide citar la fuente. |
| **metabot.gg** | 206 cubos, 3 722 spots, **agrupados** por cubo, zona y Kibelisk más cercano | "Leemos las tablas de datos del cliente global (build 0.0.4387.0)… con la calibración de mapa del propio cliente". Se actualiza en cada parche. | La API (`/api/aion-2/maps/…`) devuelve **403** (CloudFront). Solo el HTML público muestra el recuento de spots por cubo. No la forzamos. |
| **interactivemap.app** | 1 440 spots en Verteron (4 279 marcadores en total) | Datos propios, servidos con un token (`/api/mtoken`) para evitar el scraping. | No: anti-scraping. |
| **aion2atlas.app** | 2 811 spots (Verteron + Altgard) | Dice que usa "Community map data (interactivemap.app)" y "Player reports", comprobados contra el cliente. | No: son datos de terceros. |
| aion2hub / aion2.online / Fextralife | 172 o 275 "cubos" según el sitio | Cuentan grupos u otras zonas; las cifras no coinciden. | No aportan coordenadas utilizables. |
| Game8, games.gg, Destructoid, allthings.how… | Guías | Copian los mapas anteriores. | No. |

**Reddit**: r/aion2 bloquea las peticiones automáticas (HTTP 403) y el buscador no devuelve hilos, así
que no hemos podido leer las recomendaciones directamente. Las guías que sí se indexan citan siempre las
mismas cuatro fuentes: th.gl, metabot, interactivemap.app y aion2atlas. Todas coinciden en los recuentos
(1 440 / 1 371 / 456 / 455), lo que confirma que el origen común son los ficheros del cliente.

### 2.1 Formato de aion2.th.gl, en detalle

1. `https://aion2.th.gl/maps/Verteron` (Next.js) incluye en su payload
   `"nodesPaths":{"World_L_A":"/nodes/World_L_A.<md5>.raw", …}` para los seis mapas.
2. `https://cdn.th.gl/aion2/nodes/World_L_A.<md5>.raw` es un documento **cbor-x** con:
   - tag 51: tabla de valores repetidos `[valores, prefijos, sufijos, documento]`;
   - *simple values* 0–15 y tag 6 *k*: referencias a esa tabla (`valores[16 + 2k]`);
   - tag `0xDFFF [id, claves, v1…]`: define una estructura de registro (`0xE000+n`) y su primer objeto.
3. El documento es una lista de grupos `{type, mapName, static, spawns}`. Cada spawn es
   `{p: [x, y, z], id?, dbEntryId?, icon?, data?}`. Los cubos son `type == "hidden_cube"`, con
   `id == "hidden_cube@x:y"` (en Eltnen y Morheim no llevan `id`).
4. Las coordenadas son **unidades de Unreal (cm) del mundo**: el mapa se define con
   `bounds [[-408000,-408000],[408000,408000]]` y se ven valores z reales (de −12 088 a 75 272).
   Es el mismo sistema que los paquetes de spawn que lee el radar.
5. Otros tipos útiles: `gather_*` (nodos de recolección), `empyrean_trace`, `kibelisk`,
   `teleport_portal`, `merchant`, `quest_*`, `monolith`, y `monsters_*` (con `dbEntryId: "m_2100603"`,
   el mismo formato de 7 cifras que los códigos NPC de los paquetes).

Comprobado: nuestro decodificador en Python (`aion2_helper/cborx.py`, sin dependencias) produce
exactamente lo mismo que la librería oficial `cbor-x` 1.6.0 en los seis ficheros.

## 3. Qué hemos implementado

| Antes | Ahora |
|---|---|
| Calibración manual: ponerse junto a un cubo y pulsar F9, o grabar con `--log`, marcar con F8 y ejecutar `analyze`. | **Sin calibración.** La herramienta descarga los 3 722 spots y aprende sola el código NPC del cubo: el código cuyas entidades aparecen exactamente sobre spots conocidos (a ≤ 3 m y ≤ 5 m de altura). Un mob cae en un spot por casualidad en un ~0,1 % de los casos, así que exigimos 2 spots distintos y precisión ≥ 50 %. Desde el primer avistamiento ya avisa como "probable". |
| `import-spots` con coordenadas que había que convertir a mano desde píxeles de mapa. | `update-spots` (y descarga automática al arrancar, renovada cada 7 días). `update-spots --from carpeta` para ficheros descargados a mano. |
| No sabía en qué mapa estabas. | **Detecta el mapa** comparando las entidades que te rodean con los puntos fijos de cada mapa (en la simulación con los datos reales bastan 10–30 spawns). También detecta la convención de ejes (las 8 combinaciones posibles) y la guarda. Si la altura no cuadrase, hay un plan B en 2D. |
| Solo mostraba spots en los que tú ya habías visto un cubo. | Muestra **todos los spots** cercanos del mapa: azules si están por revisar y grises si ya pasaste cerca y estaban vacíos. Una flecha y la línea "→ siguiente: 42 m SO · 5 pendientes" te llevan al spot pendiente más cercano. Los revisados vuelven a pendientes a los 30 min. Ctrl+Mayús+F6 los reinicia todos (por ejemplo, al cambiar de canal). |
| Un solo color de cubo. | **Rojo = necesita llave** (código visto en Verteron/Altgard), dorado = gratis (configurable). Debajo del radar se muestra la regla del mapa ("Todos los cubos de este mapa necesitan llave"). |
| — | `analyze` ya no necesita marcas F8 si hay spots: ordena los códigos por spots acertados. |

El radio a partir del cual un spot cuenta como revisado empieza en 40 m y se ajusta solo al 60 % de la
mediana de la distancia a la que te aparecen los cubos (el servidor solo envía las entidades que tienes
cerca).

### 3.1 Lo aprendido de PetTracker

[PetTracker](https://github.com/0Fox2Give/PetTracker) es un overlay de mascotas que lee el mismo
tráfico. El repositorio solo publica el instalador y el sistema de actualizaciones, no el código; lo
que sigue sale de inspeccionar su versión pública 0.1.43 y se ha contrastado con nuestras trazas.

- **Hay que abrirlo antes de entrar al personaje.** Su entidad propia sale de `33 36`, que el servidor
  manda al entrar al mundo: `<id> <4 bytes> <07|37|3f> <long.> <nombre UTF-8> <servidor u16>`. Solo lo
  acepta con todo ese formato (id entre 100 y 999 999, nombre de 1 a 30 letras, dígitos, `'`, `-` o `_`).
  Nosotros lo tomábamos como pista porque un `33 36` sin validar no coincidía con los movimientos;
  ahora uno completo fija tu entidad igual que `4a 36`, y uno incompleto sigue siendo una pista.
- **Distingue entidades por formato exacto, no por heurística.** Cada registro se valida hasta el
  último byte (longitud exacta, rangos del código de NPC 1 000 000-10 000 000 y de habilidad
  11 000 000-20 000 000), y nunca adivina quién eres: sin `33 36`, no calcula nada tuyo.
- **`02 38`** (lanzar habilidad) lleva la orientación en grados de quien la lanza; en nuestras trazas
  coincide con el yaw de los movimientos (120,0 / 119,97; −7,4 / 352,6). Llega también de otros
  jugadores: solo se usa la de tu entidad.
- **`3c 37`** da posición y orientación de una entidad que actúa.
- **Al teletransportarte (`23 36`) vacía todo** lo que tenía de alrededor, como ya hacemos.
- Filtra las conexiones por proceso (`AION2`, `AION2-Win64-Shipping`) con la tabla TCP de Windows y
  reordena los segmentos TCP con una ventana de 1 s. Nosotros ya fijamos la conexión del juego y
  reordenamos por número de secuencia.

### 3.2 Lo aprendido de Shugo.GG App

[Shugo.GG App](https://shugo.gg/app) (0.3.95) es una app Tauri: Rust para la captura y los
decodificadores, y una interfaz web. Se ha analizado con permiso expreso de su autor.

- **Los opcodes no van en el programa.** Se descargan con la cuenta (`protocol.json` dentro de un
  paquete cifrado con una clave de la cuenta) y se asocian a ids lógicos: así sobreviven a los parches
  sin publicar una versión nueva. El paquete cifrado no se ha abierto. Nosotros, en cambio, aprendemos los
  opcodes de movimiento solos y dejamos el resto en `config.json`.
- **Tu personaje llega al entrar al juego**, no al teletransportarte: hay que volver a la selección de
  personaje, o **entrar en una mazmorra y salir**, que también lo reenvía.
- **Tu posición, cuando no es exacta**, sale de dos estimaciones que hemos copiado como idea:
  - **Borde de la visión** (`viewEdge`, 20-30 m): las entidades aparecen y desaparecen al entrar y salir
    de tu radio de visión, así que tú estás en el centro de ese círculo. Se descartan los spawns de los
    3 s que siguen a una carga de zona, que llenan toda la visión.
  - **Cerca del objetivo** (`nearTarget`, ~10 m): al lanzar una habilidad contra algo, estás cerca de
    ello. `02 38` en modo `02` lleva la posición del objetivo.
- Según Shugo, andar no envía tu posición. En nuestras trazas sí llegan `1c 37` y `1d 37` de tu entidad
  mientras andas, así que mantenemos nuestro seguimiento y usamos sus estimaciones solo como respaldo.
- Sus decodificadores (Rust, desensamblados) confirman y afinan formatos:
  - **Aparición de NPC**: `<id> <tipo> <flags> <flags de nombre> [<long. u8> <nombre> si flags & 1]
    <código u32> <2 bytes> <x> <y> <z>`. Lo leemos ahora con este orden fijo (y con nombre, para los
    NPC con nombre) y solo si no encaja buscamos el código como antes.
  - **Invocaciones y mascotas** (tipos `1d`, `1f`, `5f`): al final del registro va su dueño,
    `<id> 01 02 <12 bytes> 07 02 06 <dueño u32>`. Lo guardamos en el registro de descubrimiento.
  - **Carga de zona**: `<entidad 0> <5 bytes a cero> <x> <y> <z> <valor u32> <cola>`: nuestro `23 36`.
  - Movimiento = tipo, posición, dirección, valor, parada, aterrizaje, orientación; muerte = víctima,
    flags de combate, flags de desaparición, motivo, habilidad, asesino.
  - No hay decodificador de objetos (`34 36`): Shugo no ve nodos, Empyrean Traces ni cubos en directo;
    los de su mapa salen de su web.
  - Qué opcode corresponde a cada decodificador solo está en el `protocol.json` cifrado.

## 4. Límites y trabajo futuro

- **Grupos de spots**: con los grupos de metabot podríamos tachar todo el grupo al ver su cubo. Su API
  no es pública y los grupos no se pueden deducir por cercanía (lo hemos comprobado con clustering). La
  herramienta podría aprenderlos sola si observa que un cubo desaparece en un spot y reaparece en otro.
- **Gratis frente a llave en Eltnen/Morheim**: hay dos códigos (normal y gemelo con llave). Cuando se
  vean los dos, se pueden etiquetar en `cube_kinds` (`"key"` o `"free"`).
- Cuando th.gl publique un parche nuevo cambiará el hash de los ficheros: `update-spots` lo detecta
  solo en la página del mapa. Si la página cambia de formato, se usan las rutas conocidas.
- Los datos son de The Hidden Gaming Lair y del juego (NCSOFT): no se redistribuyen con el programa,
  se descargan en el equipo del usuario y se cita la fuente.

## Fuentes

- aion2.th.gl: [guía Hidden Cube](https://aion2.th.gl/guides/Hidden%20Cube), [mapa](https://aion2.th.gl/maps/Verteron), [Key: Hidden Cube](https://aion2.th.gl/db/inventory/it_610900001), [condiciones para desarrolladores](https://www.th.gl/developers)
- metabot.gg: [Hidden Cubes](https://metabot.gg/en/aion-2/hidden-cubes), [guía de farmeo](https://metabot.gg/en/aion-2/guides/hidden-cubes-guide), [metodología](https://metabot.gg/en/aion-2/methodology)
- [aion2atlas.app](https://aion2atlas.app/map/hidden-cube/), [interactivemap.app](https://interactivemap.app/aion2/maps/verteron/hidden-cube)
- Guías: [games.gg](https://games.gg/aion-2/guides/aion-2-all-hidden-cube-locations/), [Destructoid](https://www.destructoid.com/aion-2-hidden-cube-locations-best-spots-and-how-to-get-keys/), [allthings.how](https://allthings.how/aion-2-hidden-cubes-how-to-find-and-open-them/), [kingboost](https://kingboost.net/blogs/aion-2/aion-2-hidden-cubes-guide/), [ExitLag (resumen de mapas)](https://www.exitlag.com/blog/aion-2-interactive-map/), [mein-mmo](https://mein-mmo.de/aion-2-cubes-viele-spieler-noch-nie-gesehen-tipps/)
