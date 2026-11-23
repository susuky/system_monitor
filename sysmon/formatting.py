"""Format sampled values for both the dashboard and text reports."""


def number(value: int | float | None, suffix: str = "", digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}{suffix}"


def size(value: int | float | None) -> str:
    if value is None:
        return "—"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PiB"


def memory_speed(speeds: tuple[int, ...] | list[int]) -> str:
    return f"{' / '.join(map(str, speeds))} MT/s" if speeds else "—"
