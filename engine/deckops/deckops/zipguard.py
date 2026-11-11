"""Cheap checks on an untrusted .pptx before any parser sees it."""
import zipfile

MAX_ENTRIES = 5000
MAX_TOTAL = 500 * 1024 * 1024        # uncompressed bytes
MAX_RATIO = 200                      # per entry
MAX_INPUT = 100 * 1024 * 1024


class UnsafeArchive(Exception):
    pass


def check(path: str):
    import os
    if os.path.getsize(path) > MAX_INPUT:
        raise UnsafeArchive("input too large")
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise UnsafeArchive("not a zip / not a .pptx")
    with z:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES:
            raise UnsafeArchive("too many entries")
        total = 0
        for i in infos:
            if i.flag_bits & 0x1:
                raise UnsafeArchive("encrypted entry")
            if i.filename.startswith("/") or ".." in i.filename.split("/"):
                raise UnsafeArchive(f"bad path {i.filename!r}")
            total += i.file_size
            if i.compress_size and i.file_size / i.compress_size > MAX_RATIO and i.file_size > 1 << 20:
                raise UnsafeArchive(f"suspicious compression ratio on {i.filename}")
        if total > MAX_TOTAL:
            raise UnsafeArchive("uncompressed size too large")
        if "[Content_Types].xml" not in z.namelist():
            raise UnsafeArchive("missing [Content_Types].xml")
