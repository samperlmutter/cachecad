import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { expect, it } from "vitest";

it("reports separate reader and display phases without dropping any model bodies", async () => {
  const messages: Array<{
    phase?: string;
    meshes?: Array<{ name: string; position: Float32Array }>;
  }> = [];
  const body = (name: string) => ({
    name,
    attributes: { position: { array: [0, 0, 0, 1, 0, 0, 0, 1, 0] } },
    index: { array: [0, 1, 2] },
  });
  const self = {
    onmessage: undefined as
      | undefined
      | ((event: {
          data: { extension: string; buffer: ArrayBuffer };
        }) => Promise<void>),
    occtimportjs: async () => ({
      ReadStepFile: () => ({
        success: true,
        meshes: [body("one"), body("two")],
        root: {},
      }),
    }),
    postMessage: (message: (typeof messages)[number]) => messages.push(message),
  };
  runInNewContext(
    readFileSync(new URL("../public/occt-worker.js", import.meta.url), "utf8"),
    { self, importScripts: () => {}, Float32Array, Uint32Array, Uint8Array },
  );
  await self.onmessage!({
    data: { extension: "step", buffer: new ArrayBuffer(1) },
  });
  expect(messages.map((message) => message.phase ?? "complete")).toEqual([
    "import",
    "meshes",
    "complete",
  ]);
  expect(messages.at(-1)?.meshes?.map((mesh) => mesh.name)).toEqual([
    "one",
    "two",
  ]);
  expect([...messages.at(-1)!.meshes![0].position]).toEqual([
    0, 0, 0, 1, 0, 0, 0, 1, 0,
  ]);
});
