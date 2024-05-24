import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import { Dialog, showDialog } from '@jupyterlab/apputils';
import {
  CodeCell,
  type ICodeCellModel,
  type MarkdownCell
} from '@jupyterlab/cells';
import { URLExt } from '@jupyterlab/coreutils';
import { INotebookCellExecutor } from '@jupyterlab/notebook';
import { OutputPrompt, Stdin } from '@jupyterlab/outputarea';
import { Kernel, ServerConnection } from '@jupyterlab/services';
import * as KernelMessage from '@jupyterlab/services/lib/kernel/messages';
import { nullTranslator, type ITranslator } from '@jupyterlab/translation';
import { PromiseDelegate } from '@lumino/coreutils';
import { Panel } from '@lumino/widgets';

/**
 * Polling interval for accepted execution requests.
 */
const MAX_POLLING_INTERVAL = 1000;

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
          const apiURL = URLExt.join(
            this._serverSettings.baseUrl,
            `api/kernels/${kernelId}/execute`
          );
          const code = cell.model.sharedModel.getSource();
          const cellId = cell.model.sharedModel.getId();
          const documentId = notebook.sharedModel.getState('document_id');

          const init = {
            method: 'POST',
            body: JSON.stringify({
              code,
              metadata: { cell_id: cellId, document_id: documentId }
            })
          };
          onCellExecutionScheduled({ cell });
          let success = false;
          try {
            // FIXME quid of deletedCells and timing record
            const response = await requestServer(
              cell as CodeCell,
              apiURL,
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

async function requestServer(
  cell: CodeCell,
  url: string,
  init: RequestInit,
  settings: ServerConnection.ISettings,
  translator?: ITranslator,
  interval = 100
): Promise<Response> {
  const promise = new PromiseDelegate<Response>();
  ServerConnection.makeRequest(url, init, settings)
    .then(async response => {
      if (!response.ok) {
        if (response.status === 300) {
          let replyUrl = response.headers.get('Location') || '';

          if (!replyUrl.startsWith(settings.baseUrl)) {
            replyUrl = URLExt.join(settings.baseUrl, replyUrl);
          }
          const { parent_header, input_request } = await response.json();
          // TODO only the client sending the snippet will be prompted for the input
          // we can have a deadlock if its connection is lost.
          const panel = new Panel();
          panel.addClass('jp-OutputArea-child');
          panel.addClass('jp-OutputArea-stdin-item');

          const prompt = new OutputPrompt();
          prompt.addClass('jp-OutputArea-prompt');
          panel.addWidget(prompt);

          const input = new Stdin({
            future: Object.freeze({
              sendInputReply: (
                content: KernelMessage.IInputReply,
                parent_header: KernelMessage.IHeader<'input_request'>
              ) => {
                ServerConnection.makeRequest(
                  replyUrl,
                  {
                    method: 'POST',
                    body: JSON.stringify({ input: content.value })
                  },
                  settings
                ).catch(error => {
                  console.error(
                    `Failed to set input to ${JSON.stringify(content)}.`,
                    error
                  );
                });
              }
            }) as Kernel.IShellFuture,
            parent_header,
            password: input_request.password,
            prompt: input_request.prompt,
            translator
          });
          input.addClass('jp-OutputArea-output');
          panel.addWidget(input);

          // Get the input node to ensure focus after updating the model upon user reply.
          const inputNode = input.node.getElementsByTagName('input')[0];

          void input.value.then(value => {
            panel.addClass('jp-OutputArea-stdin-hiding');

            // FIXME this is not great as the model should not be modified on the client.
            // Use stdin as the stream so it does not get combined with stdout.
            // Note: because it modifies DOM it may (will) shift focus away from the input node.
            cell.outputArea.model.add({
              output_type: 'stream',
              name: 'stdin',
              text: value + '\n'
            });
            // Refocus the input node after it lost focus due to update of the model.
            inputNode.focus();

            // Keep the input in view for a little while; this (along refocusing)
            // ensures that we can avoid the cell editor stealing the focus, and
            // leading to user inadvertently modifying editor content when executing
            // consecutive commands in short succession.
            window.setTimeout(async () => {
              // Tack currently focused element to ensure that it remains on it
              // after disposal of the panel with the old input
              // (which modifies DOM and can lead to focus jump).
              const focusedElement = document.activeElement;
              // Dispose the old panel with no longer needed input box.
              panel.dispose();
              // Refocus the element that was focused before.
              if (focusedElement && focusedElement instanceof HTMLElement) {
                focusedElement.focus();
              }

              try {
                const response = await requestServer(
                  cell,
                  url,
                  init,
                  settings,
                  translator
                );
                promise.resolve(response);
              } catch (error) {
                promise.reject(error);
              }
            }, 500);
          });

          cell.outputArea.layout.addWidget(panel);
        } else {
          promise.reject(await ServerConnection.ResponseError.create(response));
        }
      } else if (response.status === 202) {
        let redirectUrl = response.headers.get('Location') || url;

        if (!redirectUrl.startsWith(settings.baseUrl)) {
          redirectUrl = URLExt.join(settings.baseUrl, redirectUrl);
        }

        setTimeout(
          async (
            cell: CodeCell,
            url: string,
            init: RequestInit,
            settings: ServerConnection.ISettings,
            translator?: ITranslator,
            interval?: number
          ) => {
            try {
              const response = await requestServer(
                cell,
                url,
                init,
                settings,
                translator,
                interval
              );
              promise.resolve(response);
            } catch (error) {
              promise.reject(error);
            }
          },
          interval,
          cell,
          redirectUrl,
          { method: 'GET' },
          settings,
          translator,
          // Evanescent interval
          Math.min(MAX_POLLING_INTERVAL, interval * 2)
        );
      } else {
        promise.resolve(response);
      }
    })
    .catch(reason => {
      promise.reject(new ServerConnection.NetworkError(reason));
    });
  return promise.promise;
}

export const notebookCellExecutor: JupyterFrontEndPlugin<INotebookCellExecutor> =
  {
    id: 'jupyter-server-nbmodel:notebook-cell-executor',
    description:
      'Add notebook cell executor that uses REST API instead of kernel protocol over WebSocket.',
    autoStart: true,
    provides: INotebookCellExecutor,
    activate: (app: JupyterFrontEnd): INotebookCellExecutor => {
      return new NotebookCellServerExecutor({
        serverSettings: app.serviceManager.serverSettings
      });
    }
  };
