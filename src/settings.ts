/*
 * Copyright (c) 2024-2025 Datalayer, Inc.
 *
 * Distributed under the terms of the Modified BSD License.
 */

/**
 * Whether the outputs are recovered over HTTP.
 *
 * The extension executes the cells on the server, and the server writes their
 * outputs into the shared document: that is the path, and it is the only one
 * needed when the collaborative document behaves.
 *
 * Some persisted YStore histories carry an unresolved CRDT dependency chain,
 * which the browser cannot integrate — the server saves the outputs while the
 * page never observes them. The recovery works around it: every pending
 * response carries the outputs accumulated so far, the frontend reconciles
 * them into the cell, and a refreshed page resumes the polling of a request
 * it inherited. It makes the extension a second writer of the shared
 * document, so it is asked for rather than assumed.
 *
 * @module settings
 */

/** The plugin the setting belongs to, and its name in that schema. */
export const PLUGIN_ID =
  '@datalayer/jupyter-server-nbmodel:notebook-cell-executor';
export const OUTPUT_RECOVERY_SETTING = 'outputRecovery';

let outputRecovery = false;

/**
 * Whether the outputs are recovered over HTTP, as the settings say.
 */
export function isOutputRecoveryEnabled(): boolean {
  return outputRecovery;
}

/**
 * State what the settings say; the plugin does this once they are loaded.
 */
export function setOutputRecoveryEnabled(enabled: boolean): void {
  outputRecovery = enabled;
}
