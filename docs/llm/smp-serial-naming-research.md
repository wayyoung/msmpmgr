# SMP Serial Transport Parameter Naming: Cross-Implementation Research

> **Provenance:** Generated on 2026-04-14 by Claude Opus 4.6 (Anthropic) via
> Claude Code CLI. While all claims are grounded in cited sources that were
> fetched and verified during generation, LLMs can misinterpret, misquote, or
> hallucinate details. Source URLs were valid at time of generation but may move
> or change. Zephyr Kconfig sources are pinned to commit `2ee0b2b3c0` for
> reproducibility. Readers should verify critical details against the linked
> sources before making decisions.

## Purpose

Gather naming conventions for serial transport buffer/framing parameters across SMP implementations to inform smpmgr CLI option naming for `line_length`, `line_buffers`, and the derived `max_smp_encoded_frame_size`.

---

## 1. SMP Protocol Specification (smp-console.md)

**Source:** [apache/mynewt-mcumgr — transport/smp-console.md](https://github.com/apache/mynewt-mcumgr/blob/master/transport/smp-console.md)

The protocol spec uses the term **"frame"** for individual serial lines:

> "Mcumgr packets sent over serial are fragmented into **frames** of **127 bytes** or fewer."

> "the entire frame, including header, CRC, and terminating newline"

The spec does not use the terms "line_length" or "line_buffers" — it speaks of **frames** with a hard-coded **127-byte** maximum (128 including the newline terminator).

**Terminology used:** `frame`, `packet` (the reassembled whole), `fragment` (implied by "fragmented into frames").

---

## 2. MCUboot Serial Recovery (Zephyr KConfig)

**Source:** [mcu-tools/mcuboot — boot/zephyr/Kconfig.serial_recovery](https://github.com/mcu-tools/mcuboot/blob/main/boot/zephyr/Kconfig.serial_recovery)

### `BOOT_MAX_LINE_INPUT_LEN`
- **Type:** int
- **Default:** 128
- **Help:** `"Maximum length of input serial port buffer (SMP serial transport uses fragments of 128-bytes, this should not need to be changed unless a different value is used for the transport)."`

### `BOOT_LINE_BUFS`
- **Type:** int
- **Default:** 8
- **Range:** 2–128
- **Help:** `"Number of receive buffers for data received via the serial port."`

### `BOOT_SERIAL_MAX_RECEIVE_SIZE`
- **Type:** int
- **Default:** 1024
- **Help:** `"Maximum length of received commands via the serial port (this should be equal to the maximum line length, BOOT_MAX_LINE_INPUT_LEN times by the number of receive buffers, BOOT_LINE_BUFS to allow for optimal data transfer speeds)."`

**Key quote on the relationship:**
> "this should be equal to the maximum line length, BOOT_MAX_LINE_INPUT_LEN times by the number of receive buffers, BOOT_LINE_BUFS"

So: `max_receive_size = line_input_len × line_bufs` — exactly the same relationship as smpclient's `max_smp_encoded_frame_size = line_length × line_buffers`.

**Terminology used:** `line` (input length), `line bufs` / `receive buffers`, `max receive size`.

---

## 3. Zephyr MCUmgr Transport (KConfig)

**Source (pinned commit):**
- [Kconfig.uart](https://github.com/zephyrproject-rtos/zephyr/blob/2ee0b2b3c0ad8b9af8335224e9fb1ec8afdcfa67/subsys/mgmt/mcumgr/transport/Kconfig.uart)
- [Kconfig (main transport)](https://github.com/zephyrproject-rtos/zephyr/blob/2ee0b2b3c0ad8b9af8335224e9fb1ec8afdcfa67/subsys/mgmt/mcumgr/transport/Kconfig)
- [Kconfig.shell](https://github.com/zephyrproject-rtos/zephyr/blob/2ee0b2b3c0ad8b9af8335224e9fb1ec8afdcfa67/subsys/mgmt/mcumgr/transport/Kconfig.shell)
- [Kconfig.common](https://github.com/zephyrproject-rtos/zephyr/blob/2ee0b2b3c0ad8b9af8335224e9fb1ec8afdcfa67/subsys/mgmt/mcumgr/transport/Kconfig.common)

### 3a. UART Transport (`Kconfig.uart`)

#### `MCUMGR_TRANSPORT_UART_MTU`
- **Type:** int
- **Default:** 256
- **Help:** `"Maximum size of SMP frames sent and received over UART, in bytes. This value must satisfy the following relation: MCUMGR_TRANSPORT_UART_MTU <= MCUMGR_TRANSPORT_NETBUF_SIZE + 2"`

The UART transport exposes a single `MTU` value. The line framing (127-byte fragments) is handled internally per the SMP serial protocol spec. There is no separate `RX_BUF_COUNT` for the UART transport (contrast with Shell, below).

### 3b. Shell Transport (`Kconfig.shell`)

#### `MCUMGR_TRANSPORT_SHELL_MTU`
- **Type:** int
- **Default:** 256
- **Help:** `"Maximum size of SMP frames sent and received over shell. This value must satisfy the following relation: MCUMGR_TRANSPORT_SHELL_MTU <= MCUMGR_TRANSPORT_NETBUF_SIZE + 2"`

#### `MCUMGR_TRANSPORT_SHELL_RX_BUF_COUNT`
- **Type:** int
- **Default:** 2
- **Help:** `"Number of buffers used for receiving SMP fragments over shell."`

The Shell transport **does** expose both an MTU and a separate buffer count (`RX_BUF_COUNT`). This is the closest Zephyr analog to the MCUboot `LINE_BUFS` / smpclient `line_buffers` concept.

### 3c. General Transport Buffers (`Kconfig` main file)

#### `MCUMGR_TRANSPORT_NETBUF_COUNT`
- **Type:** int
- **Default:** 2 (if UDP), else 4
- **Help:** `"The number of net_bufs to allocate for MCUmgr. These buffers are used for both requests and responses."`

#### `MCUMGR_TRANSPORT_NETBUF_SIZE`
- **Type:** int
- **Default:** 2048 (if UDP), else 384
- **Help:** `"The size, in bytes, of each MCUmgr buffer. This value must satisfy the following relation: MCUMGR_TRANSPORT_NETBUF_SIZE >= transport-specific-MTU + transport-overhead"`

These are the underlying buffer pool options shared across all transports. They constrain the per-transport MTU settings.

### 3d. Common UART Configuration (`Kconfig.common`)

#### `MCUMGR_TRANSPORT_UART_ASYNC_BUFS`
- **Type:** int
- **Default:** 2
- **Range:** 2–8
- **Help:** `"The asynchronous UART API requires registering RX buffers for received data; when the RX reaches the end of a buffer, it will send event requesting next buffer, to be able to receive data without stopping due to running out of buffer space. At least two buffers area required for smooth RX operation."`

#### `MCUMGR_TRANSPORT_UART_ASYNC_BUF_SIZE`
- **Type:** int
- **Default:** 64
- **Help:** `"The size of single buffer for asynchronous RX."`

These are lower-level async UART DMA buffers, distinct from the SMP-level framing. Naming pattern: `BUFS` (count) + `BUF_SIZE` (size of each).

---

## 4. mcumgr CLI (Go)

**Source:** [nRF Connect SDK — dfu_tools_mcumgr_cli.rst](https://github.com/nrfconnect/sdk-nrf/blob/main/doc/nrf/app_dev/bootloaders_dfu/dfu_tools_mcumgr_cli.rst)

Connection string parameters for serial transport:

- **`dev`** — `"The device name for the OS mcumgr is running"`
- **`baud`** — `"The communication speed, which must match the baudrate of the server."`
- **`mtu`** — `"Maximum Transmission Unit, the maximum protocol packet size."`

**Terminology used:** Just `mtu`. No separate line-length or buffer-count options exposed to the user. The Go CLI presents a single `mtu` value.

---

## 5. smpclient (Python — current codebase)

**Source:** [smpclient/transport/serial.py](https://github.com/intercreate/smpclient/blob/main/smpclient/transport/serial.py)

### `SMPSerialTransport.__init__` parameters:
- **`max_smp_encoded_frame_size`** (default 256) — `"The maximum size of an encoded SMP frame. The SMP server needs to have a buffer large enough to receive the encoded frame packets and to store the decoded frame."`
- **`line_length`** (default 128) — `"The maximum SMP packet size."`
- **`line_buffers`** (default 2) — `"The number of line buffers in the serial buffer."`

**Relationship (from source, lines 101-108):**
```python
if max_smp_encoded_frame_size < line_length * line_buffers:
    logger.error(...)
elif max_smp_encoded_frame_size != line_length * line_buffers:
    logger.warning(...)
```

**Terminology used:** `line_length`, `line_buffers`, `max_smp_encoded_frame_size`.

---

## Summary Comparison Table

| Implementation | "Line/Frame Size" | "Number of Buffers" | "Total Capacity / MTU" | Defaults |
|---|---|---|---|---|
| **SMP Protocol Spec** | "frame" (127 bytes) | (not named) | "packet" | 127B frames |
| **MCUboot KConfig** | `BOOT_MAX_LINE_INPUT_LEN` | `BOOT_LINE_BUFS` | `BOOT_SERIAL_MAX_RECEIVE_SIZE` | 128 × 8 = 1024 |
| **Zephyr UART KConfig** | (implicit, per SMP spec) | (not exposed) | `MCUMGR_TRANSPORT_UART_MTU` | 256 |
| **Zephyr Shell KConfig** | (implicit, per SMP spec) | `..._SHELL_RX_BUF_COUNT` | `MCUMGR_TRANSPORT_SHELL_MTU` | MTU=256, bufs=2 |
| **Zephyr Async UART** | `..._UART_ASYNC_BUF_SIZE` | `..._UART_ASYNC_BUFS` | (product) | 64 × 2 |
| **Zephyr General Transport** | `..._NETBUF_SIZE` | `..._NETBUF_COUNT` | (pool) | 384 × 4 |
| **mcumgr CLI (Go)** | (not separate) | (not separate) | `mtu` | user-specified |
| **smpclient (Python)** | `line_length` | `line_buffers` | `max_smp_encoded_frame_size` | 128 × 2 = 256 |

---

## Default Values: `line_length=128`, `line_buffers=2`

The smpclient defaults of `line_length=128` and `line_buffers=2` (yielding `max_smp_encoded_frame_size=256`) are chosen for broadest out-of-the-box compatibility across SMP server implementations.

### `line_length=128`

The SMP serial protocol spec defines the maximum frame size as 127 bytes of content plus a newline terminator:

> "Mcumgr packets sent over serial are fragmented into frames of 127 bytes or fewer."
>
> — [smp-console.md](https://github.com/apache/mynewt-mcumgr/blob/master/transport/smp-console.md)

MCUboot's default matches this exactly:

> `BOOT_MAX_LINE_INPUT_LEN` default: **128**
> "Maximum length of input serial port buffer (SMP serial transport uses fragments of **128-bytes**, this should not need to be changed unless a different value is used for the transport)."
>
> — [Kconfig.serial_recovery](https://github.com/mcu-tools/mcuboot/blob/main/boot/zephyr/Kconfig.serial_recovery)

A `line_length` of 128 is the protocol-defined fragment size. Any conforming SMP server must accept it.

### `line_buffers=2`

The product `128 × 2 = 256` matches the Zephyr MCUmgr UART transport default:

> `MCUMGR_TRANSPORT_UART_MTU` default: **256**
> "Maximum size of SMP frames sent and received over UART, in bytes."
>
> — [Kconfig.uart](https://github.com/zephyrproject-rtos/zephyr/blob/2ee0b2b3c0ad8b9af8335224e9fb1ec8afdcfa67/subsys/mgmt/mcumgr/transport/Kconfig.uart#L27-L33)

The Zephyr Shell transport also defaults to 256 with 2 RX buffers:

> `MCUMGR_TRANSPORT_SHELL_MTU` default: **256**
> `MCUMGR_TRANSPORT_SHELL_RX_BUF_COUNT` default: **2**
>
> — [Kconfig.shell](https://github.com/zephyrproject-rtos/zephyr/blob/2ee0b2b3c0ad8b9af8335224e9fb1ec8afdcfa67/subsys/mgmt/mcumgr/transport/Kconfig.shell)

With `line_buffers=2`, the total encoded frame size of 256 bytes will be accepted by any Zephyr MCUmgr UART or Shell server running default configuration. Higher values (e.g. MCUboot's `BOOT_LINE_BUFS=8`, yielding 1024) are possible when the server is known to support them, but would fail against a default Zephyr UART target.

---

## Analysis & Recommendations

### Observation 1: Two-parameter pattern is common in Zephyr
Across Zephyr's MCUmgr transport layer, the pattern of exposing both a **buffer size** and **buffer count** as separate KConfig options is well-established:
- Shell transport: `SHELL_MTU` + `SHELL_RX_BUF_COUNT`
- Async UART: `UART_ASYNC_BUF_SIZE` + `UART_ASYNC_BUFS`
- General netbufs: `NETBUF_SIZE` + `NETBUF_COUNT`

The UART transport is actually the odd one out, exposing only `UART_MTU` without a separate buffer count.

### Observation 2: MCUboot uses "line" terminology, Zephyr uses "buf"
- MCUboot: `LINE_INPUT_LEN`, `LINE_BUFS` — **"line"** based naming
- Zephyr: `BUF_SIZE` / `BUF_COUNT` / `RX_BUF_COUNT` — **"buf"** based naming
- smpclient: `line_length`, `line_buffers` — hybrid, using "line" for both

### Observation 3: The protocol spec uses "frame"
The SMP serial protocol specification calls the 127-byte chunks "frames". MCUboot calls them "lines". Zephyr calls them "bufs". The "line" terminology makes sense from the UART perspective (newline-terminated).

### Observation 4: "MTU" is overloaded
In the SMP ecosystem, "MTU" means different things:
- **Zephyr `UART_MTU`**: total reassembled encoded frame size (= smpclient's `max_smp_encoded_frame_size`)
- **mcumgr CLI `mtu`**: `"the maximum protocol packet size"` — same as above
- **BLE**: negotiated per-connection attribute MTU

smpmgr's current `--mtu` maps to `max_smp_encoded_frame_size` for serial, which is the same semantic as Zephyr's `UART_MTU`.

### Naming Recommendation for smpmgr CLI

| CLI Option | Maps to smpclient param | Rationale |
|---|---|---|
| `--line-length` | `line_length` | Matches smpclient naming; consistent with MCUboot's `BOOT_MAX_LINE_INPUT_LEN`. Hyphenated per CLI convention. |
| `--line-buffers` | `line_buffers` | Matches smpclient naming; consistent with MCUboot's `BOOT_LINE_BUFS` and Zephyr's `..._RX_BUF_COUNT` / `..._BUFS` pattern. |
| `--mtu` (deprecated for serial) | `max_smp_encoded_frame_size` (serial) / `mtu` (UDP) | Keep for UDP. For serial, emit deprecation warning and translate to `line_length=mtu, line_buffers=1` to preserve existing behavior. |

The `max_smp_encoded_frame_size` does **not** need its own CLI flag — it is always `line_length × line_buffers`. Exposing it separately would create a confusing three-way validation (as MCUboot's `BOOT_SERIAL_MAX_RECEIVE_SIZE` demonstrates — its help text has to explain the relationship).

### Alternative names considered

| Alternative | Pro | Con |
|---|---|---|
| `--frame-length` / `--frame-buffers` | Matches SMP protocol spec "frame" terminology | Doesn't match smpclient API; "frame" could be confused with the reassembled frame |
| `--rx-buf-size` / `--rx-buf-count` | Matches Zephyr Shell pattern | "rx" (receive) is only half the story; these affect TX too. Doesn't match smpclient. |
| `--fragment-size` / `--fragment-count` | MCUboot help text says "fragments of 128-bytes" | Not used as an option name anywhere |

**Final recommendation: `--line-length` and `--line-buffers`** — these match smpclient, align with MCUboot's established naming, and are descriptive ("lines" are newline-terminated UART fragments).

---

## Sources

All Zephyr sources pinned to commit `2ee0b2b3c0ad8b9af8335224e9fb1ec8afdcfa67`:

- [SMP Console Transport Spec (smp-console.md)](https://github.com/apache/mynewt-mcumgr/blob/master/transport/smp-console.md)
- [MCUboot Kconfig.serial_recovery](https://github.com/mcu-tools/mcuboot/blob/main/boot/zephyr/Kconfig.serial_recovery)
- [Zephyr MCUmgr transport/Kconfig](https://github.com/zephyrproject-rtos/zephyr/blob/2ee0b2b3c0ad8b9af8335224e9fb1ec8afdcfa67/subsys/mgmt/mcumgr/transport/Kconfig)
- [Zephyr MCUmgr transport/Kconfig.uart](https://github.com/zephyrproject-rtos/zephyr/blob/2ee0b2b3c0ad8b9af8335224e9fb1ec8afdcfa67/subsys/mgmt/mcumgr/transport/Kconfig.uart)
- [Zephyr MCUmgr transport/Kconfig.shell](https://github.com/zephyrproject-rtos/zephyr/blob/2ee0b2b3c0ad8b9af8335224e9fb1ec8afdcfa67/subsys/mgmt/mcumgr/transport/Kconfig.shell)
- [Zephyr MCUmgr transport/Kconfig.common](https://github.com/zephyrproject-rtos/zephyr/blob/2ee0b2b3c0ad8b9af8335224e9fb1ec8afdcfa67/subsys/mgmt/mcumgr/transport/Kconfig.common)
- [nRF Connect SDK — mcumgr CLI docs](https://github.com/nrfconnect/sdk-nrf/blob/main/doc/nrf/app_dev/bootloaders_dfu/dfu_tools_mcumgr_cli.rst)
- [smpclient/transport/serial.py](https://github.com/intercreate/smpclient/blob/main/smpclient/transport/serial.py)
