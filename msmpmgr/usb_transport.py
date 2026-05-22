"""Custom USB transport for SMP over raw USB (pyusb).

This module provides SMPUsbTransport, which wraps a Suart/Susb connection
to communicate with devices via VID:PID:serialno addressing.
"""

import array
import logging
import platform
import threading
from typing import Optional

import usb
from smpclient.transport.serial import SMPSerialTransport

logger = logging.getLogger(__name__)


class SusbError(Exception):
    """Class for exceptions of Susb."""

    def __init__(self, msg: str, value: int = 0):
        super().__init__(msg, value)
        self.msg = msg
        self.value = value


class Susb:
    """Provide USB functionality.

    Instance Variables:
    _read_ep: pyUSB read endpoint for this interface
    _write_ep: pyUSB write endpoint for this interface
    """

    READ_ENDPOINT = 0x81
    WRITE_ENDPOINT = 0x1
    TIMEOUT_MS = 50

    # Vendor-class interface subclass byte used by firmware to mark its
    # mcumgr/SMP transport endpoint. When `interface` is None, Susb scans
    # the device's interfaces and picks the one carrying this subclass so
    # callers only need to specify VID:PID. Matches the convention used
    # by Zephyr's zeppery/boards/* (e.g., n32g4frre_stb, kumiho_rt).
    SMP_INTERFACE_SUBCLASS = 0x60

    def __init__(
        self,
        vendor: int = 0x18D1,
        product: int = 0x500F,
        interface: Optional[int] = None,
        serialname: Optional[str] = None,
    ):
        dev_g = usb.core.find(idVendor=vendor, idProduct=product, find_all=True)
        dev_list = list(dev_g)
        if dev_list is None:
            raise SusbError("USB device not found")

        dev = None
        if serialname:
            for d in dev_list:
                dev_serial = "PyUSB doesn't have a stable interface"
                try:
                    dev_serial = usb.util.get_string(d, d.iSerialNumber)
                except Exception:
                    pass
                if dev_serial == serialname:
                    dev = d
                    break
            if dev is None:
                raise SusbError("USB device(%s) not found" % (serialname,))
        else:
            try:
                dev = dev_list[0]
            except Exception:
                try:
                    dev = next(dev_list)
                except Exception:
                    raise SusbError("USB device %04x:%04x not found" % (vendor, product))

        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass

        cfg = dev.get_active_configuration()

        if interface is None:
            # Auto-discover the SMP interface by subclass.
            intf = usb.util.find_descriptor(
                cfg, bInterfaceSubClass=self.SMP_INTERFACE_SUBCLASS
            )
            if intf is None:
                raise SusbError(
                    "No interface with subclass 0x%02x (SMP/mcumgr) on "
                    "%04x:%04x" % (self.SMP_INTERFACE_SUBCLASS, vendor, product)
                )
            interface = intf.bInterfaceNumber
            logger.info(
                "Auto-selected interface %d (subclass 0x%02x) on %04x:%04x",
                interface, self.SMP_INTERFACE_SUBCLASS, vendor, product,
            )
        else:
            intf = usb.util.find_descriptor(cfg, bInterfaceNumber=interface)

        self._dev = dev
        self._cfg = cfg
        self._intf = intf

        if not intf:
            raise SusbError("Interface not found")

        if platform.system() == 'Linux':
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                dev.detach_kernel_driver(intf.bInterfaceNumber)
        usb.util.claim_interface(dev, intf.bInterfaceNumber)

        # Look up the bulk IN / OUT endpoints by direction within the
        # selected interface. The previous "interface_number + 0x81/0x01"
        # heuristic only worked when interfaces happened to be numbered
        # the same as their endpoint pair — not portable across firmwares
        # (e.g. Zephyr USBD allocates endpoint addresses globally).
        read_ep = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: (
                usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
                and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK
            ),
        )
        write_ep = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: (
                usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
                and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK
            ),
        )
        if read_ep is None or write_ep is None:
            raise SusbError(
                "Bulk endpoints not found on interface %d" % intf.bInterfaceNumber
            )
        self._read_ep = read_ep
        self._write_ep = write_ep
        logger.debug(
            "Endpoints on intf %d: IN=0x%02x OUT=0x%02x",
            intf.bInterfaceNumber, read_ep.bEndpointAddress, write_ep.bEndpointAddress,
        )

        # The endpoints may be HALTed if a kernel driver was previously
        # attached and we just detached it. Clear the stall so the device
        # will service transfers again. Without this the firmware's IN
        # endpoint stays stuck and SMP requests time out.
        for ep in (read_ep, write_ep):
            try:
                dev.clear_halt(ep.bEndpointAddress)
            except usb.core.USBError:
                pass

        # Drain any leftover bytes from a previous session — otherwise the
        # first read picks up the previous reply and msmpmgr fails with
        # SMPBadSequence on the next request.
        for _ in range(16):
            try:
                if not read_ep.read(read_ep.wMaxPacketSize, 5):
                    break
            except usb.core.USBError:
                break

    def close(self) -> None:
        try:
            usb.core.util.release_interface(self._dev, self._intf)
        except Exception:
            pass


class Suart:
    """Provide interface to serial usb endpoint."""

    def __init__(
        self,
        vendor: int = 0x18D1,
        product: int = 0x500F,
        interface: Optional[int] = None,
        serialname: Optional[str] = None,
    ):
        self._done = threading.Event()
        self._susb = Susb(
            vendor=vendor, product=product, interface=interface, serialname=serialname
        )
        self.port: Optional[str] = None
        self.out_waiting = 0

    def write(self, data) -> int:
        self.out_waiting = len(data)
        out = self._susb._write_ep.write(data, self._susb.TIMEOUT_MS)
        self.out_waiting -= out
        return out

    def read(self) -> Optional[array.array]:
        try:
            # Request the endpoint's max packet size so the URB is large
            # enough for a full HS bulk packet (512 B). Requesting less can
            # truncate a single transaction and report "babble" on Linux.
            mps = self._susb._read_ep.wMaxPacketSize or 64
            return self._susb._read_ep.read(mps, self._susb.TIMEOUT_MS)
        except Exception:
            return None

    def read_all(self) -> Optional[array.array]:
        return self.read()

    def flush(self) -> None:
        for _ in range(20):
            if self.read_all() is None:
                break

    def close(self) -> None:
        self.flush()
        self._susb.close()

    def open(self) -> None:
        pass


class SMPUsbTransport(SMPSerialTransport):
    """SMP transport over raw USB using pyusb.

    Subclasses SMPSerialTransport and overrides connect to use Suart
    instead of pyserial for USB devices addressed by VID:PID[:serialno].
    Accepts the same kwargs as SMPSerialTransport (e.g. max_smp_encoded_frame_size,
    line_length, line_buffers); baudrate is irrelevant for USB and is dropped.
    """

    def __init__(self, **kwargs):
        # baudrate is meaningful only for the pyserial backend; USB CDC /
        # raw bulk doesn't use a bus baud and pyserial would reject it.
        kwargs.pop('baudrate', None)
        super().__init__(**kwargs)

    async def connect(self, address: str, timeout_s: float) -> None:
        tokens = address.split(':')
        vid = int(tokens[0], 16)
        pid = int(tokens[1], 16)
        sn = None if len(tokens) == 2 else tokens[2]
        self._conn = Suart(vendor=vid, product=pid, serialname=sn)
        self._conn.port = address
        logger.info(f"USB connected to {address}")
