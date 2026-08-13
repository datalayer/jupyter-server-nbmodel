/*
 * Copyright (c) 2024-2025 Datalayer, Inc.
 *
 * Distributed under the terms of the Modified BSD License.
 */

import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import { CodeCell } from '@jupyterlab/cells';
import { URLExt } from '@jupyterlab/coreutils';
import {
  INotebookCellExecutor,
  INotebookTracker,
  NotebookPanel
} from '@jupyterlab/notebook';
import { ServerConnection } from '@jupyterlab/services';
import { ISettingRegistry } from '@jupyterlab/settingregistry';
import {
  getServerExecutionMetadata,
  isClientOwnedExecution,
  setServerExecutionMetadata
} from './executionMetadata';
import {
  NotebookCellServerExecutor,
  resumeCellServerExecution
} from './executor';
import { restoreKernelModelStatus, restoreKernelStatus } from './kernelStatus';
import {
  isOutputRecoveryEnabled,
  OUTPUT_RECOVERY_SETTING,
  PLUGIN_ID,
  setOutputRecoveryEnabled
} from './settings';

const resumedRequests = new Set<string>();

interface IActiveExecution {
  cell_id?: string;
  document_path?: string;
  kernel_id: string;
  request_id: string;
  request_url: string;
}

async function resumeCellExecution(
  cell: CodeCell,
  panel: NotebookPanel,
  app: JupyterFrontEnd
): Promise<void> {
  if (panel.isDisposed || !isOutputRecoveryEnabled()) {
    return;
  }

  // Metadata created by the executor in this page is already being polled by
  // that executor. The restorer only owns requests inherited across reloads.
  if (isClientOwnedExecution(cell)) {
    return;
  }

  const execution = getServerExecutionMetadata(cell);
  if (!execution || resumedRequests.has(execution.requestId)) {
    return;
  }

  resumedRequests.add(execution.requestId);
  const requestUrl = execution.requestUrl.startsWith(
    app.serviceManager.serverSettings.baseUrl
  )
    ? execution.requestUrl
    : URLExt.join(
        app.serviceManager.serverSettings.baseUrl,
        execution.requestUrl
      );
  restoreKernelStatus(panel, 'busy');
  void resumeCellServerExecution(
    cell,
    requestUrl,
    app.serviceManager.serverSettings
  )
    .then(() => {
      restoreKernelStatus(panel, 'idle');
    })
    .catch(error => {
      restoreKernelStatus(panel, 'unknown');
      console.error(
        `Failed to resume server execution ${execution.requestId}.`,
        error
      );
    })
    .finally(() => {
      resumedRequests.delete(execution.requestId);
    });
}

async function watchNotebookExecutions(
  panel: NotebookPanel,
  app: JupyterFrontEnd
): Promise<void> {
  await Promise.all([panel.context.ready, panel.context.sessionContext.ready]);
  // Everything below recovers an execution this page did not start: it reads
  // the answers of the server rather than the shared document, which is what
  // the setting turns on.
  if (panel.isDisposed || !isOutputRecoveryEnabled()) {
    return;
  }

  try {
    await restoreKernelModelStatus(panel);
  } catch (error) {
    console.warn(
      '[jupyter-server-nbmodel] Failed to restore the kernel model status.',
      error
    );
  }

  const watchedCells = new WeakSet<CodeCell>();
  const watchCells = (): void => {
    for (const widget of panel.content.widgets) {
      if (!(widget instanceof CodeCell) || watchedCells.has(widget)) {
        continue;
      }
      watchedCells.add(widget);
      widget.model.metadataChanged.connect((_sender, change) => {
        if (change.key === 'jupyter_server_nbmodel') {
          void resumeCellExecution(widget, panel, app);
        }
      });
      void resumeCellExecution(widget, panel, app);
    }
  };

  // YStore/collaboration updates may restore the active request metadata just
  // after the document context becomes ready. Keep watching instead of making
  // restoration depend on that one timing window.
  panel.content.modelContentChanged.connect(watchCells);
  panel.disposed.connect(() => {
    panel.content.modelContentChanged.disconnect(watchCells);
  });
  watchCells();

  const kernelId = panel.context.sessionContext.session?.kernel?.id;
  if (!kernelId) {
    return;
  }
  const executeUrl = URLExt.join(
    app.serviceManager.serverSettings.baseUrl,
    `api/kernels/${kernelId}/execute`
  );
  try {
    const response = await ServerConnection.makeRequest(
      executeUrl,
      { method: 'GET' },
      app.serviceManager.serverSettings
    );
    if (!response.ok) {
      throw await ServerConnection.ResponseError.create(response);
    }
    const payload = (await response.json()) as {
      requests?: IActiveExecution[];
    };
    const documentPath = panel.context.sessionContext.session?.path;
    for (const execution of payload.requests ?? []) {
      if (
        !execution.cell_id ||
        (execution.document_path && execution.document_path !== documentPath)
      ) {
        continue;
      }
      const cell = panel.content.widgets.find(
        widget =>
          widget instanceof CodeCell &&
          widget.model.sharedModel.getId() === execution.cell_id
      );
      if (!(cell instanceof CodeCell)) {
        continue;
      }
      setServerExecutionMetadata(cell, {
        kernelId: execution.kernel_id,
        requestId: execution.request_id,
        requestUrl: execution.request_url
      });
      void resumeCellExecution(cell, panel, app);
    }
  } catch (error) {
    console.warn(
      '[jupyter-server-nbmodel] Failed to discover active executions.',
      error
    );
  }
}

export const notebookCellExecutor: JupyterFrontEndPlugin<INotebookCellExecutor> =
  {
    id: '@datalayer/jupyter-server-nbmodel:notebook-cell-executor',
    description:
      'Add notebook cell executor that uses REST API instead of kernel protocol over WebSocket.',
    autoStart: true,
    provides: INotebookCellExecutor,
    optional: [ISettingRegistry],
    activate: (
      app: JupyterFrontEnd,
      settingRegistry: ISettingRegistry | null
    ): INotebookCellExecutor => {
      const executor = new NotebookCellServerExecutor({
        serverSettings: app.serviceManager.serverSettings
      });
      // Whether the outputs are recovered over HTTP, now and whenever the
      // user changes their mind; the streaming path alone is the default.
      void settingRegistry
        ?.load(PLUGIN_ID)
        .then(settings => {
          const read = (current: ISettingRegistry.ISettings) => {
            setOutputRecoveryEnabled(
              current.get(OUTPUT_RECOVERY_SETTING).composite === true
            );
          };
          read(settings);
          settings.changed.connect(read);
        })
        .catch(reason => {
          // Without the schema the recovery cannot be asked for, which is
          // worth saying out loud: the outputs then only reach the notebook
          // through the shared document.
          console.warn(
            `[jupyter-server-nbmodel] Failed to load the settings of ${PLUGIN_ID}; the output recovery stays off.`,
            reason
          );
        });
      console.log('JupyterLab extension jupyter-server-nbmodel is activated!');
      return executor;
    }
  };

export const notebookExecutionRestorer: JupyterFrontEndPlugin<void> = {
  id: '@datalayer/jupyter-server-nbmodel:notebook-execution-restorer',
  description: 'Resumes server-side notebook executions after a page refresh.',
  autoStart: true,
  requires: [INotebookTracker],
  activate: (app: JupyterFrontEnd, tracker: INotebookTracker): void => {
    tracker.widgetAdded.connect((_sender, panel) => {
      void watchNotebookExecutions(panel, app);
    });
    tracker.forEach(panel => {
      void watchNotebookExecutions(panel, app);
    });
  }
};
