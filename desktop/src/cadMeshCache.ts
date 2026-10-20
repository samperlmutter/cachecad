export interface ImportedMesh {
  name: string;
  color?: number[];
  position: Float32Array;
  index: Uint32Array;
  normal: Float32Array | null;
}
export async function cadMeshKey(
  buffer: ArrayBuffer,
  extension: string,
): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return `${extension}:${Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}
export class CadMeshCache {
  private entry?: { key: string; meshes: ImportedMesh[] };

  constructor(private readonly byteLimit: number) {}

  get(key: string): ImportedMesh[] | undefined {
    return this.entry?.key === key ? this.entry.meshes : undefined;
  }

  put(key: string, meshes: ImportedMesh[]): void {
    // Keep only the latest model; arrays belong to the main thread after the
    // worker transfers them and remain usable when Three.js disposes its GPU data.
    this.entry = undefined;
    const buffers = new Set<ArrayBufferLike>();
    for (const mesh of meshes) {
      buffers.add(mesh.position.buffer);
      buffers.add(mesh.index.buffer);
      if (mesh.normal) buffers.add(mesh.normal.buffer);
    }
    let bytes = 0;
    for (const buffer of buffers) bytes += buffer.byteLength;
    if (bytes <= this.byteLimit) this.entry = { key, meshes };
  }
}

// Memory only: no model data is persisted, and opening another model evicts it.
export const cadMeshCache = new CadMeshCache(192 * 1024 * 1024);
