/*
 * Copyright (c) 2024-2025 Datalayer, Inc.
 *
 * Distributed under the terms of the Modified BSD License.
 */

import type { NotebookPanel } from '@jupyterlab/notebook';
import type { Kernel } from '@jupyterlab/services';

const KERNEL_STATUSES = new Set<Kernel.Status>([
  'unknown',
  'starting',
  'idle',
  'busy',
  'terminating',
  'restarting',
  'autorestarting',
  'dead'
]);

/**
 * Restore the status on JupyterLab's kernel connection.
 *
 * JupyterLab deliberately initializes a newly created kernel connection with
 * an `unknown` status. The public kernel API has no status setter because the
 * value normally comes from IOPub. A page refreshed during an execution has
 * missed the earlier `busy` message, however, while the server request endpoint
 * still knows the authoritative state. KernelConnection._updateStatus is the
 * same internal path used for IOPub status messages and emits all standard
 * statusChanged signals.
 */
export function restoreKernelStatus(
  panel: NotebookPanel,
  status: Kernel.Status
): boolean {
  const kernel = panel.context.sessionContext.session?.kernel as
    | (Kernel.IKernelConnection & {
        _updateStatus?: (value: Kernel.Status) => void;
      })
    | null
    | undefined;
  if (!kernel?._updateStatus) {
    return false;
  }
  kernel._updateStatus(status);
  return true;
}

/**
 * Initialize a refreshed connection from Jupyter Server's existing kernel
 * model, which contains the execution state that the new WebSocket missed.
 */
export async function restoreKernelModelStatus(
  panel: NotebookPanel
): Promise<boolean> {
  const sessionContext = panel.context.sessionContext;
  const kernel = sessionContext.session?.kernel;
  const kernelManager = sessionContext.kernelManager;
  if (!kernel || !kernelManager || kernel.status !== 'unknown') {
    return false;
  }

  await kernelManager.refreshRunning();
  const model = Array.from(kernelManager.running()).find(
    candidate => candidate.id === kernel.id
  );
  const status = model?.execution_state;
  return status && KERNEL_STATUSES.has(status as Kernel.Status)
    ? restoreKernelStatus(panel, status as Kernel.Status)
    : false;
}
