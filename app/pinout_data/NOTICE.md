# Pinout data: sources and licences

`pi5.json` in this folder is **not** covered by CoilGate's GPL. It is licensed under the
[Creative Commons Attribution-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-sa/4.0/)
(full text in `LICENSE-CC-BY-SA-4.0.txt`), because it is adapted from Pinout.xyz.

## Pinout.xyz

- **Project:** Pinout.xyz, the Raspberry Pi GPIO pinout guide, by Philip Howard (@Gadgetoid) and contributors.
- **Website:** https://pinout.xyz
- **Source:** https://github.com/pinout-xyz/Pinout.xyz (commit `c203faef3c3f85c41a8cfa639ffa16912d99a457`)
- **Licence:** CC BY-SA 4.0

Used from it:

- the 40-pin header layout (`src/en/template/pinout.yaml`);
- the Raspberry Pi 5 (RP1) alternate-function table (`common/pin-functions.yaml`);
- the text of the GPIO, 3v3 Power, 5v Power, Ground, I2C, SPI, UART, PWM, PCM, GPCLK,
  1-Wire, SDIO and JTAG pages (`src/en/overlay/`).

Changes made for CoilGate (October 2026):

- trimmed to the Raspberry Pi 5: details that apply only to earlier models were removed,
  and tables were reduced to their Pi 5 columns;
- the pins highlighted for each interface were rebuilt from the Pi 5 RP1 function table,
  with Pi 5 names (for example `PWM0 CHAN0`, `SPI3 MOSI (SIO0)`);
- short DPI and PIO descriptions were written, the GPCLK clock-source table (which
  describes the older chips) was replaced by a note, and the JTAG page was reworded for the
  Pi 5, which has no JTAG on its header;
- notes about how CoilGate uses some pins were added;
- the data was converted to JSON.

## Raspberry Pi pinctrl

Pinout.xyz generated its Pi 5 function table from the `pinctrl` utility in
https://github.com/raspberrypi/utils (`pinctrl/gpiochip_rp1.c`), which carries this licence:

```
Copyright (c) 2023, Raspberry Pi Ltd.
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
    * Redistributions of source code must retain the above copyright
      notice, this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.
    * Neither the name of the copyright holder nor the
      names of its contributors may be used to endorse or promote products
      derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY
DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

The Pinout page in CoilGate (`/pinout`) shows this attribution at the bottom.
