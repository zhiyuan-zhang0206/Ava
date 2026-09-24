"""Real copied-interpreter startup without cluster fixtures or network access."""

import json
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path

from shared.runtime_prepare import (
    _copy_verified_python,
    _create_private_venv,
    _python_input_inventory,
)


@unittest.skipUnless(sys.platform == "darwin", "real Mac dynamic loader regression")
class RetainedMacPythonTests(unittest.TestCase):
    def test_copied_venv_loads_only_the_retained_python_library(self) -> None:
        library = sysconfig.get_config_var("LDLIBRARY")
        if not isinstance(library, str) or not library.endswith(".dylib"):
            self.skipTest("this Python does not declare a dynamic libpython")
        source = Path(sys.base_prefix).resolve()
        if not (source / "lib" / library).is_file():
            self.skipTest("this is not a standalone managed Python layout")
        with tempfile.TemporaryDirectory(prefix="ava-mac-python-") as directory:
            root = Path(directory).resolve()
            inventory = _python_input_inventory(source)
            _copy_verified_python(source, root / "python", inventory)
            env = {"PATH": "/usr/bin:/bin", "HOME": str(root)}
            interpreter = _create_private_venv(root, inventory)
            probe = """
import ctypes, json, os, sys
dyld = ctypes.CDLL(None)
dyld._dyld_image_count.restype = ctypes.c_uint32
dyld._dyld_get_image_name.argtypes = [ctypes.c_uint32]
dyld._dyld_get_image_name.restype = ctypes.c_char_p
print(json.dumps({'base': sys.base_prefix, 'prefix': sys.prefix,
 'images': [os.fsdecode(dyld._dyld_get_image_name(i))
            for i in range(dyld._dyld_image_count())]}))
"""
            result = subprocess.run(  # noqa: S603 -- same private interpreter and fixed read-only probe.
                [str(interpreter), "-I", "-B", "-c", probe],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            facts = json.loads(result.stdout)
            self.assertEqual(Path(facts["base"]).resolve(), root / "python")
            self.assertEqual(Path(facts["prefix"]).resolve(), root / "venv")
            libraries = [Path(p).resolve() for p in facts["images"] if Path(p).name == library]
            self.assertEqual(libraries, [root / "venv/lib" / library])
