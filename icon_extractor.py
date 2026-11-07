"""High-quality Windows icon extraction for Windows Autostart Manager.

Python 3.10+, Pillow, pywin32 (for resolving .lnk shortcuts).

Public API:
    extractor = IconExtractor(size=32)
    image = extractor.get_icon(r"C:\\Program Files\\SomeApp\\app.exe")

get_icon() always returns an RGBA PIL.Image of exactly ``size x size`` pixels.
On invalid/missing paths it returns a generated gear placeholder.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from PIL import Image, ImageDraw

PathLike = Union[str, os.PathLike[str]]

# ---------------------------------------------------------------------------
# Windows API declarations. These are kept module-local so importing this file
# on a non-Windows machine (e.g. while linting/building) remains safe.
# ---------------------------------------------------------------------------

HANDLE = ctypes.c_void_p
UINT = ctypes.c_uint
DWORD = ctypes.c_uint32
BOOL = ctypes.c_int
INT = ctypes.c_int

SHGFI_ICON = 0x00000100
SHGFI_LARGEICON = 0x00000000
SHGFI_USEFILEATTRIBUTES = 0x00000010
FILE_ATTRIBUTE_NORMAL = 0x00000080

DI_NORMAL = 0x0003
DIB_RGB_COLORS = 0
BI_RGB = 0


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", HANDLE),
        ("iIcon", INT),
        ("dwAttributes", DWORD),
        ("szDisplayName", ctypes.c_wchar * 260),
        ("szTypeName", ctypes.c_wchar * 80),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", DWORD),
        ("biWidth", INT),
        ("biHeight", INT),
        ("biPlanes", ctypes.c_ushort),
        ("biBitCount", ctypes.c_ushort),
        ("biCompression", DWORD),
        ("biSizeImage", DWORD),
        ("biXPelsPerMeter", INT),
        ("biYPelsPerMeter", INT),
        ("biClrUsed", DWORD),
        ("biClrImportant", DWORD),
    ]


class RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


@dataclass(frozen=True)
class ShortcutInfo:
    target_path: str = ""
    icon_location: str = ""
    working_directory: str = ""


def _expand_windows_variables(value: str) -> str:
    """Expand %NAME% and normal environment variables in a Windows path."""
    value = os.path.expandvars(os.path.expanduser(value.strip()))

    def replace_percent(match: re.Match[str]) -> str:
        name = match.group(1)
        return os.environ.get(name, os.environ.get(name.upper(), match.group(0)))

    return re.sub(r"%([^%]+)%", replace_percent, value)


def resolve_shortcut(shortcut_path: PathLike) -> Optional[ShortcutInfo]:
    """Read TargetPath/IconLocation from a Windows .lnk using WScript.Shell.

    COM is initialized on the calling thread because icon extraction commonly
    runs in the application's background scan thread.
    """
    if os.name != "nt":
        return None

    shortcut = os.path.abspath(os.fspath(shortcut_path))
    if not os.path.isfile(shortcut) or not shortcut.lower().endswith(".lnk"):
        return None

    pythoncom = None
    initialized_com = False
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore

        pythoncom.CoInitialize()
        initialized_com = True
        shell = win32com.client.Dispatch("WScript.Shell")
        link = shell.CreateShortCut(shortcut)
        return ShortcutInfo(
            target_path=_expand_windows_variables(str(link.TargetPath or "")),
            icon_location=_expand_windows_variables(str(link.IconLocation or "")),
            working_directory=_expand_windows_variables(str(link.WorkingDirectory or "")),
        )
    except Exception:
        return None
    finally:
        if initialized_com and pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


class IconExtractor:
    """Extract application icons and return consistent, anti-aliased PIL images."""

    _ICON_SIZES_TO_TRY = (256, 128, 64, 48, 32)

    def __init__(self, size: int = 32, cache_limit: int = 512) -> None:
        if size < 16:
            raise ValueError("Icon size must be at least 16 pixels")

        self.size = int(size)
        self.cache_limit = max(16, int(cache_limit))
        self._cache: OrderedDict[tuple[str, int, int], Image.Image] = OrderedDict()
        self._lock = threading.RLock()
        self._fallback = self._create_fallback_icon(self.size)

        # Compatibility with the original application: AutostartManager
        # calls icon_for() and the UI expects icon_path to be a PNG filename.
        self._file_cache: dict[str, str] = {}
        self._cache_dir = Path(tempfile.gettempdir()) / "windows_autostart_manager_icons"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._fallback_path = self._cache_dir / f"placeholder_{self.size}.png"
        if not self._fallback_path.exists():
            self._fallback.save(self._fallback_path, format="PNG")

        self._user32 = None
        self._shell32 = None
        self._gdi32 = None
        if os.name == "nt":
            self._load_windows_apis()

    # ------------------------------ public API -----------------------------

    def get_icon(self, path: Optional[PathLike]) -> Image.Image:
        """Return a high-quality ``size x size`` RGBA icon, never an empty image.

        ``path`` may be an EXE/DLL/script/ICO path or a Startup-folder .lnk.
        For shortcuts, a custom IconLocation is preferred, then TargetPath.
        """
        if not path:
            return self._fallback.copy()

        try:
            original_path = os.path.abspath(os.fspath(path).strip().strip('"'))
        except (TypeError, ValueError, OSError):
            return self._fallback.copy()

        cache_key = self._make_cache_key(original_path)
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache.move_to_end(cache_key)
                return cached.copy()

        result = self._extract_for_path(original_path)
        if result is None or not result.convert("RGBA").getchannel("A").getbbox():
            # Treat fully transparent/failed GDI conversions as extraction
            # failures too; never pass an invisible image to CTk.
            result = self._fallback.copy()
        else:
            result = self._resize(result)

        with self._lock:
            self._cache[cache_key] = result.copy()
            self._cache.move_to_end(cache_key)
            while len(self._cache) > self.cache_limit:
                self._cache.popitem(last=False)

        return result.copy()

    def icon_for(self, file_path: Optional[PathLike]) -> str:
        """Legacy API: return a PNG path for the existing CTk row widgets.

        The newer get_icon() API returns a PIL.Image. The original app's
        AutostartManager/UI contract expects icon_for() -> str, so this
        adapter preserves compatibility without changing those modules.
        Missing paths always map to a visible gear placeholder PNG.
        """
        if not file_path:
            return str(self._fallback_path)

        try:
            source = os.path.abspath(os.fspath(file_path).strip().strip('"'))
        except (TypeError, ValueError, OSError):
            return str(self._fallback_path)

        try:
            stat = os.stat(source)
            stamp = f"{stat.st_mtime_ns}-{stat.st_size}"
        except OSError:
            stamp = "missing"

        cache_key = os.path.normcase(os.path.normpath(source)) + "|" + stamp
        with self._lock:
            cached = self._file_cache.get(cache_key)
            if cached and os.path.isfile(cached):
                return cached

        digest = hashlib.sha1(cache_key.encode("utf-8", errors="ignore")).hexdigest()
        destination = self._cache_dir / f"{digest}_{self.size}.png"
        if not destination.is_file():
            try:
                self.get_icon(source).save(destination, format="PNG")
            except Exception:
                # Last-resort copy: a corrupt source must never produce an
                # empty image slot in the application's list.
                self._fallback.save(destination, format="PNG")

        result = str(destination) if destination.is_file() else str(self._fallback_path)
        with self._lock:
            self._file_cache[cache_key] = result
        return result

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._file_cache.clear()

    # ------------------------------ path logic -----------------------------

    def _make_cache_key(self, path: str) -> tuple[str, int, int]:
        normalized = os.path.normcase(os.path.normpath(path))
        try:
            stat = os.stat(path)
            return normalized, int(stat.st_mtime_ns), int(stat.st_size)
        except OSError:
            return normalized, 0, 0

    def _extract_for_path(self, path: str) -> Optional[Image.Image]:
        if not os.path.exists(path):
            return None

        ext = Path(path).suffix.lower()
        if ext == ".lnk":
            return self._extract_shortcut(path)

        # ICO is already a raster/icon container; Pillow can select its largest
        # embedded frame, avoiding the 16x16 frame selected by some shell APIs.
        if ext == ".ico":
            image = self._load_ico(path)
            if image is not None:
                return image

        # Discord commonly registers Update.exe, while the product icon is in
        # app-x.y.z\Discord.exe. Prefer the versioned executable when present.
        for candidate in self._discord_icon_candidates(path):
            image = self._extract_native(candidate, icon_index=0)
            if image is not None:
                return image

        image = self._extract_native(path, icon_index=0)
        if image is not None:
            return image

        # SHGetFileInfo also supplies the shell's large associated icon for
        # files which do not expose a normal executable icon resource.
        return self._extract_shell_icon(path)

    def _extract_shortcut(self, shortcut_path: str) -> Optional[Image.Image]:
        info = resolve_shortcut(shortcut_path)
        if info is None:
            return None

        # The shortcut may specify a separate icon, e.g. "shell32.dll, -45".
        icon_path, icon_index = self._parse_icon_location(info.icon_location, shortcut_path)
        if icon_path and os.path.exists(icon_path):
            if Path(icon_path).suffix.lower() == ".ico":
                image = self._load_ico(icon_path)
                if image is not None:
                    return image
            image = self._extract_native(icon_path, icon_index=icon_index)
            if image is not None:
                return image
            image = self._extract_shell_icon(icon_path)
            if image is not None:
                return image

        target = info.target_path.strip().strip('"')
        if target and not os.path.isabs(target) and info.working_directory:
            target = os.path.join(info.working_directory, target)
        if target and os.path.exists(target):
            return self._extract_for_path(target)

        # Invalid/deleted shortcut target: caller receives the gear placeholder.
        return None

    @staticmethod
    def _parse_icon_location(value: str, shortcut_path: str) -> tuple[str, int]:
        value = _expand_windows_variables(value or "").strip()
        if not value:
            return "", 0

        icon_index = 0
        path_part = value
        head, comma, tail = value.rpartition(",")
        if comma and re.fullmatch(r"\s*-?\d+\s*", tail):
            path_part = head.strip()
            try:
                icon_index = int(tail.strip())
            except ValueError:
                icon_index = 0

        path_part = path_part.strip().strip('"').strip("'")
        if path_part and not os.path.isabs(path_part):
            path_part = os.path.join(os.path.dirname(shortcut_path), path_part)
        return os.path.normpath(path_part) if path_part else "", icon_index

    @staticmethod
    def _discord_icon_candidates(path: str) -> list[str]:
        file_name = os.path.basename(path).lower()
        if file_name != "update.exe":
            return []

        parent = Path(path).parent
        try:
            candidates = [p for p in parent.glob("app-*/Discord.exe") if p.is_file()]
        except OSError:
            candidates = []

        # Use the newest installed version if multiple app-x.y.z folders exist.
        try:
            candidates.sort(key=lambda p: (p.stat().st_mtime_ns, p.name.lower()), reverse=True)
        except OSError:
            candidates.sort(key=lambda p: p.name.lower(), reverse=True)
        return [str(p) for p in candidates]

    # ----------------------------- image loading ---------------------------

    def _load_ico(self, path: str) -> Optional[Image.Image]:
        try:
            with Image.open(path) as source:
                image = None
                # IcoImagePlugin exposes all available embedded sizes.
                ico = getattr(source, "ico", None)
                if ico is not None:
                    sizes = list(ico.sizes())
                    if sizes:
                        largest = max(sizes, key=lambda item: item[0] * item[1])
                        image = ico.getimage(largest)
                if image is None:
                    image = source.copy()
                return image.convert("RGBA")
        except Exception:
            return None

    def _resize(self, image: Image.Image) -> Image.Image:
        image = image.convert("RGBA")
        resampling = getattr(Image, "Resampling", Image)
        return image.resize((self.size, self.size), resampling.LANCZOS)

    # ----------------------------- Windows APIs ----------------------------

    def _load_windows_apis(self) -> None:
        try:
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

            self._user32.PrivateExtractIconsW.argtypes = [
                ctypes.c_wchar_p, INT, INT, INT,
                ctypes.POINTER(HANDLE), ctypes.POINTER(UINT), UINT, UINT,
            ]
            self._user32.PrivateExtractIconsW.restype = UINT
            self._user32.DestroyIcon.argtypes = [HANDLE]
            self._user32.DestroyIcon.restype = BOOL
            self._user32.DrawIconEx.argtypes = [
                HANDLE, INT, INT, HANDLE, INT, INT, UINT, HANDLE, UINT,
            ]
            self._user32.DrawIconEx.restype = BOOL

            self._shell32.SHGetFileInfoW.argtypes = [
                ctypes.c_wchar_p, DWORD, ctypes.POINTER(SHFILEINFOW),
                UINT, UINT,
            ]
            self._shell32.SHGetFileInfoW.restype = ctypes.c_size_t

            self._gdi32.CreateCompatibleDC.argtypes = [HANDLE]
            self._gdi32.CreateCompatibleDC.restype = HANDLE
            self._gdi32.CreateDIBSection.argtypes = [
                HANDLE, ctypes.POINTER(BITMAPINFO), UINT,
                ctypes.POINTER(ctypes.c_void_p), HANDLE, DWORD,
            ]
            self._gdi32.CreateDIBSection.restype = HANDLE
            self._gdi32.SelectObject.argtypes = [HANDLE, HANDLE]
            self._gdi32.SelectObject.restype = HANDLE
            self._gdi32.DeleteObject.argtypes = [HANDLE]
            self._gdi32.DeleteObject.restype = BOOL
            self._gdi32.DeleteDC.argtypes = [HANDLE]
            self._gdi32.DeleteDC.restype = BOOL
        except Exception:
            # Keep the application functional with the generated placeholder
            # even if a restricted Windows environment blocks an API binding.
            self._user32 = self._shell32 = self._gdi32 = None

    def _extract_native(self, path: str, icon_index: int = 0) -> Optional[Image.Image]:
        if not self._user32 or not os.path.exists(path):
            return None

        # PrivateExtractIconsW asks Windows for the largest available icon in
        # the icon group. If the binary contains a 256px resource it can be
        # selected directly instead of stretching a 16px small icon.
        for requested_size in self._ICON_SIZES_TO_TRY:
            handles = (HANDLE * 1)()
            icon_ids = (UINT * 1)()
            try:
                count = self._user32.PrivateExtractIconsW(
                    path, int(icon_index), requested_size, requested_size,
                    handles, icon_ids, 1, 0,
                )
                hicon = handles[0] if count else None
                if hicon:
                    try:
                        image = self._render_hicon(hicon, requested_size)
                        if image is not None:
                            return image
                    finally:
                        self._user32.DestroyIcon(hicon)
            except Exception:
                continue

        return None

    def _extract_shell_icon(self, path: str) -> Optional[Image.Image]:
        """Use SHGetFileInfoW(SHGFI_ICON | SHGFI_LARGEICON) as a shell fallback."""
        if not self._shell32 or not self._user32 or not os.path.exists(path):
            return None

        info = SHFILEINFOW()
        try:
            result = self._shell32.SHGetFileInfoW(
                path,
                0,
                ctypes.byref(info),
                ctypes.sizeof(info),
                SHGFI_ICON | SHGFI_LARGEICON,
            )
            if not result or not info.hIcon:
                return None
            try:
                return self._render_hicon(info.hIcon, max(32, self.size))
            finally:
                self._user32.DestroyIcon(info.hIcon)
        except Exception:
            return None

    def _render_hicon(self, hicon: HANDLE, render_size: int) -> Optional[Image.Image]:
        """Render HICON into a top-down 32-bit DIB and preserve transparency."""
        if not self._gdi32 or not self._user32 or not hicon:
            return None

        width = height = max(16, int(render_size))
        byte_count = width * height * 4
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # top-down DIB: no vertical flip needed
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bmi.bmiHeader.biSizeImage = byte_count

        hdc = self._gdi32.CreateCompatibleDC(None)
        if not hdc:
            return None

        bits = ctypes.c_void_p()
        bitmap = self._gdi32.CreateDIBSection(
            hdc, ctypes.byref(bmi), DIB_RGB_COLORS,
            ctypes.byref(bits), None, 0,
        )
        if not bitmap or not bits.value:
            self._gdi32.DeleteDC(hdc)
            return None

        old_object = self._gdi32.SelectObject(hdc, bitmap)
        try:
            # First render over transparent black. Many modern 32-bit icons
            # preserve a usable alpha channel in this pass.
            ctypes.memset(bits, 0, byte_count)
            if not self._user32.DrawIconEx(
                hdc, 0, 0, hicon, width, height, 0, None, DI_NORMAL
            ):
                return None
            black = ctypes.string_at(bits, byte_count)

            alpha_values = black[3::4]
            if any(alpha_values):
                return self._image_from_bgra_with_alpha(black, width, height)

            # Some legacy GDI icon resources do not write the alpha byte.
            # Render a second copy over white and reconstruct coverage from
            # the difference between black/white composites. This retains
            # transparent edges instead of returning a black square.
            ctypes.memset(bits, 0xFF, byte_count)
            if not self._user32.DrawIconEx(
                hdc, 0, 0, hicon, width, height, 0, None, DI_NORMAL
            ):
                return Image.frombytes("RGBA", (width, height), black, "raw", "BGRA")
            white = ctypes.string_at(bits, byte_count)
            return self._image_from_black_white(black, white, width, height)
        except Exception:
            return None
        finally:
            if old_object:
                self._gdi32.SelectObject(hdc, old_object)
            self._gdi32.DeleteObject(bitmap)
            self._gdi32.DeleteDC(hdc)

    @staticmethod
    def _image_from_bgra_with_alpha(raw: bytes, width: int, height: int) -> Image.Image:
        # GDI may provide premultiplied color channels. Convert them back to
        # straight-alpha RGBA, which is what Pillow/Tk expect.
        out = bytearray(width * height * 4)
        for src in range(0, len(raw), 4):
            blue, green, red, alpha = raw[src:src + 4]
            dst = src
            if alpha == 0:
                out[dst:dst + 4] = b"\x00\x00\x00\x00"
            else:
                if alpha < 255:
                    red = min(255, (red * 255 + alpha // 2) // alpha)
                    green = min(255, (green * 255 + alpha // 2) // alpha)
                    blue = min(255, (blue * 255 + alpha // 2) // alpha)
                out[dst:dst + 4] = bytes((red, green, blue, alpha))
        return Image.frombytes("RGBA", (width, height), bytes(out))

    @staticmethod
    def _image_from_black_white(
        black: bytes, white: bytes, width: int, height: int
    ) -> Image.Image:
        out = bytearray(width * height * 4)
        for src in range(0, len(black), 4):
            # DIB storage is BGRA. Alpha is reconstructed using the average
            # white-minus-black difference to reduce channel rounding noise.
            b0, g0, r0 = black[src], black[src + 1], black[src + 2]
            b1, g1, r1 = white[src], white[src + 1], white[src + 2]
            delta = ((b1 - b0) + (g1 - g0) + (r1 - r0)) / 3.0
            alpha = max(0, min(255, int(round(255.0 - delta))))
            if alpha <= 2:
                red = green = blue = 0
                alpha = 0
            else:
                red = max(0, min(255, int(round(r0 * 255.0 / alpha))))
                green = max(0, min(255, int(round(g0 * 255.0 / alpha))))
                blue = max(0, min(255, int(round(b0 * 255.0 / alpha))))
            out[src:src + 4] = bytes((red, green, blue, alpha))
        return Image.frombytes("RGBA", (width, height), bytes(out))

    # ------------------------------ placeholder ----------------------------

    @staticmethod
    def _create_fallback_icon(size: int) -> Image.Image:
        """Create an anti-aliased gear icon; never use a blank Tk image."""
        scale = 4
        canvas_size = max(64, size * scale)
        image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        margin = int(canvas_size * 0.07)
        radius = int(canvas_size * 0.20)
        center = canvas_size // 2
        tile_box = (margin, margin, canvas_size - margin, canvas_size - margin)
        tile_color = (34, 43, 56, 255)
        outline_color = (74, 91, 113, 255)
        draw.rounded_rectangle(
            tile_box,
            radius=radius,
            fill=tile_color,
            outline=outline_color,
            width=max(2, scale),
        )

        outer = canvas_size * 0.30
        inner = canvas_size * 0.235
        points = []
        # Sixteen alternating radii create a clean eight-tooth gear silhouette.
        for i in range(32):
            angle = -90 + i * (360 / 32)
            use_outer = (i % 4 in (0, 1))
            r = outer if use_outer else inner
            import math
            points.append((
                center + math.cos(math.radians(angle)) * r,
                center + math.sin(math.radians(angle)) * r,
            ))
        draw.polygon(points, fill=(166, 184, 207, 255))
        hole = canvas_size * 0.105
        draw.ellipse(
            (center - hole, center - hole, center + hole, center + hole),
            fill=tile_color,
        )
        # Small blue center accent makes the placeholder clearly intentional.
        dot = canvas_size * 0.035
        draw.ellipse(
            (center - dot, center - dot, center + dot, center + dot),
            fill=(94, 170, 255, 255),
        )

        resampling = getattr(Image, "Resampling", Image)
        return image.resize((size, size), resampling.LANCZOS)
