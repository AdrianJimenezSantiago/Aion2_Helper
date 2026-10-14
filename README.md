# Aion2 Helper: radar de Hidden Cubes

Overlay **de uso personal** para AION 2 que avisa (con sonido y un radar) cuando aparece un
**Hidden Cube** cerca de tu personaje, y te dice a cuántos metros y en qué dirección está.

> ⚠️ Usar herramientas de terceros puede ir contra los términos de servicio de AION 2. El proyecto
> no tiene relación con NCSOFT. Úsalo bajo tu responsabilidad.

## Cómo funciona

Los cubos rotan entre miles de spots posibles (unos 2 800 en Verteron y Altgard según los mapas
comunitarios), así que un mapa estático no te dice dónde está el cubo *ahora*. El servidor, en cambio,
solo envía al cliente las entidades que tienes cerca. Cuando un cubo entra en tu radio de visión, el
servidor manda su registro de "spawn", y la herramienta lo lee.

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
- `23 36 00`: cambio de zona. Se vacía todo.

Tu **posición propia** no está documentada en ningún proyecto, así que la herramienta la **aprende
sola**: busca en los paquetes que tu cliente envía al servidor el campo con tres floats que se mueven
de forma continua y que siempre quedan cerca de las entidades que el servidor te va spawneando. Cuando
lo encuentra lo guarda en la configuración. Hasta entonces el aviso de "cubo cerca" funciona igual,
pero sin distancia ni flecha.

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
5. Pon el juego en **pantalla completa sin bordes** o en ventana: en pantalla completa exclusiva
   Windows no deja dibujar nada encima.

Para ver el overlay sin el juego: `python -m aion2_helper --demo --discovery`.

## Calibración (una sola vez)

Nadie ha publicado el código NPC del Hidden Cube, así que hay que enseñárselo a la herramienta la
primera vez:

**Forma rápida:**
1. Arranca en modo descubrimiento: `python -m aion2_helper --discovery --log`
2. Busca un cubo como siempre (baja la música y los efectos: el cubo emite un zumbido brillante).
3. Ponte **al lado** del cubo y pulsa **F9**. Se añade a `cube_codes` el código de la entidad más
   cercana (a menos de 8 m). Si aún no hay posición propia, se usa la entidad que apareció más
   recientemente; en ese caso es mejor usar la forma de abajo.

**Forma robusta** (si F9 elige mal):
1. Juega con `--log` y pulsa **F8** cada vez que estés junto a un cubo (con 3 o 4 basta).
2. Analiza el registro:
   ```
   python -m aion2_helper analyze "%APPDATA%\Aion2Helper\logs\discovery-XXXX.jsonl"
   ```
3. Copia el código con mejor puntuación en `cube_codes` de `%APPDATA%\Aion2Helper\config.json`.

Pueden existir dos códigos (cubo normal y cubo con llave). Si es así, marca uno de cada.

## Atajos (configurables en `config.json`)

| Tecla | Acción |
|---|---|
| F7 | Modo descubrimiento: muestra las entidades cercanas con su código |
| F8 | Marca "aquí hay un cubo" en el registro (para `analyze`) |
| F9 | La entidad más cercana es un cubo: guarda su código |
| F10 | Click-through on/off (el ratón atraviesa el overlay) |
| F11 | Mostrar/ocultar overlay |

Arrastra el overlay con el ratón para moverlo; con **clic derecho** se cierra.

## Spots conocidos

Cada cubo detectado se guarda en `%APPDATA%\Aion2Helper\spots.json`, y el radar muestra en azul los
spots conocidos cercanos, aunque ahora estén vacíos, para que puedas repasarlos.
`python -m aion2_helper import-spots fichero.csv` importa spots en **coordenadas del mundo**
(`x,y,z`). Las coordenadas de los mapas web ([aion2.th.gl](https://aion2.th.gl/guides/Hidden%20Cube),
[metabot.gg](https://metabot.gg/en/aion-2/hidden-cubes),
[aion2atlas](https://aion2atlas.app/map/hidden-cube/),
[interactivemap.app](https://interactivemap.app/aion2/maps/verteron/hidden-cube),
[Game8](https://game8.co/games/Aion-2/archives/622610)) están en píxeles de mapa y hay que
convertirlas antes.

## Opciones

```
python -m aion2_helper [--discovery] [--log [ruta]] [--record juego.pcap] [--iface NOMBRE|all]
python -m aion2_helper replay juego.pcap [--fast] [--headless]
python -m aion2_helper analyze registro.jsonl
python -m aion2_helper import-spots spots.csv
python -m aion2_helper ifaces
```

- `--record` guarda el tráfico del juego para reproducirlo después con `replay`. El .pcap contiene
  nombres de personajes, así que no lo compartas.
- `--iface all` si usas VPN o un reductor de ping y no se detecta la conexión.

## Si deja de funcionar tras un parche

Los parches a veces renumeran opcodes: el de junio de 2026 movió la familia `xx 36` en +1. Todo está
en `config.json`:

```json
"opcodes": { "spawn": ["41 36"], "despawn": ["42 36"], "zone_change": ["23 36"],
             "signatures": ["0E 00 36", "06 00 36"] }
```

Mira qué cambian los DPS meters citados arriba y copia sus valores. Si la posición propia se aprende
mal, borra `"self_position"` (déjalo en `null`) para que se vuelva a aprender.

Otros ajustes: `radar_range_m`, `radar_rotation_deg` y `radar_mirror` (si el norte del radar no
coincide con el del mapa), `cube_ttl_s`, `sound` y `overlay.alpha`.

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
| `aion2_helper/overlay.py` | Overlay tkinter y atajos globales |
| `aion2_helper/analyze.py` | Ranking de códigos a partir de las marcas F8 |
| `aion2_helper/spots.py` | Spots conocidos |

Los tests usan bytes reales de una captura del 6 de octubre de 2026 publicados en los tests de A2Tools.
