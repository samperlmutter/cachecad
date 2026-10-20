import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { OBJLoader } from "three/examples/jsm/loaders/OBJLoader.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { Box, Expand, Grid2X2, RotateCcw, Scan, X } from "lucide-react";
import { previewable } from "../types";
import { PREVIEW_BYTE_LIMIT, previewError } from "../preview";
import { cadMeshCache, cadMeshKey, type ImportedMesh } from "../cadMeshCache";

interface Props {
  path: string;
  load?: () => Promise<ArrayBuffer>;
  sample?: boolean;
  onClose?: () => void;
}
function sampleBracket(): THREE.Group {
  const shape = new THREE.Shape();
  shape.moveTo(-48, -30);
  shape.lineTo(48, -30);
  shape.lineTo(48, 26);
  shape.quadraticCurveTo(48, 34, 40, 34);
  shape.lineTo(-40, 34);
  shape.quadraticCurveTo(-48, 34, -48, 26);
  shape.closePath();
  for (const x of [-31, 31]) {
    const hole = new THREE.Path();
    hole.absarc(x, 13, 7, 0, Math.PI * 2, true);
    shape.holes.push(hole);
  }
  const center = new THREE.Path();
  center.moveTo(-14, -16);
  center.lineTo(-14, 15);
  center.quadraticCurveTo(-14, 20, -9, 20);
  center.lineTo(9, 20);
  center.quadraticCurveTo(14, 20, 14, 15);
  center.lineTo(14, -16);
  center.closePath();
  shape.holes.push(center);
  const geometry = new THREE.ExtrudeGeometry(shape, {
    depth: 12,
    bevelEnabled: true,
    bevelSegments: 3,
    steps: 1,
    bevelSize: 1.5,
    bevelThickness: 1.5,
    curveSegments: 36,
  });
  geometry.center();
  const group = new THREE.Group();
  const material = new THREE.MeshStandardMaterial({
    color: 0x72c5dc,
    metalness: 0.28,
    roughness: 0.35,
  });
  const body = new THREE.Mesh(geometry, material);
  body.rotation.x = -Math.PI / 2;
  group.add(body);
  const flange = new THREE.Mesh(new THREE.BoxGeometry(96, 31, 12), material);
  flange.position.set(0, 20, 29);
  group.add(flange);
  group.rotation.y = Math.PI;
  return group;
}
function fromImported(meshes: ImportedMesh[]): THREE.Group {
  const group = new THREE.Group();
  for (const m of meshes) {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(m.position, 3));
    geometry.setIndex(new THREE.BufferAttribute(m.index, 1));
    if (m.normal)
      geometry.setAttribute("normal", new THREE.BufferAttribute(m.normal, 3));
    else geometry.computeVertexNormals();
    const color = m.color
      ? new THREE.Color(m.color[0], m.color[1], m.color[2])
      : new THREE.Color(0x72c5dc);
    const mesh = new THREE.Mesh(
      geometry,
      new THREE.MeshStandardMaterial({
        color,
        metalness: 0.15,
        roughness: 0.48,
        side: THREE.DoubleSide,
      }),
    );
    mesh.name = m.name;
    group.add(mesh);
  }
  return group;
}
function dispose(object: THREE.Object3D) {
  object.traverse((child) => {
    if (child instanceof THREE.Mesh || child instanceof THREE.LineSegments) {
      child.geometry.dispose();
      const materials = Array.isArray(child.material)
        ? child.material
        : [child.material];
      materials.forEach((m) => m.dispose());
    }
  });
}

export default function CadPreview({ path, load, sample, onClose }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const reset = useRef<() => void>(() => {});
  const changeWireframe = useRef<(enabled: boolean) => void>(() => {});
  const changeGrid = useRef<(enabled: boolean) => void>(() => {});
  const [status, setStatus] = useState("Preparing preview…");
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState("");
  const [wireframe, setWireframe] = useState(false);
  const [grid, setGrid] = useState(true);
  const [details, setDetails] = useState("");
  const supported = sample || previewable(path);
  useEffect(() => {
    const element = host.current;
    if (!element || !supported) return;
    let closed = false;
    let worker: Worker | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let progressTimer: ReturnType<typeof setInterval> | undefined;
    let phaseStarted = performance.now();
    const phase = (message: string) => {
      phaseStarted = performance.now();
      setElapsed(0);
      setStatus(message);
    };
    const finishProgress = () => {
      clearInterval(progressTimer);
      setStatus("");
    };
    let object: THREE.Object3D | undefined;
    setStatus("Preparing preview…");
    setElapsed(0);
    setError("");
    setWireframe(false);
    setGrid(true);
    setDetails("");
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({
        antialias: true,
        alpha: true,
        powerPreference: "low-power",
      });
    } catch {
      setError(
        "3D acceleration is unavailable. Enable your system’s graphics support and reopen the preview.",
      );
      return;
    }
    progressTimer = setInterval(() => {
      if (!closed)
        setElapsed(Math.floor((performance.now() - phaseStarted) / 1000));
    }, 1000);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setClearColor(0xf3f6f7, 1);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    element.appendChild(renderer.domElement);
    renderer.domElement.setAttribute("aria-label", `3D preview of ${path}`);
    renderer.domElement.setAttribute("role", "img");
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 100000);
    scene.add(new THREE.HemisphereLight(0xffffff, 0x6a7880, 2.6));
    const key = new THREE.DirectionalLight(0xffffff, 3.2);
    key.position.set(100, 180, 120);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0xb7d5df, 1.5);
    fill.position.set(-100, 30, -80);
    scene.add(fill);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = false;
    const gridHelper = new THREE.GridHelper(320, 16, 0xc5d3d9, 0xe0e8ec);
    scene.add(gridHelper);
    const render = () => {
      if (!closed) renderer.render(scene, camera);
    };
    const fit = () => {
      if (!object) return;
      const bounds = new THREE.Box3().setFromObject(object);
      const size = bounds.getSize(new THREE.Vector3());
      const center = bounds.getCenter(new THREE.Vector3());
      const radius = Math.max(size.x, size.y, size.z, 0.01);
      const sphereRadius = size.length() / 2;
      const verticalFov = THREE.MathUtils.degToRad(camera.fov);
      const horizontalFov =
        2 * Math.atan(Math.tan(verticalFov / 2) * camera.aspect);
      const distance =
        (sphereRadius * 1.2) /
        Math.sin(Math.min(verticalFov, horizontalFov) / 2);
      camera.near = Math.max(radius / 1000, 0.0001);
      camera.far = radius * 100;
      camera.position
        .copy(center)
        .add(
          new THREE.Vector3(1, 0.8, 1.25).normalize().multiplyScalar(distance),
        );
      camera.updateProjectionMatrix();
      controls.target.copy(center);
      controls.minDistance = radius / 20;
      controls.maxDistance = radius * 12;
      controls.update();
      gridHelper.position.y = bounds.min.y - radius * 0.04;
      gridHelper.scale.setScalar(radius / 120);
      render();
    };
    reset.current = fit;
    changeWireframe.current = (enabled) => {
      object?.traverse((child) => {
        if (child instanceof THREE.Mesh)
          for (const material of Array.isArray(child.material)
            ? child.material
            : [child.material])
            if ("wireframe" in material) material.wireframe = enabled;
      });
      render();
    };
    changeGrid.current = (enabled) => {
      gridHelper.visible = enabled;
      render();
    };
    const resizeViewport = () => {
      const { width, height } = element.getBoundingClientRect();
      if (width && height) {
        renderer.setSize(width, height);
        camera.aspect = width / height;
        camera.updateProjectionMatrix();
        render();
      }
    };
    // A reused mesh can mount before ResizeObserver's first callback. Size the
    // camera now so its initial fit uses the viewport's actual aspect ratio.
    resizeViewport();
    const resize = new ResizeObserver(resizeViewport);
    resize.observe(element);
    controls.addEventListener("change", render);
    const onLost = (event: Event) => {
      event.preventDefault();
      setError("The graphics context was lost. Close and reopen this preview.");
    };
    renderer.domElement.addEventListener("webglcontextlost", onLost);
    const mount = (model: THREE.Object3D) => {
      if (closed) {
        dispose(model);
        return;
      }
      object = model;
      scene.add(model);
      fit();
      let triangles = 0;
      let parts = 0;
      model.traverse((child) => {
        if (child instanceof THREE.Mesh) {
          const positions = child.geometry.getAttribute("position");
          if (!positions || positions.count > 15000000)
            throw new Error("The model is too large to preview.");
          triangles += (child.geometry.index?.count ?? positions.count) / 3;
          parts++;
        }
      });
      renderer.domElement.dataset.previewReady = "true";
      setDetails(
        `${parts} ${parts === 1 ? "body" : "bodies"} · ${Math.round(triangles).toLocaleString()} triangles`,
      );
      finishProgress();
    };
    void (async () => {
      try {
        if (sample) {
          mount(sampleBracket());
          return;
        }
        if (!load) throw new Error("Select a local file to preview it.");
        phase("Loading file…");
        const buffer = await load();
        if (closed) return;
        if (buffer.byteLength > PREVIEW_BYTE_LIMIT)
          throw new Error(
            "This file exceeds the 512 MiB preview limit. Export a lighter model.",
          );
        const extension = path.split(".").pop()!.toLowerCase();
        if (["step", "stp", "iges", "igs", "brep", "brp"].includes(extension)) {
          phase("Preparing model…");
          // Hash before transferring the source buffer. A revision at the same
          // path must never reuse stale geometry; hashing failure only skips reuse.
          const cacheKey = await cadMeshKey(buffer, extension).catch(
            () => undefined,
          );
          if (closed) return;
          const cached = cacheKey ? cadMeshCache.get(cacheKey) : undefined;
          if (cached) {
            mount(fromImported(cached));
            return;
          }
          phase("Starting OpenCascade…");
          worker = new Worker("/occt-worker.js");
          timer = setTimeout(() => {
            worker?.terminate();
            if (!closed) {
              setError(
                "The model took too long to process. Try a simpler export.",
              );
              finishProgress();
            }
          }, 60000);
          worker.onmessage = ({ data }) => {
            if (closed) return;
            if (data.phase === "import") {
              phase("Reading and tessellating model…");
              return;
            }
            if (data.phase === "meshes") {
              phase("Preparing model for display…");
              return;
            }
            clearTimeout(timer);
            worker?.terminate();
            if (data.error) {
              setError(data.error);
              finishProgress();
            } else {
              try {
                mount(fromImported(data.meshes));
                if (cacheKey) cadMeshCache.put(cacheKey, data.meshes);
              } catch (e) {
                setError(previewError(e));
                finishProgress();
              }
            }
          };
          worker.onerror = () => {
            clearTimeout(timer);
            worker?.terminate();
            if (!closed) {
              setError(
                "The CAD reader could not start. Reopen the preview to try again.",
              );
              finishProgress();
            }
          };
          worker.postMessage({ buffer, extension }, [buffer]);
        } else if (extension === "stl") {
          const geometry = new STLLoader().parse(buffer);
          geometry.computeVertexNormals();
          mount(
            new THREE.Mesh(
              geometry,
              new THREE.MeshStandardMaterial({
                color: 0x72c5dc,
                metalness: 0.18,
                roughness: 0.4,
                side: THREE.DoubleSide,
              }),
            ),
          );
        } else if (extension === "obj") {
          const model = new OBJLoader().parse(new TextDecoder().decode(buffer));
          model.traverse((child) => {
            if (child instanceof THREE.Mesh)
              child.material = new THREE.MeshStandardMaterial({
                color: 0x72c5dc,
                side: THREE.DoubleSide,
              });
          });
          mount(model);
        } else {
          const manager = new THREE.LoadingManager();
          manager.setURLModifier((url) => {
            if (url.startsWith("data:") || url.startsWith("blob:")) return url;
            throw new Error(
              "Preview models must embed their resources. External URLs are disabled.",
            );
          });
          const model = await new GLTFLoader(manager).parseAsync(buffer, "");
          mount(model.scene);
        }
      } catch (e) {
        if (!closed) {
          setError(previewError(e));
          finishProgress();
        }
      }
    })();
    return () => {
      closed = true;
      clearTimeout(timer);
      clearInterval(progressTimer);
      worker?.terminate();
      resize.disconnect();
      controls.dispose();
      renderer.domElement.removeEventListener("webglcontextlost", onLost);
      dispose(scene);
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
    };
  }, [path, load, sample, supported]);
  return (
    <section className="preview-panel" aria-label="CAD preview">
      <div className="preview-title">
        <div className="eyebrow">
          <Box size={14} /> MODEL PREVIEW
        </div>
        {onClose && (
          <button
            className="icon-button"
            onClick={onClose}
            aria-label="Close preview"
          >
            <X size={16} />
          </button>
        )}
      </div>
      <div className="preview-viewport" ref={host} />
      {!supported ? (
        <div className="preview-message">
          <Box size={32} />
          <strong>Sync it. Preview an export.</strong>
          <p>
            Native SolidWorks files sync in full. To view this model, add a
            STEP, IGES, BREP, STL, OBJ, or GLB export to the project.
          </p>
        </div>
      ) : error ? (
        <div className="preview-message error" role="alert">
          <Box size={28} />
          <strong>Preview unavailable</strong>
          <p>{error}</p>
        </div>
      ) : status ? (
        <div className="preview-message">
          <span className="spinner" />
          <p>
            {status}
            {elapsed > 0 ? ` ${elapsed}s elapsed` : ""}
          </p>
        </div>
      ) : null}
      <div className="preview-corner">
        <span className="axis z">Z</span>
        <span className="axis y">Y</span>
        <span className="axis x">X</span>
      </div>
      <div className="preview-tools">
        <button
          className="icon-button"
          onClick={() => reset.current()}
          aria-label="Reset camera"
          title="Reset camera"
        >
          <RotateCcw size={16} />
        </button>
        <button
          className={`icon-button ${wireframe ? "active" : ""}`}
          onClick={() => {
            setWireframe(!wireframe);
            changeWireframe.current(!wireframe);
          }}
          aria-label="Toggle wireframe"
          aria-pressed={wireframe}
          title="Wireframe"
        >
          <Scan size={17} />
        </button>
        <button
          className={`icon-button ${grid ? "active" : ""}`}
          onClick={() => {
            setGrid(!grid);
            changeGrid.current(!grid);
          }}
          aria-label="Toggle grid"
          aria-pressed={grid}
          title="Grid"
        >
          <Grid2X2 size={16} />
        </button>
        <button
          className="icon-button"
          onClick={() => void host.current?.parentElement?.requestFullscreen()}
          aria-label="Fullscreen preview"
          title="Fullscreen"
        >
          <Expand size={16} />
        </button>
      </div>
      <div className="preview-caption">
        <strong>{path.split("/").pop()}</strong>
        <span>{details || "Local rendering · files stay on your device"}</span>
      </div>
      <div className="preview-hint">
        Drag to orbit <span>·</span> Scroll to zoom <span>·</span> Right-drag to
        pan
      </div>
    </section>
  );
}
