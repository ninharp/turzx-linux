"""turzx - Linux driver, theme player and editor for TURZX USB displays."""

__version__ = "0.2.0"

# where the protocol, theme format and drawing logic were reconstructed from
BASED_ON = "TURZX V3.1.0 (UsbMonitorL.exe 4.2.1.3)"


def version_info() -> list[tuple[str, str]]:
    """(label, value) rows for --version and the editor's About dialog."""
    import platform
    from importlib import metadata

    rows = [("turzx", __version__), ("Based on", BASED_ON),
            ("Python", platform.python_version()), ("System", f"{platform.system()} {platform.release()}")]
    for dist in ("pyusb", "Pillow", "PySide6", "fonttools", "psutil"):
        try:
            rows.append((dist, metadata.version(dist)))
        except metadata.PackageNotFoundError:
            rows.append((dist, "not installed"))
    return rows


def version_text() -> str:
    return "\n".join(f"{k + ':':<10} {v}" for k, v in version_info())
