"""Isolated, bundled OpenCascade worker. Only generated GLB leaves this process."""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import subprocess
import signal

PROTOCOL = "permafrost-occt-7.9.3-merged-v1"
MAX_SOURCE = 512 * 1024 * 1024
MAX_OUTPUT = 192 * 1024 * 1024
MAX_TRIANGLES = 5_000_000


def watch_parent(parent, worker):
    """Separate lightweight process: native CAD calls may hold Python's GIL."""
    if worker <= 1 or os.getppid() != worker or parent <= 1:
        return 2
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.WaitForMultipleObjects.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD)
        kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        owner = kernel.OpenProcess(0x100000, False, parent)
        child = kernel.OpenProcess(0x100001, False, worker)
        if not owner or not child:
            return 2
        print("READY", flush=True)
        result = kernel.WaitForMultipleObjects(2, (wintypes.HANDLE * 2)(owner, child), False, 150000)
        if result != 1:
            kernel.TerminateProcess(child, 2)
        kernel.CloseHandle(owner)
        kernel.CloseHandle(child)
    elif sys.platform == "darwin":
        import select
        queue = select.kqueue()
        changes = [select.kevent(pid, filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)
            for pid in (parent, worker)]
        queue.control(changes, 0, 0)
        print("READY", flush=True)
        events = queue.control(None, 2, 150)
        if not events or any(event.ident == parent for event in events):
            try:
                os.kill(worker, signal.SIGKILL)
            except ProcessLookupError:
                pass
        queue.close()
    else:
        import select
        owner, child = os.pidfd_open(parent), os.pidfd_open(worker)
        print("READY", flush=True)
        ready, _, _ = select.select([owner, child], [], [], 150)
        if child not in ready:
            os.kill(worker, signal.SIGKILL)
        os.close(owner)
        os.close(child)
    return 0


def progress(phase):
    print("PF_CAD " + json.dumps({"phase": phase}), flush=True)


def convert(source, extension, output):
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString, TCollection_AsciiString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.TDF import TDF_LabelSequence
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.RWGltf import RWGltf_CafWriter
    from OCP.TColStd import TColStd_IndexedDataMapOfStringString
    from OCP.Message import Message_ProgressRange
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS
    from OCP.BRep import BRep_Tool
    from OCP.TopLoc import TopLoc_Location

    doc = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    progress("reading")
    if extension in ("step", "stp", "iges", "igs"):
        if extension in ("step", "stp"):
            from OCP.STEPCAFControl import STEPCAFControl_Reader
            reader = STEPCAFControl_Reader()
        else:
            from OCP.IGESCAFControl import IGESCAFControl_Reader
            reader = IGESCAFControl_Reader()
        reader.SetColorMode(True)
        reader.SetNameMode(True)
        if reader.ReadFile(str(source)) != IFSelect_RetDone or not reader.Transfer(doc):
            raise ValueError("read")
    elif extension in ("brep", "brp"):
        from OCP.BRepTools import BRepTools
        from OCP.BRep import BRep_Builder
        from OCP.TopoDS import TopoDS_Shape
        shape = TopoDS_Shape()
        if not BRepTools.Read_s(shape, str(source), BRep_Builder()):
            raise ValueError("read")
        tool.AddShape(shape, False)
    else:
        raise ValueError("format")
    labels = TDF_LabelSequence()
    tool.GetFreeShapes(labels)
    if labels.IsEmpty():
        raise ValueError("empty")
    progress("meshing")
    triangles = 0
    for index in range(1, labels.Length() + 1):
        shape = tool.GetShape_s(labels.Value(index))
        bounds = Bnd_Box()
        BRepBndLib.Add_s(shape, bounds, False)
        if bounds.IsVoid():
            raise ValueError("empty")
        x0, y0, z0, x1, y1, z1 = bounds.Get()
        deviation = max(((x1 - x0) + (y1 - y0) + (z1 - z0)) / 3 * .002, 1e-7)
        if not math.isfinite(deviation):
            raise ValueError("bounds")
        mesh = BRepMesh_IncrementalMesh(shape, deviation, False, .5, True)
        if not mesh.IsDone():
            raise ValueError("mesh")
        faces = TopExp_Explorer(shape, TopAbs_FACE)
        while faces.More():
            face = TopoDS.Face_s(faces.Current())
            tessellation = BRep_Tool.Triangulation_s(face, TopLoc_Location())
            if tessellation is not None:
                triangles += tessellation.NbTriangles()
            if triangles > MAX_TRIANGLES:
                raise ValueError("triangles")
            faces.Next()
    if not triangles:
        raise ValueError("empty")
    progress("writing")
    writer = RWGltf_CafWriter(TCollection_AsciiString(str(output)), True)
    writer.SetMergeFaces(True)
    if not writer.Perform(doc, TColStd_IndexedDataMapOfStringString(), Message_ProgressRange()):
        raise ValueError("export")
    if not 20 <= output.stat().st_size <= MAX_OUTPUT:
        raise ValueError("size")
    progress("complete")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--extension")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--parent", type=int)
    parser.add_argument("--watch-parent", type=int)
    parser.add_argument("--worker", type=int)
    args = parser.parse_args()
    if args.watch_parent:
        return watch_parent(args.watch_parent, args.worker)
    if args.protocol:
        print(PROTOCOL)
        return 0
    watcher = None
    try:
        if not args.source or not args.output or args.source.is_symlink():
            raise ValueError("source")
        if not args.source.is_file() or not 0 < args.source.stat().st_size <= MAX_SOURCE:
            raise ValueError("source")
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("output")
        if args.parent:
            command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, __file__]
            watcher = subprocess.Popen(command + ["--watch-parent", str(args.parent), "--worker", str(os.getpid())],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if watcher.stdout.readline().strip() != b"READY":
                raise ValueError("watcher")
        convert(args.source, args.extension, args.output)
        return 0
    except Exception:
        # Native readers can include model names in diagnostics. Do not forward
        # exception text or native stdout/stderr into the UI or application logs.
        print('PF_CAD {"error":"conversion_failed"}', flush=True)
        return 1
    finally:
        if watcher is not None:
            watcher.terminate()
            watcher.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
