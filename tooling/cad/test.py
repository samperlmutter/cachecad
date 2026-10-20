"""Build-time integration tests for the packaged runtime, using public geometry."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / 'src-tauri/resources/cad-helper/permafrost-cad' / ('permafrost-cad.exe' if os.name == 'nt' else 'permafrost-cad')


class PackagedRuntime(unittest.TestCase):
    def test_watchdog_terminates_worker_when_app_exits(self):
        owner = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
        # The wrapper stands in for a worker stuck inside native CAD code. The
        # actual frozen watchdog must terminate it when its app owner exits.
        code = """import os,subprocess,sys,time
w = subprocess.Popen([sys.argv[1], '--watch-parent', sys.argv[2], '--worker', str(os.getpid())], stdout=subprocess.PIPE)
print(w.stdout.readline().decode().strip(), flush=True)
time.sleep(60)
"""
        worker = subprocess.Popen([sys.executable, '-c', code, str(HELPER), str(owner.pid)], stdout=subprocess.PIPE)
        try:
            self.assertEqual(worker.stdout.readline().strip(), b'READY')
            owner.terminate()
            owner.wait(timeout=5)
            self.assertNotEqual(worker.wait(timeout=10), 0)
        finally:
            for process in (owner, worker):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
            worker.stdout.close()

    def test_iges_and_brep_exports(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.BRepTools import BRepTools
        from OCP.IGESControl import IGESControl_Writer
        shape = BRepPrimAPI_MakeBox(10, 20, 30).Shape()
        with tempfile.TemporaryDirectory(prefix='permafrost-cad-formats-') as tmp:
            root = Path(tmp)
            BRepTools.Write_s(shape, str(root / 'cube.brep'))
            writer = IGESControl_Writer()
            self.assertTrue(writer.AddShape(shape))
            self.assertTrue(writer.Write(str(root / 'cube.iges')))
            for extension in ('brep', 'iges'):
                output = root / (extension + '.glb')
                result = subprocess.run([str(HELPER), '--source', str(root / ('cube.' + extension)), '--extension', extension,
                    '--output', str(output), '--parent', str(os.getpid())], capture_output=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stdout)
                data = output.read_bytes()
                self.assertEqual(data[:4], b'glTF')
                self.assertEqual(int.from_bytes(data[8:12], 'little'), len(data))
                document = json.loads(data[20:20+int.from_bytes(data[12:16], 'little')])
                self.assertGreater(len(document['meshes']), 0)


if __name__ == '__main__':
    unittest.main()
