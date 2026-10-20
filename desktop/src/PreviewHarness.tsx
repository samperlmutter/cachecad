// Development-only harness exercises the production CAD viewer with actual files.
import { useCallback, useState } from "react";
import CadPreview from "./components/CadPreview";
export default function PreviewHarness() {
  const [file, setFile] = useState<File>();
  const load = useCallback(() => file!.arrayBuffer(), [file]);
  return (
    <main style={{ padding: 30, maxWidth: 1000, margin: "auto" }}>
      <h1>CAD import verification</h1>
      <label>
        Model fixture
        <input type="file" onChange={(e) => setFile(e.target.files?.[0])} />
      </label>
      {file && (
        <div style={{ height: 600, display: "grid", marginTop: 20 }}>
          <CadPreview path={file.name} load={load} />
        </div>
      )}
    </main>
  );
}
