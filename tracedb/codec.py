"""Block codec. Speed over space: zlib level 1 (or zstd if installed), raw if incompressible."""
import zlib
try:
    import zstandard as _z
    _zc, _zd = _z.ZstdCompressor(level=3), _z.ZstdDecompressor()
except Exception:  # pragma: no cover
    _z = None

BLOCK = 1 << 20  # 1 MiB raw blocks: the unit of range reads


def compress(raw: bytes) -> tuple[str, bytes]:
    if len(raw) < 64:
        return "raw", raw
    c = _zc.compress(raw) if _z else zlib.compress(raw, 1)
    if len(c) >= len(raw) * 0.92:
        return "raw", raw
    return ("zstd" if _z else "zlib"), c


def decompress(codec: str, data: bytes) -> bytes:
    if codec == "raw":
        return data
    if codec == "zlib":
        return zlib.decompress(data)
    if codec == "zstd":
        if _z is None:
            raise RuntimeError("zstd block but zstandard not installed")
        return _zd.decompress(data)
    raise ValueError(codec)
