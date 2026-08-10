/*
 * Copyright (c) 2024-2025 Datalayer, Inc.
 *
 * Distributed under the terms of the Modified BSD License.
 */

import { Dialog, showDialog } from '@jupyterlab/apputils';
import { URLExt } from '@jupyterlab/coreutils';
import { CodeCell } from '@jupyterlab/cells';
import type { ICodeCellModel, MarkdownCell } from '@jupyterlab/cells';
import { INotebookCellExecutor } from '@jupyterlab/notebook';
import { ServerConnection } from '@jupyterlab/services';
import { nullTranslator } from '@jupyterlab/translation';
import { JSONExt } from '@lumino/coreutils';
import {
  clearServerExecutionMetadata,
  markClientOwnedExecution,
  unmarkClientOwnedExecution
} from './executionMetadata';
import { normalizeServerOutputs } from './outputReconciliation';
import { requestServer } from './requestServer';
import { KernelSubmissionQueue } from './submissionQueue';

/**
 * Notebook cell executor posting a request to the server for execution.
 */
export class NotebookCellServerExecutor implements INotebookCellExecutor {
  private _serverSettings: ServerConnection.ISettings;
  private _submissionQueue = new KernelSubmissionQueue();

  /**
   * Constructor
   *
   * @param options Constructor options; the contents manager, the collaborative drive and optionally the server settings.
   */
  constructor(options: { serverSettings?: ServerConnection.ISettings }) {
    this._serverSettings =
      options.serverSettings ?? ServerConnection.makeSettings();
  }

  /**
   * Execute a given cell of the notebook.
   *
   * @param options Execution options
   * @returns Execution success status
   */
  async runCell({
    cell,
    notebook,
    notebookConfig,
    onCellExecuted,
    onCellExecutionScheduled,
    sessionContext,
    sessionDialogs,
    translator
  }: INotebookCellExecutor.IRunCellOptions): Promise<boolean> {
    translator = translator ?? nullTranslator;
    const trans = translator.load('jupyterlab');
    switch (cell.model.type) {
      case 'markdown':
        (cell as MarkdownCell).rendered = true;
        cell.inputHidden = false;
        onCellExecuted({ cell, success: true });
        break;
      case 'code':
        if (sessionContext) {
          if (sessionContext.isTerminating) {
            await showDialog({
              title: trans.__('Kernel Terminating'),
              body: trans.__(
                'The kernel for %1 appears to be terminating. You can not run any cell for now.',
                sessionContext.session?.path
              ),
              buttons: [Dialog.okButton()]
            });
            break;
          }
          if (sessionContext.pendingInput) {
            await showDialog({
              title: trans.__('Cell not executed due to pending input'),
              body: trans.__(
                'The cell has not been executed to avoid kernel deadlock as there is another pending input! Submit your pending input and try again.'
              ),
              buttons: [Dialog.okButton()]
            });
            return false;
          }
          if (sessionContext.hasNoKernel) {
            const shouldSelect = await sessionContext.startKernel();
            if (shouldSelect && sessionDialogs) {
              await sessionDialogs.selectKernel(sessionContext);
            }
          }
          if (sessionContext.hasNoKernel) {
            cell.model.sharedModel.transact(() => {
              (cell.model as ICodeCellModel).clearExecution();
            });
            return true;
          }
          const kernelId = sessionContext?.session?.kernel?.id;
          if (!kernelId) {
            cell.model.sharedModel.transact(() => {
              (cell.model as ICodeCellModel).clearExecution();
            });
            return true;
          }
          const executeApiURL = URLExt.join(
            this._serverSettings.baseUrl,
            `api/kernels/${kernelId}/execute`
          );
          const code = cell.model.sharedModel.getSource();
          const cellId = cell.model.sharedModel.getId();
          const documentId = notebook.sharedModel.getState('document_id');
          const documentPath = sessionContext.session?.path;
          const { recordTiming } = notebookConfig;
          const kernelSettings = sessionContext.session?.kernel?.serverSettings;
          const remoteServer =
            kernelSettings &&
            kernelSettings.baseUrl !== this._serverSettings.baseUrl
              ? {
                  url: kernelSettings.baseUrl,
                  token: kernelSettings.token
                }
              : undefined;
          const init = {
            method: 'POST',
            body: JSON.stringify({
              code,
              server: remoteServer,
              metadata: {
                cell_id: cellId,
                document_id: documentId,
                document_path: documentPath,
                record_timing: recordTiming
              }
            })
          };
          onCellExecutionScheduled({ cell });
          const sharedCodeCell = (cell.model as ICodeCellModel).sharedModel;
          let sharedModelUpdates = 0;
          const onSharedModelChanged = (): void => {
            sharedModelUpdates += 1;
            if (sharedModelUpdates <= 3) {
              console.debug(
                '[jupyter-server-nbmodel] Received shared cell update',
                {
                  cellId,
                  documentId,
                  documentPath,
                  sharedModelUpdates
                }
              );
            }
          };
          sharedCodeCell.changed.connect(onSharedModelChanged);
          markClientOwnedExecution(cell as CodeCell);
          const releaseSubmission =
            await this._submissionQueue.acquire(kernelId);
          let success = false;
          try {
            // FIXME quid of deletedCells and timing record.
            const response = await requestServer(
              cell as CodeCell,
              executeApiURL,
              init,
              this._serverSettings,
              translator,
              100,
              releaseSubmission
            );
            const data = await response.json();
            success = data['status'] === 'ok';
            clearServerExecutionMetadata(cell as CodeCell);

            const serverOutputs = normalizeServerOutputs(
              data['outputs'] ?? '[]'
            );
            const sharedOutputs = sharedCodeCell.getOutputs();
            const executionCount = data['execution_count'] ?? null;
            const outputsMissing = !JSONExt.deepEqual(
              sharedOutputs,
              serverOutputs
            );
            const executionCountMissing =
              sharedCodeCell.execution_count !== executionCount;

            console.debug('[jupyter-server-nbmodel] Execution completed', {
              cellId,
              documentId,
              documentPath,
              sharedModelUpdates,
              serverOutputCount: serverOutputs.length,
              sharedOutputCount: sharedOutputs.length,
              outputsMissing,
              executionCountMissing
            });

            // Server-side collaboration is the streaming path. If its update
            // was unavailable (no collaboration extension) or could not be
            // integrated (for example an invalid historical YStore), reconcile
            // the completed result returned by the REST endpoint in the client.
            if (outputsMissing || executionCountMissing) {
              console.warn(
                '[jupyter-server-nbmodel] Reconciling missing server execution update',
                {
                  cellId,
                  documentId,
                  documentPath,
                  sharedModelUpdates,
                  outputsMissing,
                  executionCountMissing
                }
              );
              sharedCodeCell.setOutputs(serverOutputs);
              sharedCodeCell.execution_count = executionCount;
              sharedCodeCell.executionState = 'idle';
            }
          } catch (error: unknown) {
            onCellExecuted({
              cell,
              success: false
            });
            if (cell.isDisposed) {
              return false;
            } else {
              throw error;
            }
          } finally {
            // Also release on an HTTP/network failure before the server could
            // acknowledge the request, so later cells are never deadlocked.
            releaseSubmission();
            unmarkClientOwnedExecution(cell as CodeCell);
            sharedCodeCell.changed.disconnect(onSharedModelChanged);
          }
          onCellExecuted({ cell, success });
          return true;
        }
        cell.model.sharedModel.transact(() => {
          (cell.model as ICodeCellModel).clearExecution();
        }, false);
        break;
      default:
        break;
    }
    return Promise.resolve(true);
  }
}

/**
 * Resume polling an execution request persisted in cell metadata.
 */
export async function resumeCellServerExecution(
  cell: CodeCell,
  requestUrl: string,
  serverSettings: ServerConnection.ISettings
): Promise<boolean> {
  const sharedCodeCell = (cell.model as ICodeCellModel).sharedModel;
  sharedCodeCell.executionState = 'running';
  try {
    const response = await requestServer(
      cell,
      requestUrl,
      { method: 'GET' },
      serverSettings
    );
    const data = await response.json();
    const serverOutputs = normalizeServerOutputs(data['outputs'] ?? '[]');
    if (!JSONExt.deepEqual(sharedCodeCell.getOutputs(), serverOutputs)) {
      sharedCodeCell.setOutputs(serverOutputs);
    }
    sharedCodeCell.execution_count = data['execution_count'] ?? null;
    sharedCodeCell.executionState = 'idle';
    clearServerExecutionMetadata(cell);
    return data['status'] === 'ok';
  } catch (error) {
    if (
      error instanceof ServerConnection.ResponseError &&
      error.response.status === 404
    ) {
      clearServerExecutionMetadata(cell);
      sharedCodeCell.executionState = 'idle';
      return false;
    }
    throw error;
  }
}

export default NotebookCellServerExecutor;
