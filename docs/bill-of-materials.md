# V1 bill of materials

These quantities build one plug-in tabletop radio. The original prototype demonstrated stereo playback with the electronics below. The public host and firmware still need a test together on hardware. The proposed mobile V2 has no confirmed parts list.

## Electronics from the working prototype

| Qty | Part | What to look for |
| ---: | --- | --- |
| 1 | [Raspberry Pi Zero 2 W](https://www.raspberrypi.com/products/raspberry-pi-zero-2-w/) | Runs the player and sends digital audio to the amplifier. Check whether your board comes with a GPIO header. |
| 1 | [M5Stack Dial, original K130](https://docs.m5stack.com/en/core/Dial) | Rotary control, push button and display. Keep the panel locking ring for the enclosure. |
| 1 | [Adafruit stereo I2S amplifier, product 6513](https://www.adafruit.com/product/6513) | One board with two MAX98357A chips drives both speakers. |
| 2 | Enclosed speakers, 4 Ω, 3 W each | One per channel. The prototype label read HIBOX3070-K. [Adafruit 3351](https://www.adafruit.com/product/3351) is the dimensional reference for the 30 × 70 mm enclosure mounts. Check your speakers against the fit coupon. |

## Power, storage and wiring

Choose these by connector and function. Exact accessory models are unconfirmed.

| Qty | Item | Selection or fit check |
| ---: | --- | --- |
| 1 | Micro-USB wall supply for the Pi | Plug into **PWR IN**. Raspberry Pi recommends its [micro-USB supply](https://www.raspberrypi.com/products/raspberry-pi-zero-2-w/). If your supply has a detachable lead, include one matching micro-B power cable. |
| 1 | Right-angle micro-USB OTG host adapter | Fits the Pi port marked **USB**. Choose an orientation that leaves room for the adjacent power plug. |
| 1 | USB data cable for the Dial | Match the host end to the adapter and the USB-C end to the Dial. This cable carries both power and controls. A charge-only cable cannot carry controls. |
| 1 | microSD card | Holds the Pi operating system and your local music. Allow space for both. |
| 1 set | Pi-to-amplifier hookup wire and compatible headers or connectors | Allow for power, ground and I2S signals. Check which headers are fitted to your boards before ordering. |
| 2 pairs | Speaker leads | Count leads already attached to the speakers. Connect each pair to its own channel on the amplifier. |

For bench assembly, have a computer with a microSD reader, wire strippers and a screwdriver that fits the amplifier terminals. If headers or leads need soldering, have a soldering iron and electronics solder.

## Printable enclosure and fasteners

The V1 enclosure has passed software geometry and slicing checks. It has yet to be printed or fit-tested. The fastener quantities below come from the model.

1. Print one [front fit coupon](../cad/stl/v1-coupon-front.stl). Check the Dial, locking ring, speaker flange, holes and lead exit.
2. Print one [back panel](../cad/stl/v1-back.stl). Mount both boards and plug in both USB connectors. Check the connectors fit side by side and the power lead reaches the strain-relief saddle and exit notch.
3. Once both checks pass, print one [body](../cad/stl/v1-body.stl) and one [handle](../cad/stl/v1-handle.stl).

| Qty | Hardware | Where it goes |
| ---: | --- | --- |
| 8 | M2.5 × 6 mm screws | Four for the Pi and four for the amplifier. Nylon is suitable for the boards. |
| 8 | M2.5 × 6 mm steel screws and 8 washers | Four per speaker. Keep to the specified length because the posts have shallow pilot holes. |
| 4 | M2.5 × 12 mm steel screws | Back panel to body. |
| 2 | M3 × 12 mm steel screws | Handle to body. Install from inside before closing the back. Check the handle under load before carrying the radio. |
| 3 | Cable ties, up to 3.6 mm wide | One for the power lead and two for excess Dial cable. |

Allow about 313 g of PLA for one of each print, plus extra for retries. This estimate assumes a 0.4 mm nozzle and 0.20 mm layers. Have access to a filament printer, calipers, screwdrivers that match your fasteners and cutters to trim the ties.
