// Keep STL/OBJ text parsing and normal generation off the workspace thread.
import * as THREE from "three";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { OBJLoader } from "three/examples/jsm/loaders/OBJLoader.js";
import type { ImportedMesh } from "./cadMeshCache";

self.onmessage = ({
  data,
}: MessageEvent<{ buffer: ArrayBuffer; extension: string }>) => {
  try {
    self.postMessage({ phase: "import" });
    const model =
      data.extension === "stl"
        ? new THREE.Mesh(new STLLoader().parse(data.buffer))
        : new OBJLoader().parse(new TextDecoder().decode(data.buffer));
    const meshes: ImportedMesh[] = [];
    const buffers: ArrayBuffer[] = [];
    let triangles = 0;
    model.traverse((child) => {
      if (!(child instanceof THREE.Mesh)) return;
      const geometry = child.geometry;
      const attribute = geometry.getAttribute("position");
      if (!attribute) return;
      triangles += (geometry.index?.count ?? attribute.count) / 3;
      if (triangles > 5_000_000)
        throw new Error(
          "This preview exceeds the five-million-triangle limit. Export a lighter model.",
        );
      geometry.computeVertexNormals();
      const position =
        attribute.array instanceof Float32Array
          ? attribute.array
          : new Float32Array(attribute.array);
      const normalAttribute = geometry.getAttribute("normal");
      const normal = normalAttribute
        ? new Float32Array(normalAttribute.array)
        : null;
      // Empty indices mean a non-indexed mesh; avoid a redundant large index array.
      const index = geometry.index
        ? new Uint32Array(geometry.index.array)
        : new Uint32Array();
      meshes.push({ name: child.name, position, index, normal });
      buffers.push(position.buffer as ArrayBuffer, index.buffer as ArrayBuffer);
      if (normal) buffers.push(normal.buffer as ArrayBuffer);
    });
    self.postMessage({ meshes }, { transfer: buffers });
  } catch (error) {
    self.postMessage({
      error:
        error instanceof Error ? error.message : "Unable to read this mesh.",
    });
  }
};
