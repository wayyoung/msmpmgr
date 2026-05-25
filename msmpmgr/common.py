"""Common CLI helpers from rich, typer, click, etc."""

import asyncio
import functools
import logging
import os
import tempfile
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import Any, Callable, Final, Type, TypedDict, TypeVar

import typer
from filelock import FileLock
from rich.progress import Progress, SpinnerColumn, TextColumn
from serial import SerialException
from smp.exceptions import SMPBadStartDelimiter
from smpclient import SMPClient
from smpclient.generics import SMPRequest, TEr1, TEr2, TRep
from smpclient.transport.ble import SMPBLETransport
from smpclient.transport.serial import SMPSerialTransport
from smpclient.transport.udp import SMPUDPTransport

from msmpmgr.usb_transport import SMPUsbTransport

logger = logging.getLogger(__name__)

TSMPClient = TypeVar(
    "TSMPClient",
    bound=SMPClient,
)


@dataclass(frozen=True)
class TransportDefinition:
    port: str | None
    ble: str | None
    ip: str | None


@dataclass(frozen=True)
class Options:
    timeout: float
    transport: TransportDefinition
    # The following have defaults so external plugins (e.g. mgmt_ex_group.py
    # in ~/opt/msmpmgr/plugins) that construct Options(timeout=..., transport=...,
    # mtu=..., baudrate=...) without passing the newer fields keep working
    # without modification.
    mtu: int | None = None
    baudrate: int | None = None
    line_length: int | None = None
    line_buffers: int | None = None


DEFAULT_LINE_LENGTH: Final = 128
DEFAULT_LINE_BUFFERS: Final = 2

# Default transport timeouts. BLE link setup (scan + GATT connect) and BLE
# request round-trips routinely exceed the wired default, so BLE gets a longer
# default. Used by main.py (Options.timeout) and the connect helpers below so a
# bare connect_no_spinner(smpclient) — as called by external plugins — still
# waits long enough on BLE.
DEFAULT_TIMEOUT_S: Final = 2.0
DEFAULT_BLE_TIMEOUT_S: Final = 30.0


class SMPSerialTransportKwargs(TypedDict, total=False):
    max_smp_encoded_frame_size: int
    line_length: int
    line_buffers: int
    baudrate: int


def get_custom_smpclient(options: Options, smp_client_cls: Type[TSMPClient]) -> TSMPClient:
    """Return an `SMPClient` subclass to the chosen transport or raise `typer.Exit`."""
    if options.transport.port is not None:
        port = options.transport.port
        kwargs: SMPSerialTransportKwargs = {}
        if options.mtu is not None:
            kwargs['max_smp_encoded_frame_size'] = options.mtu
            kwargs['line_length'] = options.mtu
            kwargs['line_buffers'] = 1

        if ':' in port:
            # VID:PID[:serialno] → custom raw USB transport.
            # Thread options.timeout through to SMPClient so upstream-style
            # callers (e.g. image_management.smp_request without explicit
            # timeout) get the user-configured value.  The MCUMgr-params
            # probe inside SMPClient.connect() uses the same timeout — its
            # cosmetic "Timeout waiting for MCUMgr parameters" log lines
            # are suppressed by the filter installed in Susb.__init__.
            logger.info(f"Initializing SMPClient with SMPUsbTransport, {port=}")
            return smp_client_cls(SMPUsbTransport(**kwargs), port, options.timeout)
        else:
            # /dev/ttyACMx → standard serial transport
            logger.info(f"Initializing SMPClient with SMPSerialTransport, {port=}")
            if options.baudrate is not None:
                kwargs['baudrate'] = options.baudrate
            return smp_client_cls(SMPSerialTransport(**kwargs), port)
    elif options.transport.ble is not None:
        logger.info(f"Initializing SMPClient with the SMPBLETransport, {options.transport.ble=}")
        return smp_client_cls(
            SMPBLETransport(),
            options.transport.ble,
        )
    elif options.transport.ip is not None:
        logger.info(f"Initializing SMPClient with the SMPUDPTransport, {options.transport.ip=}")
        if options.mtu is not None:
            return smp_client_cls(SMPUDPTransport(mtu=options.mtu), options.transport.ip)
        else:
            return smp_client_cls(SMPUDPTransport(), options.transport.ip)
    else:
        typer.echo(
            f"A transport option is required; "
            f"one of [{', '.join(map(lambda x: '--' + x.name, fields(options.transport)))}]."
        )
        typer.echo("See msmpmgr --help.")
        raise typer.Exit(code=1)


def get_smpclient(options: Options) -> SMPClient:
    """Return an `SMPClient` to the chosen transport or raise `typer.Exit`."""
    return get_custom_smpclient(options, SMPClient)


async def connect_with_spinner(smpclient: SMPClient, timeout_s: float | None = None) -> None:
    """Spin while connecting to the SMP Server; raises `typer.Exit` if connection fails."""
    from smpclient.exceptions import SMPBadSequence
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}")
    ) as progress:
        connect_task_description = f"Connecting to {smpclient._address}..."
        connect_task = progress.add_task(description=connect_task_description, total=None)
        try:
            # SMPClient.connect() treats timeout_s=None as "use the value the
            # client was constructed with" — that's the right behavior here.
            # The BLE client is constructed without an explicit timeout, so give
            # it the longer BLE default rather than smpclient's wired default.
            if timeout_s is None and isinstance(smpclient._transport, SMPBLETransport):
                timeout_s = DEFAULT_BLE_TIMEOUT_S
            if timeout_s is None:
                await smpclient.connect()
            else:
                await smpclient.connect(timeout_s)
            progress.update(
                connect_task, description=f"{connect_task_description} OK", completed=True
            )
            return
        except SMPBadSequence:
            # The MCUMgr-params probe inside SMPClient.connect() can pick up
            # a stale response from a previous SMP session whose sequence
            # doesn't match. Drain the transport and treat as connected —
            # the actual SMP traffic the caller is about to send will use
            # a fresh sequence and work normally.
            conn = getattr(smpclient._transport, "_conn", None)
            if conn is not None and hasattr(conn, "flush"):
                conn.flush()
            progress.update(
                connect_task, description=f"{connect_task_description} OK", completed=True
            )
            return
        except asyncio.TimeoutError:
            logger.error("Transport error: connection timeout")
        except SerialException as e:
            logger.error(f"Serial transport error: {e.__class__.__name__} - {e}")

        progress.update(
            connect_task, description=f"{connect_task_description} error", completed=True
        )
        raise typer.Exit(code=1)


async def connect_no_spinner(smpclient: SMPClient, timeout_s: float | None = None) -> None:
    """Connect to the SMP Server without UI; raises `typer.Exit` if connection fails."""
    from smpclient.exceptions import SMPBadSequence
    if timeout_s is None:
        # No explicit timeout (e.g. external plugins call connect_no_spinner(
        # smpclient)); pick a transport-aware default so BLE gets 30s while
        # wired transports keep 2.0s.
        timeout_s = (
            DEFAULT_BLE_TIMEOUT_S
            if isinstance(smpclient._transport, SMPBLETransport)
            else DEFAULT_TIMEOUT_S
        )
    try:
        await smpclient.connect(timeout_s)
        return
    except SMPBadSequence:
        # Stale frame from a previous session leaked into the probe inside
        # SMPClient.connect(); drain the transport and proceed.
        conn = getattr(smpclient._transport, "_conn", None)
        if conn is not None and hasattr(conn, "flush"):
            conn.flush()
        return
    except asyncio.TimeoutError:
        logger.error("Transport error: connection timeout")
    except SerialException as e:
        logger.error(f"Serial transport error: {e.__class__.__name__} - {e}")

    raise typer.Exit(code=1)


# ----------------------------------------------------------------------------
# Fork divergence from upstream smpmgr (notify on next merge):
#
# Upstream commit 688cef6 ("refactor: defer default timeout to SMPClient and
# unused argument 'options'") removed the `options: Options` parameter from
# `smp_request`, `smp_request_no_spinner`, and `ping_connect`, and instead
# threads `options.timeout` into the SMPClient constructor at connect time.
#
# This fork intentionally keeps `options` in those signatures because external
# plugins (notably ~/opt/msmpmgr/plugins/mgmt_ex_group.py) call them with the
# legacy signature. Dropping `options` would break every external plugin call
# site without warning.
#
# When merging upstream changes:
#   - Do NOT take upstream's signature change for these three functions.
#   - When porting upstream call sites (e.g. main.py), re-add `options` as
#     the second positional argument.
#   - If external plugins are ever migrated off the legacy signature, this
#     divergence can be retired and the upstream signature adopted.
# ----------------------------------------------------------------------------
async def smp_request(
    smpclient: SMPClient,
    *args: Any,
    description: str | None = None,
    timeout_s: float | None = None,
) -> Any:
    """Send an SMP request with progress UI.

    Accepts two call signatures for backwards compatibility:
      - legacy fork (used by ~/opt/msmpmgr/plugins/mgmt_ex_group.py and the
        original fork code): ``smp_request(smpclient, options, request,
        description=..., timeout_s=...)`` — ``options.timeout`` provides the
        default ``timeout_s``.
      - upstream new (used by msmpmgr/image_management.py et al. after the
        squash-merge of intercreate/smpmgr main): ``smp_request(smpclient,
        request, description=..., timeout_s=...)``.

    The shape is disambiguated by whether the second positional argument is
    an :class:`Options` instance.
    """
    options: Options | None = None
    if args and isinstance(args[0], Options):
        options = args[0]
        request = args[1] if len(args) > 1 else None
        if description is None and len(args) > 2:
            description = args[2]
    else:
        request = args[0] if args else None
        if description is None and len(args) > 1:
            description = args[1]
    if request is None:
        raise TypeError("smp_request: missing request argument")
    if timeout_s is None and options is not None:
        timeout_s = options.timeout

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}")
    ) as progress:
        description = description or f"Waiting for response to {request.__class__.__name__}..."
        task = progress.add_task(description=description, total=None)

        # Resync on SMPBadSequence. When a previous SMP session left an
        # unread response in the device's IN buffer, the first frame this
        # session reads is the stale one (sequence != ours). Drain the
        # transport and retry once; the next frame is our actual response.
        from smpclient.exceptions import SMPBadSequence
        for attempt in range(2):
            try:
                r = await smpclient.request(request, timeout_s)
                progress.update(task, description=f"{description} OK", completed=True)
                return r
            except SMPBadSequence as e:
                logger.warning(
                    "Stale response from previous SMP session (%s); "
                    "draining and retrying", e
                )
                # Reach into the transport to clear any further stale frames.
                conn = getattr(smpclient._transport, "_conn", None)
                if conn is not None and hasattr(conn, "flush"):
                    conn.flush()
                if attempt == 1:
                    progress.update(task, description=f"{description} SMP error",
                                    completed=True)
                    raise typer.Exit(code=1)
                continue
            except asyncio.TimeoutError:
                progress.update(task, description=f"{description} timeout",
                                completed=True)
                logger.error("Timeout waiting for response")
                raise typer.Exit(code=1)
            except SMPBadStartDelimiter:
                progress.update(task, description=f"{description} SMP error",
                                completed=True)
                logger.error("Is the device an SMP server?")
                raise typer.Exit(code=1)
            except OSError as e:
                progress.update(task, description=f"{description} OS error",
                                completed=True)
                logger.error(f"Connection to device lost: {e.__class__.__name__} - {e}")
                raise typer.Exit(code=1)


async def smp_request_no_spinner(
    smpclient: SMPClient,
    options: Options,
    request: SMPRequest[TRep, TEr1, TEr2],
    description: str | None = None,
    retry: int = 0,
) -> TRep | TEr1 | TEr2:
    """Send an SMP request without UI, with optional retry support."""
    r = None
    for i in range(retry + 1):
        try:
            if i > 0:
                await connect_no_spinner(smpclient, options.timeout)
            r = await smpclient.request(request, options.timeout)
            break
        except Exception as e:
            if i < retry:
                try:
                    await asyncio.wait_for(smpclient.disconnect(), timeout=3)
                except Exception:
                    pass
            else:
                if isinstance(e, asyncio.TimeoutError):
                    logger.error(f"Timeout waiting for response, rcnt: {i}")
                    raise typer.Exit(code=1)
                elif isinstance(e, SMPBadStartDelimiter):
                    logger.error(f"Is the device an SMP server? rcnt: {i}")
                    raise typer.Exit(code=1)
                elif isinstance(e, OSError):
                    logger.error(
                        f"Connection to device lost: {e.__class__.__name__} - {e}, rcnt: {i}"
                    )
                    raise typer.Exit(code=1)
                else:
                    logger.error(f"rcnt: {i}, {e}")
                    raise e
    return r  # type: ignore[return-value]


async def ping_connect(smpclient: SMPClient, options: Options, timeout: int, nopg: bool) -> bool:
    """Retry connect-then-echo-ping loop until success or timeout."""
    from smpclient.requests.os_management import EchoWrite

    tout = None
    if nopg:
        while tout is None or datetime.now() < tout:
            if tout is None:
                tout = datetime.now() + timedelta(seconds=timeout)
            try:
                await connect_no_spinner(smpclient)
                r = await smp_request_no_spinner(smpclient, options, EchoWrite(d="ping"))  # type: ignore
                if str(r.r) == "ping":
                    return True
            except Exception:
                try:
                    await asyncio.wait_for(smpclient.disconnect(), timeout=3)
                except Exception:
                    pass
                await asyncio.sleep(1)
        return False
    else:
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}")
        ) as progress:
            connect_task_description = "Connecting..."
            connect_task = progress.add_task(description=connect_task_description, total=None)
            while tout is None or datetime.now() < tout:
                if tout is None:
                    tout = datetime.now() + timedelta(seconds=timeout)
                try:
                    await connect_no_spinner(smpclient)
                    r = await smp_request_no_spinner(smpclient, options, EchoWrite(d="ping"))  # type: ignore
                    if str(r.r) == "ping":
                        progress.update(
                            connect_task,
                            description=f"{connect_task_description} success",
                            completed=True,
                        )
                        return True
                except Exception:
                    try:
                        await asyncio.wait_for(smpclient.disconnect(), timeout=3)
                    except Exception:
                        pass
                    await asyncio.sleep(1)
            progress.update(
                connect_task, description=f"{connect_task_description} failed!!", completed=True
            )
            return False


def with_multiprocess_lock(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to ensure only one instance of a command can run at a time across processes."""
    lock_file = os.path.join(tempfile.gettempdir(), "smpmgr_command.lock")

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        logger.debug(f"Acquiring lock for {func.__name__}")
        with FileLock(lock_file, timeout=1):
            logger.debug(f"Lock acquired for {func.__name__}")
            try:
                return func(*args, **kwargs)
            finally:
                logger.debug(f"Releasing lock for {func.__name__}")

    return wrapper
