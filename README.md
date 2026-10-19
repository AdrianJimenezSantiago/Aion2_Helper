# Aion2 Helper: radar de Hidden Cubes

Overlay **de uso personal** para AION 2 que avisa (con sonido y un radar) cuando aparece un
**Hidden Cube** cerca de tu personaje, y te dice a cuántos metros y en qué dirección está. Además te
guía por los 3 722 spots posibles de los cubos (los rojos que piden llave y los dorados gratuitos) y
tacha los que ya has revisado.

![Overlay: aviso de cubo nuevo, lista de cubos y modo compacto](docs/overlay-demo.png)

> ⚠️ Usar herramientas de terceros puede ir contra los términos de servicio de AION 2. El proyecto
> no tiene relación con NCSOFT. Úsalo bajo tu responsabilidad.

## Cómo funciona

Los cubos rotan entre miles de spots posibles: 1 440 en Verteron, 1 371 en Altgard, 456 en Eltnen y
455 en Morheim. Un mapa estático no te dice dónde está el cubo *ahora*. El servidor, en cambio, solo
envía al cliente las entidades que tienes cerca. Cuando un cubo entra en tu radio de visión, el servidor
manda su registro de "spawn", y la herramienta lo lee.

Los spots se descargan de [aion2.th.gl](https://aion2.th.gl/guides/Hidden%20Cube), que los extrae de
los ficheros del juego en las mismas coordenadas que envía el servidor. Con ellos la herramienta:

- **sabe en qué mapa estás**, porque las entidades que te rodean caen sobre los puntos fijos de ese mapa;
- **aprende sola el código del cubo**: el código cuyas entidades aparecen justo encima de spots
  conocidos. Ya no hace falta calibrar;
- te muestra los **spots pendientes** cercanos, tacha los que ya revisaste y te indica el siguiente;
- distingue los cubos **con llave** (rojos; todos en Verteron y Altgard) de los **gratuitos** y muestra
  la regla del mapa.

La investigación completa sobre las webs de la comunidad y su formato de datos está en
[docs/INVESTIGACION_CUBOS.md](docs/INVESTIGACION_CUBOS.md).

Usa la misma técnica que los DPS meters open source del juego
([A2Tools](https://github.com/taengu/A2Tools-DPS-Meter),
[domel1998/Aion2DPSMeter](https://github.com/domel1998/Aion2DPSMeter),
[NOIA2](https://github.com/ZDYoung0519/NOIA2),
[cyberbadger6969/aion2-dps-meter](https://github.com/cyberbadger6969/aion2-dps-meter)):

- **Captura pasiva** del tráfico con Npcap. No envía nada al juego ni a sus servidores, no lee ni
  escribe la memoria del juego y no inyecta nada en él.
- El flujo del juego no va cifrado: paquetes con longitud varint y bundles comprimidos con LZ4.
- `41 36 <id> <mask> … <código NPC u24> 00 (00|40) 02 <x f32> <y f32> <z f32>`: spawn de NPC/objeto
  con su posición en el mundo, en unidades de Unreal (cm).
- `42 36 <id> 00 <flag>`: muerte (flag 3) o desaparición (flag 7). Con eso el cubo se quita del radar.
- `23 36 00 <5 bytes> <x> <y> <z>`: teletransporte del jugador local (al entrar en una zona): tu
  posición exacta.
- `21 36 <u32> <u32 id de mapa> <8 bytes> <x> <y> <z>`: carga de mapa (1010 = Verteron) y tu posición.
  La herramienta aprende qué id corresponde a cada mapa.
- `1a 37 <id> <2 bytes> <x> <y> <z> <yaw u16>` y `1b 37` / `1c 37 <id> <flags> [1 byte si flags & 1] <x> <y> <z> <yaw u16>`:
  una entidad se mueve, también tú. `1b` empieza a andar, `1c` informa cada ~2 m y `1a` se para; el
  yaw (u16 × 360/65536, como `atan2(dy, dx)`) es la dirección de la marcha.
  Otros opcodes con la forma `<id> <posición>` se aprenden solos.
- `34 36 <id> <tipo> 00 <código u32> <x> <y> <z>`: aparece un **objeto** (nodos de recolección, Empyrean
  Traces y, casi seguro, los Hidden Cubes). Las posiciones coinciden al centímetro con aion2.th.gl.
- `45 36 <id> …`: aparece otro jugador. Se usa solo para no confundirlo contigo.
- `1d 37 <id> <flags> [modo] [vx] [vy] [vz] <yaw> <yaw> [01]`: la **velocidad**, 10 veces por segundo,
  en unidades de 10 cm/s (los bits 0-3 de flags dicen qué campos van). Integrada entre dos posiciones
  tuyas cae a 0,18 m de mediana de la siguiente (traza en directo).
- `20 37 <id> <flags> <5 bytes> <vx> <vy> <vz>`: velocidad en floats (cm/s), al volar.
- `2a 37 <id> … <vx> <vy> <vz> <yaw>` y `28 37` / `29 37 <id> <2 bytes> <x> <y> <z> <vx> <vy> <vz>`: al
  volar, velocidad (y posición) 10 veces por segundo.
- `33 36 <id> <4 bytes> <07|37|3f> <long.> <nombre> <servidor u16>`: el personaje con el que entras
  al mundo. Es tu entidad, sin dudas, pero **solo llega al entrar desde la selección de personaje**.
  Solo cuenta si viene completo; el nombre se comprueba y no se guarda.
- `4a 36 <id> …`: tus estadísticas; solo llegan de tu personaje, así que su id también es tu entidad.
  Sin ninguno de los dos, tu entidad se deduce de quién se mueve sin haber aparecido como monstruo,
  objeto o mascota (los flags `05` no sirven: otros jugadores también los llevan).
- `02 38 <id> 00 <habilidad u32> <u8> <modo> <objetivo> <orientación f32>`: una habilidad, con hacia
  dónde mira quien la lanza, en grados (120,0 frente al yaw 119,97 de tu marcha, en directo). Los buffs
  de vuelo la mandan cada segundo: es la flecha del radar mientras vuelas.
- `3c 37 <id> 03 <x> <y> <z> <orientación f32> …`: una entidad actuando, con su posición y orientación.
- `2a 38` / `2b 38`: un efecto con la posición de quien lo lanza; si te lo lanzas tú (montura,
  vuelo), es tu posición, más o menos cada segundo.
- Bloques LZ4 dentro de otros paquetes: también se abren (llevan spawns).

Los paquetes que envía tu cliente van **cifrados por completo** (comprobado en directo), así que tu
**posición propia** sale del servidor: exacta al entrar en una zona y, después, de los registros de
movimiento de tu propia entidad, que el servidor envía al empezar a moverte, girar o pararte. Mientras
no se tiene tu posición, se estima (radar con "posición aproximada"): junto al objetivo de tu última
habilidad (unos 10 m de error), o en el centro del círculo donde las entidades entran y salen de tu
radio de visión (20-30 m), o, sin más, en el centro de lo que el servidor te muestra.
Con tu entidad reconocida, tu posición avanza con cada registro
de velocidad (10 por segundo) y se corrige con cada posición exacta. Si no hay velocidad, el radar desliza tu posición con
tu velocidad (la de `29 37`, o la medida entre tus registros) en la dirección de tu yaw, hasta que
llega un registro de parada, para que se mueva de forma continua.

Los datos de aion2.th.gl guardan las coordenadas como `[y, x]` del juego (`fromInGameCoords`), y en
su mapa +X es el este y +Y el sur. La herramienta detecta sola ese intercambio de ejes (`yx`).

## Instalación (Windows 10/11)

1. Instala **Npcap** desde <https://npcap.com/#download> y marca *"Install Npcap in WinPcap
   API-compatible Mode"*.
2. Instala Python 3.10 o superior (marca "Add to PATH").
3. En esta carpeta:
   ```
   pip install -r requirements.txt
   ```
4. Ejecuta **como administrador** (Npcap y los atajos globales lo necesitan):
   ```
   python -m aion2_helper
   ```
   Ábrelo **antes de entrar con tu personaje**: el servidor dice cuál es tu entidad al entrar al mundo.
   Si ya estabas jugando, vuelve a la selección de personaje y entra otra vez, o entra en una mazmorra
   y sal (también lo reenvía; no basta con teletransportarse).
5. Pon el juego en **pantalla completa sin bordes** o en ventana: en pantalla completa exclusiva
   Windows no deja dibujar nada encima.

Para ver el overlay sin el juego: `python -m aion2_helper --demo --discovery`.

La primera vez que arranca descarga los spots de aion2.th.gl (unos 400 KB) y los renueva cada 7 días.
Si no hay conexión, ejecuta más tarde `python -m aion2_helper update-spots`.

## Calibración: automática

No hay que hacer nada: juega normalmente. En cuanto veas un cubo, la herramienta lo reconoce porque está
sobre un spot conocido, avisa y, con el segundo, guarda su código en `cube_codes`. El overlay muestra
"Código del Hidden Cube aprendido solo". En Verteron y Altgard el código queda marcado como cubo con
llave.

Si quieres hacerlo a mano (por ejemplo, sin los spots descargados):

**Forma rápida:**
1. Arranca en modo descubrimiento: `python -m aion2_helper --discovery --log`
2. Busca un cubo como siempre (baja la música y los efectos: el cubo emite un zumbido brillante).
3. Ponte **al lado** del cubo y pulsa **Ctrl+Mayús+F9**. Se añade a `cube_codes` el código de la entidad más
   cercana (a menos de 8 m). Si aún no hay posición propia, se usa la entidad que apareció más
   recientemente; en ese caso es mejor usar la forma de abajo.

**Forma robusta** (si Ctrl+Mayús+F9 elige mal):
1. Juega con `--log` y pulsa **Ctrl+Mayús+F8** cada vez que estés junto a un cubo (con 3 o 4 basta).
2. Analiza el registro:
   ```
   python -m aion2_helper analyze "%APPDATA%\Aion2Helper\logs\discovery-XXXX.jsonl"
   ```
3. Copia el código con mejor puntuación en `cube_codes` de `%APPDATA%\Aion2Helper\config.json`.

Pueden existir varios códigos (cubo con llave, cubo gratuito y versión submarina). Con los spots
descargados, `analyze` los ordena solo, sin marcas. Para colorear un código como gratuito, pon
`"cube_kinds": {"1234567": "free"}` en la configuración.

## Atajos (configurables en `config.json`)

| Tecla | Acción |
|---|---|
| Ctrl+Mayús+F5 | Panel de diagnóstico |
| Ctrl+Mayús+F6 | Vuelve a poner todos los spots como pendientes (por ejemplo, al cambiar de canal) |
| Ctrl+Mayús+F7 | Modo descubrimiento: muestra las entidades cercanas con su código |
| Ctrl+Mayús+F8 | Marca "aquí hay un cubo" en el registro (para `analyze`) |
| Ctrl+Mayús+F9 | La entidad más cercana es un cubo: guarda su código |
| Ctrl+Mayús+F10 | Click-through on/off (el ratón atraviesa el overlay) |
| Ctrl+Mayús+F11 | Mostrar/ocultar overlay |
| Ctrl+Mayús+F12 | Modo compacto (solo el radar) |

Llevan Ctrl+Mayús porque los atajos globales no se "comen" la tecla: con F6–F8 solos, usar un objeto
de la barra del juego también activaba el overlay. Si tu configuración tenía las teclas antiguas, se
cambian solas.

Con el ratón:

- **arrastrar** mueve el overlay;
- la **rueda** cambia el alcance del radar (30–240 m);
- **doble clic** en la cabecera activa o desactiva el modo compacto;
- el **clic derecho** abre un menú: modo compacto, sonido, animaciones, modo descubrimiento,
  click-through, reiniciar spots, ocultar y cerrar.

## Qué muestra el overlay

- **Cabecera**: mapa actual y su regla de llave (`LLAVE` o `GRATIS / LLAVE`), número de cubos cerca
  y cuatro luces de estado: conexión con el juego, posición propia, spots de la comunidad y código del
  cubo. Verde significa que está listo; ámbar, que se está buscando o aprendiendo; rojo, que falta.
- **Radar**: el norte arriba, con anillos de distancia, un barrido animado y tú en el centro. Los cubos
  fuera de alcance aparecen como flechas en el borde con su distancia.
- **Al aparecer un cubo**: ondas en su posición, el borde del panel parpadea con el color del cubo,
  aparece un aviso y suena una melodía (distinta para los cubos con llave y los gratuitos).
- **Siguiente spot**: la flecha te lleva al spot pendiente más cercano. La barra verde muestra cuántos
  spots del radar has revisado ya.
- **Tarjetas de cubos**: distancia, dirección, altura (▲ encima de ti, en un saliente; ▼ debajo, en el
  agua o en una cueva), tipo (`LLAVE` o `GRATIS`) y cuánto hace que apareció.

Las animaciones van a 15 fps (30 fps durante un aviso) y cada fotograma tarda unos 10–13 ms. Si el juego
va justo de CPU, desactívalas en el menú.

## Spots

En el radar:

| Símbolo | Significado |
|---|---|
| ◆ rojo con halo | Hidden Cube que necesita llave |
| ◆ dorado | Hidden Cube gratuito (`cube_kinds`) |
| ◆ naranja | Hidden Cube de tipo desconocido |
| ○ azul | Spot por revisar |
| · gris | Spot revisado: pasaste cerca y estaba vacío. Vuelve a azul a los 30 min (`spot_recheck_s`) |
| ◎ azul claro y línea discontinua | Siguiente spot pendiente más cercano (flecha en el borde si está lejos) |
| ➤ en el borde | Cubo fuera del alcance del radar, con su distancia |

Un spot cuenta como revisado cuando pasas a menos de 40 m (`spot_check_radius_m`). La herramienta ajusta
ese radio sola según la distancia a la que te van apareciendo los cubos.

`python -m aion2_helper update-spots` descarga los spots al momento. Si la descarga falla (proxy,
firewall...), baja a mano los ficheros `World_*.raw` de `https://cdn.th.gl/aion2/nodes/` (los nombres
actuales aparecen como `nodesPaths` en el código fuente de <https://aion2.th.gl/maps/Verteron>) y usa
`update-spots --from carpeta`.

Además, cada cubo detectado se guarda en `%APPDATA%\Aion2Helper\spots.json`. Esos spots propios se
muestran mientras no se reconoce el mapa. `python -m aion2_helper import-spots fichero.csv` añade spots
en **coordenadas del mundo** (`x,y,z`).

## Opciones

```
python -m aion2_helper [--discovery] [--log [ruta]] [--record juego.pcap] [--iface NOMBRE|all]
python -m aion2_helper replay juego.pcap [--fast] [--headless]
python -m aion2_helper diagnose juego.pcap
python -m aion2_helper analyze registro.jsonl
python -m aion2_helper update-spots [--from carpeta]
python -m aion2_helper import-spots spots.csv
python -m aion2_helper ifaces
```

- `--record` guarda el tráfico del juego para reproducirlo después con `replay`. El .pcap contiene
  nombres de personajes, así que no lo compartas.
- `--iface all` si usas VPN o un reductor de ping y no se detecta la conexión.

## No detecta nada: diagnóstico

Las luces de la cabecera dicen en qué paso se queda:

| Luz en ámbar o rojo | Qué significa |
|---|---|
| JUEGO | No se encuentra la conexión del juego (Npcap, permisos de administrador, VPN: prueba `--iface all`) |
| POSICIÓN | Ámbar: posición aproximada (estimada, de 10 a 30 m de error) o desconocida. Verde: exacta. Si el radar pide *vuelve a entrar con tu personaje*, vuelve a la selección de personaje con la herramienta abierta, o entra en una mazmorra y sal |
| SPOTS | Hay spots, pero el mapa no se reconoce todavía |
| CÓDIGO | Aún no se ha visto ningún cubo sobre un spot conocido |

Para ver el detalle:

- **Panel de diagnóstico** (clic derecho > *Diagnóstico*, o Ctrl+Mayús+F5): paquetes recibidos,
  bloques LZ4, opcodes más frecuentes, spawns con posición, votos de cada mapa, lo cerca que caen los
  spawns de los puntos de cada mapa, y el estado del aprendizaje de la posición propia.
- **Copiar diagnóstico** (clic derecho): copia ese texto al portapapeles para compartirlo. No contiene
  nombres de personajes ni chat.
- **Traza de tu entidad**: `logs\selftrace-*.txt` se crea al arrancar. Mientras tu entidad no se ha
  reconocido guarda todos los registros de movimiento (`xx 37`), los teletransportes y las cargas de
  mapa; después, solo los paquetes que te mencionan. Nunca guarda los registros con nombres. Compártela
  si el radar no te sigue bien.
- **Log**: `%APPDATA%\Aion2Helper\logs\aion2helper.log`, con el mismo informe cada 30 s, la conexión
  elegida, el mapa reconocido y los errores (clic derecho > *Abrir carpeta de logs*).
- **Grabar y analizar**: `python -m aion2_helper --record juego.pcap` mientras juegas un par de minutos
  y luego `python -m aion2_helper diagnose juego.pcap`. El .pcap contiene nombres de personajes: comparte
  solo la salida de `diagnose`.

Cómo leer el informe:

- `Spawns: 0`: no llegan registros de spawn. Mira los opcodes más frecuentes; quizá un parche los ha
  renumerado (ver la sección siguiente).
- Hay spawns, pero los `votos` están vacíos y la `alineación` da medianas de decenas de metros en todos
  los mapas: las coordenadas del juego no coinciden con las de los spots.
- `Posición propia`: los paquetes del cliente van cifrados, así que tu posición sale del servidor. Al
  entrar en una zona llega exacta (`teletransporte`); después se sigue a tu entidad (`servidor`) y, si
  aún no se ha identificado, se estima con las entidades que te rodean (`aproximada`). Con una posición
  aproximada los spots no se tachan como revisados.
- `Muestras (hex)`: el inicio de los últimos paquetes de varios opcodes, sin nombres. Sirven para
  descifrar registros nuevos; inclúyelas si reportas un problema.

## Si deja de funcionar tras un parche

Los parches a veces renumeran opcodes: el de junio de 2026 movió la familia `xx 36` en +1. Todo está
en `config.json`:

```json
"opcodes": { "spawn": ["41 36"], "despawn": ["42 36"], "zone_change": ["23 36"],
             "signatures": ["0E 00 36", "06 00 36"] }
```

Mira qué cambian los DPS meters citados arriba y copia sus valores.

Si el mapa no se reconoce nunca ("buscando mapa"), borra `"world_transform"` (déjalo en `null`) y
ejecuta `update-spots`.

Otros ajustes: `radar_range_m`, `radar_rotation_deg` y `radar_mirror` (si el norte del radar no
coincide con el del mapa), `cube_ttl_s`, `sound`, `overlay.alpha` (transparencia), `overlay.compact`,
`overlay.animations`, `auto_cube_codes` (desactiva el
aprendizaje automático), `community.auto_update` y `community.max_age_days`.

## Desarrollo

```
pip install -e .[dev]
pytest
```

| Ruta | Qué hace |
|---|---|
| `aion2_helper/protocol.py` | Framing (varint, LZ4), spawn/despawn/zona |
| `aion2_helper/capture.py` | Npcap/scapy, reensamblado TCP, detección de la conexión, pcap |
| `aion2_helper/selfpos.py` | Aprendizaje de la posición propia |
| `aion2_helper/detector.py` | Estado del mundo y cubos activos |
| `aion2_helper/overlay.py` | Overlay tkinter (dibujo, efectos, ratón, menú) y atajos globales |
| `aion2_helper/theme.py` | Paleta, tipografías y geometría del overlay |
| `aion2_helper/analyze.py` | Ranking de códigos por spots o por marcas |
| `aion2_helper/diag.py` | Contadores de cada etapa, informe y log |
| `aion2_helper/spots.py` | Spots vistos por ti |
| `aion2_helper/community.py` | Descarga y conversión de los spots de aion2.th.gl |
| `aion2_helper/cborx.py` | Decodificador CBOR con las extensiones de cbor-x (formato de th.gl) |
| `aion2_helper/worldmap.py` | Índice de spots, detección de mapa, aprendizaje del código, spots revisados |

Los tests usan bytes reales de una captura del 6 de octubre de 2026 publicados en los tests de A2Tools.
