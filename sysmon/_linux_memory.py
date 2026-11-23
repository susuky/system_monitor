"""Read the system RAM transfer rate from Linux firmware or AMD HSMP."""

from fcntl import ioctl
from pathlib import Path
from struct import Struct

from ._smbios import memory_speeds as _smbios_memory_speeds

_DMI = Path("/sys/firmware/dmi/tables/DMI")
_PLATFORM = Path("/sys/devices/platform")
_HSMP_DEVICE = Path("/dev/hsmp")
# Linux asm/amd_hsmp.h defines the ioctl number and hsmp_message layout.
_HSMP_IOCTL_CMD = 0xC02CF800
_HSMP_GET_FCLK_MCLK = 0x0F
_HSMP_MESSAGE = Struct("<IHH8IH2x")


def memory_speeds() -> tuple[int, ...]:
    try:
        speeds = _smbios_memory_speeds(_DMI.read_bytes())
    except OSError:
        speeds = ()
    return speeds or _hsmp_memory_speeds()


def _hsmp_memory_speeds() -> tuple[int, ...]:
    clocks = set()
    for source in _PLATFORM.glob("AMDI0097:*/mclk_input"):
        try:
            clocks.add(int(source.read_text()))
        except (OSError, ValueError):
            continue
    sockets = _PLATFORM.glob("amd_hsmp/socket[0-9]*")
    try:
        with _HSMP_DEVICE.open("rb") as device:
            for socket in sockets:
                index = int(socket.name.removeprefix("socket"))
                request = bytearray(_HSMP_MESSAGE.pack(_HSMP_GET_FCLK_MCLK, 0, 2, *(0,) * 8, index))
                try:
                    ioctl(device.fileno(), _HSMP_IOCTL_CMD, request)
                except OSError:
                    continue
                _, mclk = _HSMP_MESSAGE.unpack(request)[3:5]
                clocks.add(mclk)
    except OSError:
        pass
    return tuple(sorted(2 * clock for clock in clocks if clock > 0))
