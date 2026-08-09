/*
 * Copyright (c) 2024-2025 Datalayer, Inc.
 *
 * Distributed under the terms of the Modified BSD License.
 */

import type { CodeCell } from '@jupyterlab/cells';
import {
  clearServerExecutionMetadata,
  getServerExecutionMetadata,
  setServerExecutionMetadata
} from '../executionMetadata';
import {
  isOutputPrefix,
  reconcileOutputSnapshot
} from '../outputReconciliation';
import { restoreKernelModelStatus, restoreKernelStatus } from '../kernelStatus';
import type { NotebookPanel } from '@jupyterlab/notebook';

const stream = (text: string): any => ({
  output_type: 'stream',
  name: 'stdout',
  text
});

function createCell(outputs: any[]): {
  cell: CodeCell;
  add: jest.Mock;
  clear: jest.Mock;
  setOutputs: jest.Mock;
} {
  const add = jest.fn();
  const clear = jest.fn();
  const setOutputs = jest.fn();
  return {
    cell: {
      model: {
        sharedModel: {
          getId: () => 'cell-id',
          getOutputs: () => outputs,
          setOutputs
        }
      },
      outputArea: { model: { add, clear } }
    } as unknown as CodeCell,
    add,
    clear,
    setOutputs
  };
}

describe('jupyter-server-nbmodel', () => {
  it('restores status through the standard kernel connection', () => {
    const updateStatus = jest.fn();
    const panel = {
      context: {
        sessionContext: {
          session: { kernel: { _updateStatus: updateStatus } }
        }
      }
    } as unknown as NotebookPanel;

    expect(restoreKernelStatus(panel, 'busy')).toBe(true);
    expect(updateStatus).toHaveBeenCalledWith('busy');
  });

  it('restores the execution state from the existing kernel model', async () => {
    const updateStatus = jest.fn();
    const refreshRunning = jest.fn().mockResolvedValue(undefined);
    const panel = {
      context: {
        sessionContext: {
          kernelManager: {
            refreshRunning,
            running: () =>
              [{ id: 'kernel-id', execution_state: 'busy' }][Symbol.iterator]()
          },
          session: {
            kernel: {
              id: 'kernel-id',
              status: 'unknown',
              _updateStatus: updateStatus
            }
          }
        }
      }
    } as unknown as NotebookPanel;

    expect(await restoreKernelModelStatus(panel)).toBe(true);
    expect(refreshRunning).toHaveBeenCalledTimes(1);
    expect(updateStatus).toHaveBeenCalledWith('busy');
  });

  it('persists the request identity needed after refresh', () => {
    let metadata: unknown;
    const cell = {
      model: {
        getMetadata: () => metadata,
        setMetadata: (_key: string, value: unknown) => {
          metadata = value;
        },
        deleteMetadata: () => {
          metadata = undefined;
        }
      }
    } as unknown as CodeCell;
    const execution = {
      kernelId: 'kernel-id',
      requestId: 'request-id',
      requestUrl: '/api/kernels/kernel-id/requests/request-id'
    };

    setServerExecutionMetadata(cell, execution);
    expect(getServerExecutionMetadata(cell)).toEqual(execution);
    clearServerExecutionMetadata(cell);
    expect(getServerExecutionMetadata(cell)).toBeUndefined();
  });

  it('recognizes a shorter stream snapshot as a prefix', () => {
    expect(isOutputPrefix([stream('1\n')], [stream('1\n2\n')])).toBe(true);
    expect(isOutputPrefix([stream('1\n2\n')], [stream('1\n')])).toBe(false);
  });

  it('does not erase output for an initial empty snapshot', () => {
    const { cell, clear, setOutputs } = createCell([stream('1\n')]);

    reconcileOutputSnapshot(cell, []);

    expect(clear).not.toHaveBeenCalled();
    expect(setOutputs).not.toHaveBeenCalled();
  });

  it('does not replace output with an older stream snapshot', () => {
    const { cell, add, setOutputs } = createCell([stream('1\n2\n')]);

    reconcileOutputSnapshot(cell, [stream('1\n')], []);

    expect(add).not.toHaveBeenCalled();
    expect(setOutputs).not.toHaveBeenCalled();
  });

  it('appends only the missing stream suffix', () => {
    const { cell, add, setOutputs } = createCell([stream('1\n')]);

    reconcileOutputSnapshot(cell, [stream('1\n2\n')], [stream('1\n')]);

    expect(add).toHaveBeenCalledTimes(1);
    expect(add).toHaveBeenCalledWith(stream('2\n'));
    expect(setOutputs).not.toHaveBeenCalled();
  });
});
