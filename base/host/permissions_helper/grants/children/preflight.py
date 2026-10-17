import ctypes
import json
import sys
from pathlib import Path

RESULT_WORDS = {0: "granted", 1: "denied", 2: "not-determined"}

CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
TCC_FRAMEWORK = "/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC"


class Libraries:
    def __init__(self) -> None:
        self.core_foundation = ctypes.CDLL(CORE_FOUNDATION)
        self.core_foundation.CFStringCreateWithCString.restype = ctypes.c_void_p
        self.core_foundation.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self.tcc = ctypes.CDLL(TCC_FRAMEWORK)
        self.tcc.TCCAccessPreflight.restype = ctypes.c_int
        self.tcc.TCCAccessPreflight.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    def preflight(self, service: str) -> int:
        cf_service = self.core_foundation.CFStringCreateWithCString(
            None, service.encode(), 0x08000100
        )
        return int(self.tcc.TCCAccessPreflight(ctypes.c_void_p(cf_service), None))


def main() -> None:
    libraries = Libraries()
    services = json.loads(sys.argv[1])
    results = {}
    for service in services:
        code = libraries.preflight(service)
        results[service] = {"code": code, "result": RESULT_WORDS.get(code, "unknown")}
    with Path(sys.argv[2]).open("w") as handle:
        json.dump(results, handle, indent=2)


if __name__ == "__main__":
    main()
