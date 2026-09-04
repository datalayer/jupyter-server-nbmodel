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
import {
  normalizeServerOutputs,
  reconcileOutputSnapshot
} from './outputReconciliation';
import { requestServer } from './requestServer';
import { isOutputRecoveryEnabled } from './settings';
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
          /*
           * The path of the session, when it names a file of the server.
           *
           * An editor with no file of its own — a notebook of a Datalayer
           * space, one built from a model — still needs a session, and a
           * session is named by a path: those are kept under `.datalayer/`
           * and name nothing on disk. Sending one makes the server look up a
           * document at a path it will never find, index a file id for it and
           * warn about it, all before falling back to the identifier of the
           * room, which is what could answer from the start. Only a path that
           * names something is worth sending.
           */
          const sessionPath = sessionContext.session?.path;
          const documentPath = sessionPath?.startsWith('.datalayer/')
            ? undefined
            : sessionPath;
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
          /*
           * What the cell showed belongs to the previous execution.
           *
           * The server clears the outputs of the shared cell when it starts
           * executing, and JupyterLab clears them in its own executor. This
           * is the same gesture on the client: without it, a cell that is run
           * again keeps what it had, and the outputs of the new execution are
           * appended below the old ones.
           *
           * Through the output area, as JupyterLab does, and never through
           * the shared model: replacing the outputs array of a cell is what
           * can expose a half-built nested value to the widget being
           * constructed — the failure that once left a notebook empty. And
           * whatever it does, it must not keep the cell from running, hence
           * the guard.
           */
          try {
            (cell as CodeCell).outputArea.model.clear();
          } catch (reason) {
            console.warn(
              '[jupyter-server-nbmodel] Failed to clear the outputs of the cell.',
              reason
            );
          }
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
          /*
           * A document the server cannot write into has no other path.
           *
           * The recovery is asked for rather than assumed because it makes
           * this extension a second writer of the shared document. That
           * caution is meaningless when there is no shared document for the
           * server to write into at all: a notebook of a Datalayer space is
           * collaborated on through the spacer, names no room of this server
           * and no file of it, so nothing streams into it — the outputs of a
           * cell reach the page that asked for them, and a page that reloads
           * mid-run inherits nothing. The answers of the server are then the
           * only way its outputs are ever seen, during the run and after it.
           *
           * The path is what says so, and the identifier is not: a notebook of
           * a space HAS a document identifier — the one of its room on the
           * spacer — which this server cannot resolve to anything, so keying
           * on it left the recovery off for the very notebooks that have
           * nothing else. A path is sent only when it names a file of this
           * server; see where it is built above.
           */
          const recoverOutputs = isOutputRecoveryEnabled() || !documentPath;
          console.info('[jupyter-server-nbmodel] Executing cell', {
            cellId,
            documentId,
            documentPath,
            sessionPath,
            recoverOutputs,
            settingEnabled: isOutputRecoveryEnabled()
          });
          if (recoverOutputs) {
            markClientOwnedExecution(cell as CodeCell);
          }
          const releaseSubmission =
            await this._submissionQueue.acquire(kernelId);
          let success: boolean;
          try {
            // FIXME quid of deletedCells and timing record.
            const response = await requestServer(
              cell as CodeCell,
              executeApiURL,
              init,
              this._serverSettings,
              translator,
              100,
              releaseSubmission,
              recoverOutputs
            );
            const data = await response.json();
            success = data['status'] === 'ok';
            if (recoverOutputs) {
              clearServerExecutionMetadata(cell as CodeCell);
            }

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

            /*
             * One FLAT line, never an object.
             *
             * A console groups an object behind a disclosure triangle, and
             * what is copied out of it is the collapsed form — the counts
             * that say on which side the outputs were lost end up as an
             * ellipsis in every report. A string cannot be collapsed.
             */
            console.debug(
              `[jupyter-server-nbmodel] Execution completed: cell=${cellId} ` +
                `http=${response.status} status=${String(data['status'])} ` +
                `serverOutputs=${serverOutputs.length} ` +
                `sharedOutputs=${sharedOutputs.length} ` +
                `sharedModelUpdates=${sharedModelUpdates} ` +
                `outputsMissing=${outputsMissing} ` +
                `executionCountMissing=${executionCountMissing} ` +
                `recoverOutputs=${recoverOutputs} ` +
                `documentId=${String(documentId)} ` +
                `documentPath=${String(documentPath)}`
            );

            // Server-side collaboration is the streaming path. If its update
            // was unavailable (no collaboration extension) or could not be
            // integrated (for example an invalid historical YStore), reconcile
            // the completed result returned by the REST endpoint in the client.
            //
            // A cell whose shared model heard NOTHING during the execution is
            // reconciled whatever the recovery setting says: the server wrote
            // its outputs into a shared document this editor evidently does
            // not receive, and the reply is then the only way the user sees
            // them at all. The recovery setting keeps governing the richer
            // case where the shared path delivered and the reply merely
            // completes it.
            const sharedPathSilent = sharedModelUpdates === 0;
            if (
              (recoverOutputs || sharedPathSilent) &&
              (outputsMissing || executionCountMissing)
            ) {
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
              // The same reconciliation as the pending snapshots, and for the
              // same reason: this answer is one view of the execution, not
              // the authority on it. An interrupted cell, whose stream the
              // browser read further than the snapshot did, would otherwise
              // lose that stream and keep only the error that stopped it.
              reconcileOutputSnapshot(cell as CodeCell, serverOutputs);
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
      serverSettings,
      undefined,
      100,
      undefined,
      // A resumed request is the recovery: this page inherited it, and the
      // outputs it missed can only come from the answers of the server.
      true
    );
    const data = await response.json();
    const serverOutputs = normalizeServerOutputs(data['outputs'] ?? '[]');
    reconcileOutputSnapshot(cell, serverOutputs);
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
