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
    TIMEOUT_MS = 2

    def __init__(
        self,
        vendor: int = 0x18D1,
        product: int = 0x500F,
        interface: int = 1,
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
        intf = usb.util.find_descriptor(cfg, bInterfaceNumber=interface)

        self._dev = dev
        self._cfg = cfg
        self._intf = intf

        if not intf:
            raise SusbError("Interface not found")

        if platform.system() == 'Linux':
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                dev.detach_kernel_driver(intf.bInterfaceNumber)

        read_ep_number = intf.bInterfaceNumber + self.READ_ENDPOINT
        read_ep = usb.util.find_descriptor(intf, bEndpointAddress=read_ep_number)
        self._read_ep = read_ep

        write_ep_number = intf.bInterfaceNumber + self.WRITE_ENDPOINT
        write_ep = usb.util.find_descriptor(intf, bEndpointAddress=write_ep_number)
        self._write_ep = write_ep

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
        interface: int = 0,
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
            return self._susb._read_ep.read(64, self._susb.TIMEOUT_MS)
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
    """

    def __init__(
        self,
        max_smp_encoded_frame_size: int = 256,
        line_length: int = 128,
        line_buffers: int = 2,
    ):
        super().__init__(
            max_smp_encoded_frame_size=max_smp_encoded_frame_size,
            line_length=line_length,
            line_buffers=line_buffers,
        )

    async def connect(self, address: str, timeout_s: float) -> None:
        tokens = address.split(':')
        vid = int(tokens[0], 16)
        pid = int(tokens[1], 16)
        sn = None if len(tokens) == 2 else tokens[2]
        self._conn = Suart(vendor=vid, product=pid, serialname=sn)
        self._conn.port = address
        logger.info(f"USB connected to {address}")
