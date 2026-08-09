/*
 * Copyright (c) 2024-2025 Datalayer, Inc.
 *
 * Distributed under the terms of the Modified BSD License.
 */

import type { CodeCell } from '@jupyterlab/cells';

export const EXECUTION_METADATA_KEY = 'jupyter_server_nbmodel';

export interface IServerExecutionMetadata {
  kernelId: string;
  requestId: string;
  requestUrl: string;
}

export function getServerExecutionMetadata(
  cell: CodeCell
): IServerExecutionMetadata | undefined {
  const value = cell.model.getMetadata(EXECUTION_METADATA_KEY) as
    Partial<IServerExecutionMetadata> | undefined;
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
