"""Read configured memory transfer rates from a raw SMBIOS table."""

from struct import unpack_from


def memory_speeds(table: bytes) -> tuple[int, ...]:
    speeds = set()
    offset = 0
    while offset + 4 <= len(table):
        kind, length = table[offset:offset + 2]
        end = offset + length
        if kind == 127 or length < 4 or end > len(table):
            break
        strings_end = table.find(b"\0\0", end)
        if strings_end < 0:
            break
        if kind == 17 and length >= 0x22:
            size = unpack_from("<H", table, offset + 0x0C)[0]
            speed = unpack_from("<H", table, offset + 0x20)[0]
            if speed == 0xFFFF:
                speed = unpack_from("<I", table, offset + 0x58)[0] & 0x7FFFFFFF if length >= 0x5C else 0
            if size and speed:
                speeds.add(speed)
        offset = strings_end + 2
    return tuple(sorted(speeds))
