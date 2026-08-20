/*
 * Copyright (c) 2024-2025 Datalayer, Inc.
 *
 * Distributed under the terms of the Modified BSD License.
 */

import type { CodeCell } from '@jupyterlab/cells';
import { URLExt } from '@jupyterlab/coreutils';
import { OutputPrompt, Stdin } from '@jupyterlab/outputarea';
import { Kernel, ServerConnection } from '@jupyterlab/services';
import { IHeader, IInputReply } from '@jupyterlab/services/lib/kernel/messages';
import type { ITranslator } from '@jupyterlab/translation';
import { PromiseDelegate } from '@lumino/coreutils';
import { Panel } from '@lumino/widgets';
import { setServerExecutionMetadata } from './executionMetadata';
import {
  normalizeServerOutputs,
  reconcileOutputSnapshot
} from './outputReconciliation';

/**
 * Polling interval for accepted execution requests.
 */
const MAX_POLLING_INTERVAL = 1000;

interface IOutputReconciliationState {
  accepted?: boolean;
  onAccepted?: () => void;
  /** Whether the outputs of the pending answers are read into the cell. */
  recoverOutputs?: boolean;
  serverOutputs?: any[];
  /**
   * Where the request is polled, read from the BODY of the answers.
   *
   * The server names it in the `Location` header too, but that header is
   * not CORS-safelisted: a page served from another origin — the React
   * application against a local Jupyter server — reads `null` there, fell
   * back to re-GETting the execute URL, and took the pending-requests
   * listing it got back as the final result of the execution. The body is
   * readable from any origin.
   */
  requestUrl?: string;
}

interface IRequestProgressPayload {
  kernel_id?: string;
  outputs?: string | unknown[];
  request_id?: string;
  request_url?: string;
}

async function reconcilePendingOutputs(
  cell: CodeCell,
  response: Response,
  state: IOutputReconciliationState
): Promise<void> {
  let payload: IRequestProgressPayload;
  try {
    payload = await response.json();
  } catch {
    return;
  }
  if (payload.request_id && payload.kernel_id) {
    const requestUrl = response.headers.get('Location') ?? payload.request_url;
    if (requestUrl) {
      state.requestUrl = requestUrl;
    }
    // The metadata is what a reloaded page resumes from; it is only written
    // when that recovery is in use.
    if (requestUrl && state.recoverOutputs) {
      setServerExecutionMetadata(cell, {
        kernelId: payload.kernel_id,
        requestId: payload.request_id,
        requestUrl
      });
    }
    if (!state.accepted) {
      state.accepted = true;
      state.onAccepted?.();
    }
  }

  if (payload.outputs === undefined || !state.recoverOutputs) {
    return;
  }
  const serverOutputs = normalizeServerOutputs(payload.outputs);
  const previousServerOutputs = state.serverOutputs;
  state.serverOutputs = serverOutputs;
  reconcileOutputSnapshot(cell, serverOutputs, previousServerOutputs);
}

async function requestServerWithState(
  cell: CodeCell,
  url: string,
  init: RequestInit,
  settings: ServerConnection.ISettings,
  translator?: ITranslator,
  interval = 100,
  reconciliationState: IOutputReconciliationState = {}
): Promise<Response> {
  const promise = new PromiseDelegate<Response>();
  ServerConnection.makeRequest(url, init, settings)
    .then(async response => {
      if (!response.ok) {
        if (response.status === 300) {
          let replyUrl = response.headers.get('Location') || '';
          if (!replyUrl) {
            // The header is not CORS-safelisted; the input endpoint of the
            // kernel is fixed, so it is derived from the URL being polled.
            const kernelUrl = url.match(/^(.*\/api\/kernels\/[^/?]+)/);
            replyUrl = kernelUrl ? `${kernelUrl[1]}/input` : '';
          }
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
                inputReply: IInputReply,
                parentHeader: IHeader<'input_request'>
              ) => {
                ServerConnection.makeRequest(
                  replyUrl,
                  {
                    method: 'POST',
                    body: JSON.stringify({ input: inputReply.value })
                  },
                  settings
                ).catch(error => {
                  console.error(
                    `Failed to set input to ${JSON.stringify(inputReply)}.`,
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
                const response = await requestServerWithState(
                  cell,
                  url,
                  init,
                  settings,
                  translator,
                  100,
                  reconciliationState
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
        await reconcilePendingOutputs(cell, response, reconciliationState);
        // Header first, body second (see IOutputReconciliationState), and
        // only then the polled URL itself — re-GETting the execute URL is
        // right when THAT is the requests URL, and wrong on the first hop.
        let redirectUrl =
          response.headers.get('Location') ||
          reconciliationState.requestUrl ||
          url;
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
            interval?: number,
            reconciliationState?: IOutputReconciliationState
          ) => {
            try {
              const response = await requestServerWithState(
                cell,
                url,
                init,
                settings,
                translator,
                interval,
                reconciliationState
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
          Math.min(MAX_POLLING_INTERVAL, interval * 2),
          reconciliationState
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

export async function requestServer(
  cell: CodeCell,
  url: string,
  init: RequestInit,
  settings: ServerConnection.ISettings,
  translator?: ITranslator,
  interval = 100,
  onAccepted?: () => void,
  /** Whether the pending answers are read into the cell; see `settings`. */
  recoverOutputs = false
): Promise<Response> {
  return requestServerWithState(
    cell,
    url,
    init,
    settings,
    translator,
    interval,
    { onAccepted, recoverOutputs }
  );
}

export default requestServer;
