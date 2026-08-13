/*
 * Copyright (c) 2024-2025 Datalayer, Inc.
 *
 * Distributed under the terms of the Modified BSD License.
 */

import type { CodeCell } from '@jupyterlab/cells';

export const EXECUTION_METADATA_KEY = 'jupyter_server_nbmodel';

const clientOwnedExecutions = new WeakSet<CodeCell>();

export interface IServerExecutionMetadata {
  kernelId: string;
  requestId: string;
  requestUrl: string;
}

/**
 * Mark a cell execution as owned by the executor in this browser page.
 *
 * The execution restorer must only poll requests inherited from a previous
 * page. Polling a live request here as well would race the executor for the
 * consumptive result endpoint.
 */
export function markClientOwnedExecution(cell: CodeCell): void {
  clientOwnedExecutions.add(cell);
}

export function unmarkClientOwnedExecution(cell: CodeCell): void {
  clientOwnedExecutions.delete(cell);
}

export function isClientOwnedExecution(cell: CodeCell): boolean {
  return clientOwnedExecutions.has(cell);
}

export function getServerExecutionMetadata(
  cell: CodeCell
): IServerExecutionMetadata | undefined {
  const value = cell.model.getMetadata(EXECUTION_METADATA_KEY) as
    | Partial<IServerExecutionMetadata>
    | undefined;
  return value?.kernelId && value.requestId && value.requestUrl
    ? (value as IServerExecutionMetadata)
    : undefined;
}

export function setServerExecutionMetadata(
  cell: CodeCell,
  metadata: IServerExecutionMetadata
): void {
  cell.model.setMetadata(EXECUTION_METADATA_KEY, metadata);
}

export function clearServerExecutionMetadata(cell: CodeCell): void {
  cell.model.deleteMetadata(EXECUTION_METADATA_KEY);
}
