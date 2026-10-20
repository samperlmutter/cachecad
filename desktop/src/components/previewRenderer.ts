import * as THREE from "three";
import { SVGRenderer } from "three/examples/jsm/renderers/SVGRenderer.js";

export interface PreviewRenderer {
  domElement: HTMLCanvasElement | SVGElement;
  mode: "webgl" | "software-svg";
  maxTriangles: number;
  setSize(width: number, height: number): void;
  render(scene: THREE.Scene, camera: THREE.Camera): void;
  dispose(): void;
}

/** Render real geometry on machines without a WebGL context, with a CPU budget. */
export function createPreviewRenderer(): PreviewRenderer {
  let webgl: THREE.WebGLRenderer;
  try {
    webgl = new THREE.WebGLRenderer({
      antialias: true,
      alpha: true,
      powerPreference: "low-power",
    });
  } catch {
    const svg = new SVGRenderer();
    svg.setQuality("low");
    svg.setPrecision(3);
    svg.setClearColor(new THREE.Color(0xf3f6f7), 1);
    svg.setSize(300, 150);
    return {
      domElement: svg.domElement,
      mode: "software-svg",
      maxTriangles: 50_000,
      setSize: (width, height) => svg.setSize(width, height),
      render: (scene, camera) => svg.render(scene, camera),
      dispose: () => svg.clear(),
    };
  }
  webgl.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  webgl.setClearColor(0xf3f6f7, 1);
  webgl.outputColorSpace = THREE.SRGBColorSpace;
  return {
    domElement: webgl.domElement,
    mode: "webgl",
    maxTriangles: Infinity,
    setSize: (width, height) => webgl.setSize(width, height),
    render: (scene, camera) => webgl.render(scene, camera),
    dispose() {
      webgl.dispose();
      webgl.forceContextLoss();
    },
  };
}
