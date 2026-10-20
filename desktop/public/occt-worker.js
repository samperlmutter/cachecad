/* Local, disposable worker: a malformed/expensive CAD import can be terminated
   without blocking the app's interface. No model data leaves this process. */
importScripts('/occt/occt-import-js.js');
let engine;
self.onmessage = async ({ data }) => {
  try {
    engine ??= self.occtimportjs({ locateFile: file => `/occt/${file}` });
    const occt = await engine;
    self.postMessage({ phase: 'import' });
    const ext = data.extension.toLowerCase();
    const read = ext === 'step' || ext === 'stp' ? occt.ReadStepFile : ext === 'iges' || ext === 'igs' ? occt.ReadIgesFile : occt.ReadBrepFile;
    const result = read(new Uint8Array(data.buffer), { linearUnit: 'millimeter', linearDeflectionType: 'bounding_box_ratio', linearDeflection: 0.002, angularDeflection: 0.5 });
    if (!result.success || !result.meshes?.length) throw new Error('OpenCascade could not read this model. Check the file or try a fresh export.');
    self.postMessage({ phase: 'meshes' });
    let count = 0;
    const transfers = [];
    const meshes = result.meshes.map(mesh => {
      count += mesh.index.array.length;
      if (count > 15000000) throw new Error('This preview exceeds the five-million-triangle limit. Export a lighter model.');
      const position = new Float32Array(mesh.attributes.position.array);
      const index = new Uint32Array(mesh.index.array);
      const normal = mesh.attributes.normal ? new Float32Array(mesh.attributes.normal.array) : null;
      transfers.push(position.buffer,index.buffer);
      if (normal) transfers.push(normal.buffer);
      return { name: mesh.name, color: mesh.color, position,index,normal };
    });
    self.postMessage({ meshes, root: result.root },transfers);
  } catch (error) { self.postMessage({ error: error instanceof Error ? error.message : 'Unable to read this CAD file.' }); }
};
