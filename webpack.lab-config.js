/*
 * Copyright (c) 2024-2025 Datalayer, Inc.
 *
 * Distributed under the terms of the Modified BSD License.
 */

/*
 * Provide a local (bundled) fallback for Jupyter-ecosystem shared modules.
 *
 * `@jupyter/builder` forces every core JupyterLab package (its `core.package.json`
 * dependencies plus every `singletonPackages` entry) to be shared with
 * `import: false`, i.e. "the host must provide this, there is no bundled
 * fallback". When this extension is loaded into a host that does not expose a
 * matching version of one of those packages (common when the host lab and this
 * rspack-built extension disagree on versions), the module-federation runtime
 * throws:
 *
 *   Error: The getter for the shared module is not a function. This may be
 *   caused by setting "shared.import: false" without the host providing the
 *   corresponding lib. #RUNTIME-012
 *
 * Rather than enumerating every affected `@jupyterlab/*` / `@lumino/*` package
 * in `sharedPackages` (a never-ending game of whack-a-mole), we patch rspack's
 * `ModuleFederationPlugin` to re-enable the bundled fallback (delete
 * `import: false`) for any Jupyter-ecosystem share that is actually installed in
 * this extension's `node_modules`. The package stays a `singleton`, so when the
 * host does provide a compatible version that copy still wins; the local
 * fallback is only used when the host has nothing satisfying to offer.
 */
function patchModuleFederationSharedFallback() {
  const SCOPE_PATTERN =
    /^@(jupyterlab|jupyter|jupyter-notebook|jupyter-widgets|lumino)\//;

  let rspack;
  try {
    rspack = require('@rspack/core');
  } catch (error) {
    return;
  }

  const ModuleFederationPlugin = rspack?.container?.ModuleFederationPlugin;
  const prototype = ModuleFederationPlugin?.prototype;
  if (!prototype || typeof prototype.apply !== 'function') {
    return;
  }
  if (prototype.__datalayerSharedFallbackPatched) {
    return;
  }

  const originalApply = prototype.apply;

  prototype.apply = function applyWithSharedFallback(compiler) {
    const shared = this?._options?.shared;
    if (shared && typeof shared === 'object' && !Array.isArray(shared)) {
      for (const key of Object.keys(shared)) {
        const config = shared[key];
        if (!config || typeof config !== 'object') {
          continue;
        }
        // Only touch host-only shares within the Jupyter ecosystem.
        if (config.import !== false || !SCOPE_PATTERN.test(key)) {
          continue;
        }
        // Only enable a fallback for packages we can actually resolve locally,
        // so we never try to bundle something that is not installed.
        try {
          require.resolve(key, { paths: [__dirname] });
        } catch (error) {
          continue;
        }
        // Deleting `import` lets module federation bundle the local copy as a
        // fallback while keeping the package a shared singleton.
        delete config.import;
      }
    }
    return originalApply.call(this, compiler);
  };

  prototype.__datalayerSharedFallbackPatched = true;
}

patchModuleFederationSharedFallback();

module.exports = {
  // Workaround for an rspack panic during the production build:
  // "RealContentHashPlugin: circular hash dependency". Disabling the real
  // content hash pass avoids the circular hash computation while still
  // producing content-hashed asset filenames.
  optimization: {
    realContentHash: false
  }
};
