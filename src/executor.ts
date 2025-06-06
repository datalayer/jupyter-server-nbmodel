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
import { requestServer } from './requestServer';

/**
 * Notebook cell executor posting a request to the server for execution.
 */
export class NotebookCellServerExecutor implements INotebookCellExecutor {
  private _serverSettings: ServerConnection.ISettings;

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
          const executeApiURL = URLExt.join(
            this._serverSettings.baseUrl,
            `api/kernels/${kernelId}/execute`
          );
          const code = cell.model.sharedModel.getSource();
          const cellId = cell.model.sharedModel.getId();
          const documentId = notebook.sharedModel.getState('document_id');
          const { recordTiming } = notebookConfig;
          const init = {
            method: 'POST',
            body: JSON.stringify({
              code,
              metadata: {
                cell_id: cellId,
                document_id: documentId,
                record_timing: recordTiming
              }
            })
          };
          onCellExecutionScheduled({ cell });
          let success = false;
          try {
            // FIXME quid of deletedCells and timing record.
            const response = await requestServer(
              cell as CodeCell,
              executeApiURL,
              init,
              this._serverSettings,
              translator
            );
            const data = await response.json();
            success = data['status'] === 'ok';
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

export default NotebookCellServerExecutor;
