import { describe, expect, it } from "vitest";
import { CadMeshCache, cadMeshKey, type ImportedMesh } from "./cadMeshCache";

function triangle(): ImportedMesh[] {
  return [
    {
      name: "body",
      position: new Float32Array([0, 0, 0, 1, 0, 0, 0, 1, 0]),
      index: new Uint32Array([0, 1, 2]),
      normal: null,
    },
  ];
}

describe("CAD mesh reuse", () => {
  it("reuses a completed mesh for identical contents but not revised bytes or another format", async () => {
    const cache = new CadMeshCache(1024);
    const key = await cadMeshKey(new Uint8Array([1, 2, 3]).buffer, "step");
    const meshes = triangle();
    cache.put(key, meshes);
    expect(
      cache.get(await cadMeshKey(new Uint8Array([1, 2, 3]).buffer, "step")),
    ).toBe(meshes);
    expect(
      cache.get(await cadMeshKey(new Uint8Array([1, 2, 4]).buffer, "step")),
    ).toBeUndefined();
    expect(
      cache.get(await cadMeshKey(new Uint8Array([1, 2, 3]).buffer, "iges")),
    ).toBeUndefined();
  });

  it("retains only the latest completed model", () => {
    const cache = new CadMeshCache(1024);
    cache.put("old", triangle());
    const latest = triangle();
    cache.put("new", latest);
    expect(cache.get("old")).toBeUndefined();
    expect(cache.get("new")).toBe(latest);
  });

  it("counts positions, indices and normals against its memory bound", () => {
    const cache = new CadMeshCache(48);
    cache.put("fits", triangle()); // 36 bytes of positions + 12 bytes of indices
    expect(cache.get("fits")).toBeDefined();
    const meshes = triangle();
    meshes[0].normal = new Float32Array(9);
    cache.put("oversized", meshes);
    expect(cache.get("oversized")).toBeUndefined();
    expect(cache.get("fits")).toBeUndefined();
  });
});
