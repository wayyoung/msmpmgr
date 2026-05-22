"""Common CLI helpers from rich, typer, click, etc."""

import asyncio
import functools
import logging
import os
import tempfile
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import Any, Callable, Final, Type, TypedDict, TypeVar, assert_never

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
    mtu: int | None
    baudrate: int | None
    line_length: int | None
    line_buffers: int | None


DEFAULT_LINE_LENGTH: Final = 128
DEFAULT_LINE_BUFFERS: Final = 2


class SMPSerialTransportKwargs(TypedDict, total=False):
    max_smp_encoded_frame_size: int
    line_length: int
    line_buffers: int
    baudrate: int


def get_custom_smpclient(options: Options, smp_client_cls: Type[TSMPClient]) -> TSMPClient:
    """Return an `SMPClient` subclass to the chosen transport or raise `typer.Exit`."""
    if options.transport.port is not None:
        port = options.transport.port
        usb_transport = ':' in port
        if usb_transport:
            logger.info(f"Initializing SMPClient with the SMPUsbTransport, {port=}")
        else:
            logger.info(f"Initializing SMPClient with the SMPSerialTransport, {port=}")
        kwargs: SMPSerialTransportKwargs = {}
        match (options.line_length, options.line_buffers, options.mtu):
            case (int(), int(), None):
                kwargs['line_length'] = options.line_length
                kwargs['line_buffers'] = options.line_buffers
                kwargs['max_smp_encoded_frame_size'] = options.line_length * options.line_buffers
            case (int(), None, None):
                kwargs['line_length'] = options.line_length
                kwargs['line_buffers'] = DEFAULT_LINE_BUFFERS
                kwargs['max_smp_encoded_frame_size'] = options.line_length * DEFAULT_LINE_BUFFERS
            case (None, int(), None):
                kwargs['line_length'] = DEFAULT_LINE_LENGTH
                kwargs['line_buffers'] = options.line_buffers
                kwargs['max_smp_encoded_frame_size'] = DEFAULT_LINE_LENGTH * options.line_buffers
            case (None, None, int()):
                kwargs['line_length'] = options.mtu
                kwargs['line_buffers'] = 1
                kwargs['max_smp_encoded_frame_size'] = options.mtu
                typer.echo(
                    typer.style(
                        "Warning: --mtu is deprecated for serial transport."
                        " Use --line-length and --line-buffers instead."
                        f" --mtu {options.mtu} has been applied as"
                        f" --line-length {options.mtu} --line-buffers 1.",
                        fg=typer.colors.YELLOW,
                    )
                )
            case (None, None, None):
                kwargs['line_length'] = DEFAULT_LINE_LENGTH
                kwargs['line_buffers'] = DEFAULT_LINE_BUFFERS
                kwargs['max_smp_encoded_frame_size'] = DEFAULT_LINE_LENGTH * DEFAULT_LINE_BUFFERS
            case (_, _, int()):
                typer.echo("--mtu cannot be used with --line-length or --line-buffers.")
                raise typer.Exit(code=1)
            case _:
                assert_never((options.line_length, options.line_buffers, options.mtu))  # type: ignore[arg-type] # noqa: E501
        if options.baudrate is not None:
            kwargs['baudrate'] = options.baudrate
        if usb_transport:
            # VID:PID[:serialno] — pyusb-backed transport. Interface is
            # auto-discovered by mcumgr SMP subclass (0x60); any kernel
            # driver on the interface is detached for the session.
            return smp_client_cls(
                SMPUsbTransport(**kwargs), port, options.timeout
            )
        return smp_client_cls(SMPSerialTransport(**kwargs), port, options.timeout)
    elif options.transport.ble is not None:
        logger.info(f"Initializing SMPClient with the SMPBLETransport, {options.transport.ble=}")
        return smp_client_cls(
            SMPBLETransport(),
            options.transport.ble,
            options.timeout,
        )
    elif options.transport.ip is not None:
        logger.info(f"Initializing SMPClient with the SMPUDPTransport, {options.transport.ip=}")
        if options.mtu is not None:
            return smp_client_cls(
                SMPUDPTransport(mtu=options.mtu), options.transport.ip, options.timeout
            )
        else:
            return smp_client_cls(SMPUDPTransport(), options.transport.ip, options.timeout)
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


async def connect_with_spinner(smpclient: SMPClient) -> None:
    """Spin while connecting to the SMP Server; raises `typer.Exit` if connection fails."""
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}")
    ) as progress:
        connect_task_description = f"Connecting to {smpclient._address}..."
        connect_task = progress.add_task(description=connect_task_description, total=None)
        try:
            await smpclient.connect()
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


async def connect_no_spinner(smpclient: SMPClient, timeout_s: float = 2.0) -> None:
    """Connect to the SMP Server without UI; raises `typer.Exit` if connection fails."""
    try:
        await smpclient.connect(timeout_s)
        return
    except asyncio.TimeoutError:
        logger.error("Transport error: connection timeout")
    except SerialException as e:
        logger.error(f"Serial transport error: {e.__class__.__name__} - {e}")

    raise typer.Exit(code=1)


async def smp_request(
    smpclient: SMPClient,
    *args: Any,
    description: str | None = None,
    timeout_s: float | None = None,
) -> Any:
    """Send an SMP request with progress UI.

    Two call signatures are supported for backwards compatibility:
      - new: ``smp_request(smpclient, request, description=..., timeout_s=...)``
      - legacy (used by external plugins): ``smp_request(smpclient, options, request, description)``
        — ``options.timeout`` is used as the default ``timeout_s``.
    """
    options: Options | None = None
    if args and isinstance(args[0], Options):
        options = args[0]
        request = args[1] if len(args) > 1 else None
        if description is None and len(args) > 2:
            description = args[2]
        if timeout_s is None:
            timeout_s = options.timeout
    else:
        request = args[0] if args else None
        if description is None and len(args) > 1:
            description = args[1]
    if request is None:
        raise TypeError("smp_request: missing request argument")

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}")
    ) as progress:
        description = description or f"Waiting for response to {request.__class__.__name__}..."
        task = progress.add_task(description=description, total=None)
        try:
            r = await smpclient.request(request, timeout_s)
            progress.update(task, description=f"{description} OK", completed=True)
            return r
        except asyncio.TimeoutError:
            progress.update(task, description=f"{description} timeout", completed=True)
            logger.error("Timeout waiting for response")
            raise typer.Exit(code=1)
        except SMPBadStartDelimiter:
            progress.update(task, description=f"{description} SMP error", completed=True)
            logger.error("Is the device an SMP server?")
            raise typer.Exit(code=1)
        except OSError as e:
            progress.update(task, description=f"{description} OS error", completed=True)
            logger.error(f"Connection to device lost: {e.__class__.__name__} - {e}")
            raise typer.Exit(code=1)


async def smp_request_no_spinner(
    smpclient: SMPClient,
    options: "Options",
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


async def ping_connect(
    smpclient: SMPClient, options: "Options", timeout: int, nopg: bool
) -> bool:
    """Retry connect-then-echo-ping loop until success or timeout."""
    from smpclient.requests.os_management import EchoWrite

    tout: datetime | None = None
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
    lock_file = os.path.join(tempfile.gettempdir(), "msmpmgr_command.lock")

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
