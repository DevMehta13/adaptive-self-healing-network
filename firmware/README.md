# ESP8266 firmware

F0 provides the bounded C++ codecs shared by all four ESP8266 nodes. It does not use
ESP-NOW or a physical board yet. The radio decoder checks the 14-byte header and every
defined payload in the documented order. The serial decoder checks line limits,
printable ASCII, CRC, strict fields and embedded radio frames. Both use caller-owned
buffers and fixed-capacity views; an invalid message changes no node state.

## Prerequisite

Install Python 3.11 or newer, then install PlatformIO Core in an isolated environment.
The commands are portable between macOS Terminal and Windows PowerShell:

```sh
cd firmware
python -m venv .platformio-venv
```

Activate it on macOS:

```sh
source .platformio-venv/bin/activate
```

Or on Windows PowerShell:

```powershell
.platformio-venv\Scripts\Activate.ps1
```

Then install the pinned tool and run the checks:

```sh
python -m pip install -r requirements.txt
pio test -e native
pio run -e nodemcuv2
```

The native check runs all 79 radio vectors and all 48 serial vectors from
`tests/generated/test_vectors.h`. The NodeMCU command cross-compiles the same codec for
the selected ESP8266 board. Neither command uploads firmware or proves radio behavior.
F1 is the first real two-board ESP-NOW experiment.

If Harshal changes a protocol example, regenerate the shared header from `backend/`
before rerunning F0:

```sh
uv run python -m iot_backend.vectors --check
```
